"""Person 5: the engineer's console logic (ui/incident.py), without Streamlit, the shop or a model."""
import operator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, TypedDict

import pytest
import yaml

from ui.incident import IncidentSession, approval_answer, firing_alerts, review_answer, to_alert

ROOT = Path(__file__).resolve().parent.parent
SHOP_RULES = yaml.safe_load((ROOT / "infra/prometheus/rules/shop.yml").read_text(encoding="utf-8"))["groups"][0]


def _api_rule(rule: dict) -> dict:
    """A rule as Prometheus's /api/v1/rules returns it (the expression is called `query`)."""
    return {"name": rule["alert"], "query": rule["expr"], "labels": rule["labels"],
            "annotations": rule["annotations"]}


def _firing(rule: dict, value: str = "0.111", state: str = "firing") -> dict:
    return {"labels": {"alertname": rule["alert"], **rule["labels"]}, "annotations": {},
            "state": state, "activeAt": "2026-11-06T12:00:00Z", "value": value}


def test_a_firing_shop_alert_becomes_the_graphs_alert():
    rule = next(r for r in SHOP_RULES["rules"] if r["alert"] == "CheckoutErrorRateHigh")
    alert = to_alert(_api_rule(rule), _firing(rule, "1.1111111111111111e-01"))
    assert alert.alert_name == "CheckoutErrorRateHigh" and alert.service == "checkout"
    assert alert.signal == "error_rate" and alert.threshold == 0.05
    assert alert.value == pytest.approx(0.1111111111111111)
    assert rule["expr"].startswith(f"({alert.query})") and "> 0.05" not in alert.query
    assert alert.summary == rule["annotations"]["summary"] and len(alert.fingerprint) == 16


@pytest.mark.parametrize("rule", SHOP_RULES["rules"], ids=lambda r: r["alert"])
def test_every_shop_rule_opens_an_incident_with_the_rules_own_query_and_threshold(rule):
    # Guards against drift from infra/prometheus/build_rules.py: the console rebuilds query and threshold.
    from eval.probes.faults import MOVE_THRESHOLDS
    signal = rule["labels"]["signal"]
    relative = signal not in ("error_rate", "error_count")
    alert = to_alert(_api_rule(rule), _firing(rule), baseline=100.0 if relative else None)
    assert alert.service == rule["labels"]["service"] and alert.signal == signal
    assert rule["expr"].startswith(f"({alert.query})")
    absolute, rel = MOVE_THRESHOLDS[signal]
    if relative:
        assert f"+ {absolute} and" in rule["expr"] and f"* {1 + rel}" in rule["expr"]
        assert alert.threshold == max(100.0 + absolute, 100.0 * (1 + rel))
    else:
        assert float(rule["expr"].split()[-1]) == alert.threshold


def test_a_relative_rule_reads_its_baseline_at_the_time_it_fired():
    rule = next(r for r in SHOP_RULES["rules"] if r["labels"]["signal"] == "latency_p95_ms")
    seen = []

    def fetch(url):
        seen.append(url)
        if "/api/v1/rules" in url:
            return {"data": {"groups": [{"name": "shop", "rules": [{**_api_rule(rule), "alerts": [_firing(rule)]}]}]}}
        return {"data": {"result": [{"value": [0, "200.0"]}]}}

    [item] = firing_alerts("http://prom:9090", fetch=fetch)
    assert item["problem"] is None and item["alert"].threshold > 200.0
    assert "offset%2015m" in seen[1] and "time=2026-11-06T12%3A00%3A00Z" in seen[1]


def test_an_alert_that_cannot_open_an_incident_is_listed_with_the_reason():
    rule = next(r for r in SHOP_RULES["rules"] if r["labels"]["signal"] == "latency_p95_ms")

    def fetch(url):
        if "/api/v1/rules" in url:
            return {"data": {"groups": [{"name": "shop", "rules": [{**_api_rule(rule), "alerts": [_firing(rule)]}]}]}}
        return {"data": {"result": []}}           # no earlier level yet (the stack ran under 45 minutes)

    [item] = firing_alerts("http://prom:9090", fetch=fetch)
    assert item["alert"] is None and "earlier level" in item["problem"]


def test_only_the_shops_firing_and_pending_alerts_are_listed_firing_first():
    shop = [r for r in SHOP_RULES["rules"][:3]]
    data = {"data": {"groups": [
        {"name": "shop", "rules": [
            {**_api_rule(shop[0]), "alerts": [_firing(shop[0], state="pending")]},
            {**_api_rule(shop[1]), "alerts": [_firing(shop[1])]},
            {**_api_rule(shop[2]), "alerts": [_firing(shop[2], state="inactive")]},
        ]},
        {"name": "copilot", "rules": [{"name": "CopilotLoopOrBudget", "query": "x > 0", "labels": {},
                                       "annotations": {}, "alerts": [{"state": "firing", "value": "1"}]}]},
    ]}}
    listed = firing_alerts("http://prom:9090", fetch=lambda url: data)
    assert [(a["name"], a["state"]) for a in listed] == [(shop[1]["alert"], "firing"), (shop[0]["alert"], "pending")]
    assert all(a["alert"] is not None for a in listed)


def test_an_approval_is_bound_to_the_payload_hash_shown():
    payload = {"kind": "approval", "payload_hash": "cc90ac08", "action": {"tool": "set_flag"}}
    answer = approval_answer(payload, "approve", engineer="uppili", reason="error rate matches the flag")
    assert answer["decision"] == "approve" and answer["payload_hash"] == "cc90ac08"
    assert answer["decided_by"] == "uppili" and answer["token"] is None      # the gate mints the token


