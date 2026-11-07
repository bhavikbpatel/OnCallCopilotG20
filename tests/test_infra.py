"""Person 5: compose stack, alert rules, dashboard and metrics stay consistent.

CI also runs `docker compose config` and `promtool check rules`; these tests
catch the same mistakes without Docker.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SHOP = ROOT / "vendor" / "opentelemetry-demo"
needs_shop = pytest.mark.skipif(not (SHOP / "compose.yaml").exists(),
                                reason="shop submodule not checked out (git submodule update --init)")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def shop_include(root_file: str) -> dict:
    return load(ROOT / root_file)["include"][0]


def ours() -> dict:
    return load(ROOT / "infra/compose/copilot.yaml")["services"]


# ------------------------------------------------------------------ compose

@needs_shop
@pytest.mark.parametrize("root_file", ["compose.yaml", "compose.lite.yaml"])
def test_included_files_exist_and_overrides_hit_real_services(root_file):
    inc = shop_include(root_file)
    for rel in inc["path"] + inc["env_file"]:
        assert (ROOT / rel).exists(), rel
    upstream = set()
    for rel in inc["path"]:
        if rel.startswith("vendor/"):
            upstream |= set(load(ROOT / rel).get("services") or {})
    for rel in inc["path"]:
        if not rel.startswith("vendor/"):
            extra = set(load(ROOT / rel).get("services") or {}) - upstream
            assert not extra, f"{rel} overrides services the shop does not have: {extra}"
    assert not set(ours()) & upstream, "our service names clash with the shop's"


@pytest.mark.parametrize("override", ["infra/compose/shop-monitoring.yaml", "infra/compose/shop-lite.yaml"])
def test_shop_override_mounts_resolve_from_the_shop_directory(override):
    for name, svc in load(ROOT / override)["services"].items():
        for vol in svc.get("volumes", []):
            src = vol.split(":")[0]
            assert (SHOP / src).resolve().exists(), f"{name}: {src}"


def test_our_services_bind_to_localhost_and_share_approval_state():
    for name, svc in ours().items():
        for port in svc.get("ports", []):
            assert port.startswith("127.0.0.1:"), f"{name} publishes {port} on every interface"
    mcp = {n: s for n, s in ours().items() if n.startswith("mcp-")}
    assert set(mcp) == {"mcp-telemetry", "mcp-runbooks", "mcp-tickets", "mcp-ops"}
    for name, svc in mcp.items():
        env = svc["environment"]
        assert env["APPROVAL_STATE_DB"] == "/state/approval_state.db", name
        assert env["MCP_HOST"] == "0.0.0.0" and env["MCP_TRANSPORT"] == "http", name
        assert "copilot-state:/state" in svc["volumes"], name


def test_mcp_ports_match_the_agent_config():
    servers = load(ROOT / "config/mcp_servers.yaml")["servers"]
    by_port = {re.search(r":(\d+)/", s["url"]).group(1) for s in servers.values()}
    assert {svc["environment"]["MCP_PORT"] for n, svc in ours().items() if n.startswith("mcp-")} == by_port


def test_heldout_cases_never_reach_an_image():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert "eval/heldout" in ignored


# ------------------------------------------------------------------ alerts

def test_shop_rules_are_generated_from_the_signals():
    done = subprocess.run([sys.executable, "infra/prometheus/build_rules.py", "--check"], cwd=ROOT,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_every_case_alert_has_a_rule():
    names = {load(p)["alert"]["name"] for p in (ROOT / "eval/cases/dev").glob("*.yaml")}
    rules = {r["alert"] for r in load(ROOT / "infra/prometheus/rules/shop.yml")["groups"][0]["rules"]}
    assert names <= rules, f"cases name alerts with no rule: {names - rules}"


def test_copilot_alerts_go_to_the_team_not_the_incident_endpoint():
    for rule in load(ROOT / "infra/prometheus/rules/agent.yml")["groups"][0]["rules"]:
        assert rule["labels"]["component"] == "copilot", rule["alert"]
    route = load(ROOT / "infra/alertmanager/alertmanager.yml")["route"]
    assert route["routes"][0] == {"matchers": ['component="copilot"'], "receiver": "team"}


# ------------------------------------------------------------------ metrics

def _defined_metrics() -> set:
    metrics = pytest.importorskip("copilot.metrics")
    from prometheus_client import REGISTRY
    names = set()
    for collector in list(REGISTRY._collector_to_names):
        for family in collector.collect():
            if family.name.startswith("copilot_"):
                names |= {family.name + s for s in ("", "_total", "_bucket", "_count", "_sum")}
    assert metrics.ENABLED
    return names


def _used(text: str) -> set:
    return set(re.findall(r"\bcopilot_[a-z_]+", text))


def test_dashboard_and_agent_rules_use_defined_metrics():
    defined = _defined_metrics()
    dashboard = json.loads((ROOT / "infra/grafana/dashboards/agent.json").read_text(encoding="utf-8"))
    exprs = " ".join(t["expr"] for p in dashboard["panels"] for t in p["targets"])
    rules = (ROOT / "infra/prometheus/rules/agent.yml").read_text(encoding="utf-8")
    assert not _used(exprs) - defined, _used(exprs) - defined
    assert not _used(rules) - defined, _used(rules) - defined


def test_loop_or_budget_alert_matches_every_budget_and_loop_block():
    metrics = pytest.importorskip("copilot.metrics")
    from prometheus_client import REGISTRY
    from copilot.run import BUDGET_BLOCKS

    rule = next(r for r in load(ROOT / "infra/prometheus/rules/agent.yml")["groups"][0]["rules"]
                if r["alert"] == "CopilotLoopOrBudget")
    alerted = set(re.search(r'reason=~"([^"]+)"', rule["expr"]).group(1).split("|"))
    assert alerted == set(BUDGET_BLOCKS) | {"loop_detected"}
    assert alerted == set(metrics.ALERT_BLOCK_REASONS)
    for reason in metrics.ALERT_BLOCK_REASONS:   # exported at 0 before any run, so the first block counts
        assert REGISTRY.get_sample_value("copilot_tool_calls_blocked_total", {"reason": reason}) is not None


def test_a_run_that_ran_out_of_budget_feeds_the_loop_or_budget_alert():
    metrics = pytest.importorskip("copilot.metrics")
    from prometheus_client import REGISTRY
    from eval.schemas.result import ResultLine

    data = json.loads((ROOT / "eval/schemas/examples/result_D-001.json").read_text(encoding="utf-8"))
    result = ResultLine.model_validate(data).model_copy(update={"status": "budget_exhausted"})
    before = REGISTRY.get_sample_value("copilot_budget_exhausted_total")
    assert before is not None          # unlabelled, so exported at 0 before any run
    metrics.record_run(result)
    assert REGISTRY.get_sample_value("copilot_budget_exhausted_total") == before + 1
    rule = next(r for r in load(ROOT / "infra/prometheus/rules/agent.yml")["groups"][0]["rules"]
                if r["alert"] == "CopilotLoopOrBudget")
    assert "copilot_budget_exhausted_total" in rule["expr"]


def test_record_run_counts_a_result_line():
    metrics = pytest.importorskip("copilot.metrics")
    from prometheus_client import REGISTRY
    from eval.schemas.result import ResultLine

    data = json.loads((ROOT / "eval/schemas/examples/result_D-001.json").read_text(encoding="utf-8"))
    result = ResultLine.model_validate(data)

    def value(name, **labels):
        return REGISTRY.get_sample_value(name, labels) or 0.0

    before = value("copilot_incidents_total", config_id=result.config_id, status=result.status,
                   final_status=result.final_status or "none")
    calls_before = value("copilot_tool_calls_per_incident_count")
    metrics.record_run(result)
    assert value("copilot_incidents_total", config_id=result.config_id, status=result.status,
                 final_status=result.final_status or "none") == before + 1
    assert value("copilot_tool_calls_per_incident_count") == calls_before + 1