def test_reject_and_edit_answers():
    payload = {"payload_hash": "h", "action": {"tool": "set_flag", "flag": "paymentFailure", "variant": "off"}}
    rejected = approval_answer(payload, "reject", engineer="uppili")
    assert rejected["decision"] == "reject" and rejected["reason"] and rejected["payload_hash"] is None
    edited = approval_answer(payload, "edit", engineer="uppili",
                             edited_action={"tool": "set_flag", "flag": "paymentFailure", "variant": "10%"})
    assert edited["decision"] == "edit" and edited["edited_action"]["variant"] == "10%"
    with pytest.raises(ValueError):
        approval_answer(payload, "edit", engineer="uppili")
    with pytest.raises(ValueError):
        approval_answer(payload, "maybe", engineer="uppili")


def test_review_answer():
    assert review_answer(True, "uppili", "payment flag")["edited_root_cause"] == "payment flag"
    assert review_answer(False, "uppili")["accept_postmortem"] is False


# ------------------------------------------------------------------ the session, on a small stand-in graph

class _State(TypedDict, total=False):
    alert: object
    timeline: Annotated[list, operator.add]
    approval: dict
    review: dict


def _stand_in_graph(seen=None):
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    from copilot import toolctx

    def plan(state):
        if seen is not None:
            seen.append(toolctx.current().hooks)          # what a tool call made in this node would go through
        return {"timeline": [{"node": "plan", "kind": "plan", "text": "scope, hypotheses, test"}]}

    def gate(state):
        return {"approval": interrupt({"kind": "approval", "payload_hash": "h1",
                                       "action": {"tool": "set_flag", "flag": "paymentFailure", "variant": "off"}})}

    def review(state):
        return {"review": interrupt({"kind": "review", "postmortem": {"summary": "fixed"}})}

    g = StateGraph(_State)
    for name, fn in (("plan", plan), ("gate", gate), ("review", review)):
        g.add_node(name, fn)
    g.add_edge(START, "plan")
    g.add_edge("plan", "gate")
    g.add_edge("gate", "review")
    g.add_edge("review", END)
    return g.compile(checkpointer=MemorySaver())


def _runtime(graph):
    from copilot.config import AppSettings

    @asynccontextmanager
    async def runtime():
        yield SimpleNamespace(graph=graph, deps=SimpleNamespace(settings=AppSettings()))
    return runtime


def _alert():
    rule = SHOP_RULES["rules"][0]
    return to_alert(_api_rule(rule), _firing(rule))


def test_a_session_pauses_for_the_engineer_twice_and_finishes():
    session = IncidentSession(_alert(), engineer_id="uppili", runtime=_runtime(_stand_in_graph()))
    assert session.wait(("awaiting_approval",)) == "awaiting_approval"
    view = session.view()
    assert view["pending"]["payload_hash"] == "h1" and view["values"]["timeline"][0]["text"].startswith("scope")

    session.answer(approval_answer(view["pending"], "approve", engineer="uppili"))
    assert session.view()["status"] in ("running", "awaiting_review")      # never shows the answered pause again
    assert session.wait(("awaiting_review",)) == "awaiting_review"
    session.answer(review_answer(True, "uppili"))
    assert session.wait(("done",)) == "done"

    values = session.view()["values"]
    assert values["approval"]["decision"] == "approve" and values["approval"]["payload_hash"] == "h1"
    assert values["review"]["accept_postmortem"] is True


def test_the_graph_runs_under_this_incidents_hooks():
    from copilot.config import AppSettings
    from copilot.hooks import Hooks

    seen = []
    session = IncidentSession(_alert(), runtime=_runtime(_stand_in_graph(seen)))
    assert session.wait(("awaiting_approval",)) == "awaiting_approval"
    assert len(seen) == 1 and isinstance(seen[0], Hooks) and seen[0] is session.hooks
    assert seen[0].max_tool_calls == AppSettings().limits.tool_calls
    assert seen[0].permissions                              # config/permissions.yaml, as run_incident loads it


def test_answering_when_nothing_waits_is_refused():
    session = IncidentSession(_alert(), runtime=_runtime(_stand_in_graph()))
    session.wait(("awaiting_approval",))
    session.answer(approval_answer(session.view()["pending"], "reject", engineer="uppili"))
    with pytest.raises(RuntimeError):
        session.answer(approval_answer({"payload_hash": "h1"}, "reject", engineer="uppili"))


def test_a_failure_is_shown_not_raised():
    @asynccontextmanager
    async def broken():
        raise ConnectionError("MCP server mcp-telemetry is not answering")
        yield  # pragma: no cover

    session = IncidentSession(_alert(), runtime=broken)
    assert session.wait(("done",)) == "error"
    assert "mcp-telemetry" in session.view()["error"]


def test_a_session_uses_a_selector_loop_on_windows_when_the_dsn_is_only_in_dotenv(monkeypatch, tmp_path):
    """10 Nov dry run: POSTGRES_DSN only in .env, the incident's loop was Proactor and the console failed at once."""
    import asyncio

    from copilot import run as run_mod
    from copilot.memory import backends

    (tmp_path / ".env").write_text("POSTGRES_DSN=postgresql://u:p@localhost:5432/copilot" + chr(10))
    monkeypatch.setattr(run_mod, "ROOT", tmp_path)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.setattr(backends.sys, "platform", "win32")
    session = IncidentSession(_alert(), runtime=_runtime(_stand_in_graph()))
    assert isinstance(session._loop, asyncio.SelectorEventLoop)
    assert session.wait(("awaiting_approval",)) == "awaiting_approval"      # and the incident runs on it
