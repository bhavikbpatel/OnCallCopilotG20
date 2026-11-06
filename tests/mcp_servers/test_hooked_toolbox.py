"""HookedToolbox: the graph's code-driven tool calls go through the hooks (async)."""
from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("langgraph")        # copilot.clients imports the graph's state

from copilot.clients import ToolError                       # noqa: E402
from copilot.hooked_toolbox import HookedToolbox, use_role, with_role   # noqa: E402
from copilot.hooks import Hooks                             # noqa: E402

SECRET = "test-secret"


class FakeInner:
    """Stands in for McpToolbox: call() returns the parsed result or raises ToolError."""
    tools = {"x": 1}

    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    async def call(self, qualified, args):
        self.calls.append((qualified, args))
        value = self.results.get(qualified, {"done": True})
        if isinstance(value, Exception):
            raise value
        return value


def run(coro):
    return asyncio.run(coro)


def make(results=None):
    inner = FakeInner(results)
    return inner, HookedToolbox(inner, Hooks(secret=SECRET))


def test_an_allowed_call_runs_and_is_logged():
    inner, tb = make({"telemetry.query_promql": {"series": []}})

    async def go():
        with use_role("triage"):
            return await tb.call("telemetry.query_promql", {"query": "up"})

    assert run(go()) == {"series": []}
    assert inner.calls == [("telemetry.query_promql", {"query": "up"})]
    entry = tb.hooks.tool_calls()[0]
    assert entry["agent_role"] == "triage" and entry["ok"] and not entry["blocked"]


def test_a_call_with_no_role_is_blocked_and_never_runs():
    inner, tb = make()
    with pytest.raises(ToolError) as exc:
        run(tb.call("telemetry.query_promql", {"query": "up"}))
    assert exc.value.code == "blocked:unknown_role" and inner.calls == []


def test_a_tool_outside_the_roles_permissions_is_blocked():
    inner, tb = make()

    async def go():
        with use_role("triage"):
            return await tb.call("ops_actions.restart_service", {"service": "cart"})

    with pytest.raises(ToolError) as exc:
        run(go())
    assert exc.value.code == "blocked:permission_denied" and inner.calls == []


def test_a_gated_write_without_an_approval_token_is_blocked():
    inner, tb = make()

    async def go():
        with use_role("remediation"):
            return await tb.call("ops_actions.restart_service", {"service": "cart"})

    with pytest.raises(ToolError) as exc:
        run(go())
    assert exc.value.code.startswith("blocked:approval") and inner.calls == []


def test_a_tool_error_keeps_its_own_code():
    inner, tb = make({"tickets.get_incident": ToolError("not_found", "no incident 'x'")})

    async def go():
        with use_role("triage"):
            return await tb.call("tickets.get_incident", {"incident_id": "x"})

    with pytest.raises(ToolError) as exc:
        run(go())
    assert exc.value.code == "not_found"
    assert tb.hooks.tool_calls()[0]["ok"] is False


def test_secrets_in_a_result_are_redacted_before_the_graph_sees_them():
    inner, tb = make({"tickets.get_incident": {"incident_id": "i", "note": "key api_key=sk-abc123def456ghi789"}})

    async def go():
        with use_role("triage"):
            return await tb.call("tickets.get_incident", {"incident_id": "i"})

    assert "sk-abc123def456ghi789" not in json.dumps(run(go()))


def test_with_role_sets_and_then_clears_the_role():
    inner, tb = make()

    @with_role("triage")
    async def node():
        return await tb.call("telemetry.recent_changes", {})

    assert run(node()) == {"done": True}
    with pytest.raises(ToolError):                 # outside the node there is no role again
        run(tb.call("telemetry.recent_changes", {}))


def test_other_attributes_come_from_the_inner_toolbox():
    inner, tb = make()
    assert tb.tools == {"x": 1}


def test_the_graphs_ticket_client_works_through_the_hooks():
    pytest.importorskip("langchain_core")
    pytest.importorskip("langchain_mcp_adapters")
    from datetime import datetime, timezone

    from copilot.state import Alert, Severity
    from copilot.tools import McpTickets
    from mcp_servers.tickets.server import Tickets

    tickets = Tickets(":memory:", secret=SECRET)

    class Inner(FakeInner):
        async def call(self, qualified, args):
            from copilot.tools import _parse
            from mcp_servers.common.models import check_output
            _, tool = qualified.split(".", 1)
            return _parse(json.dumps(check_output("tickets", tool, getattr(tickets, tool)(**args))))

    tb = HookedToolbox(Inner(), Hooks(secret=SECRET))
    alert = Alert(alert_name="CheckoutErrorRateHigh", service="checkout", signal="error_rate", summary="s", query="q",
                  threshold=0.05, value=0.4, fired_at=datetime.now(timezone.utc), fingerprint="f")

    async def go():
        with use_role("triage"):
            await McpTickets(tb).create_incident("INC-1", alert, Severity.SEV2)

    run(go())
    assert tickets.get_incident("INC-1")["result"]["alert_name"] == "CheckoutErrorRateHigh"
    assert tb.hooks.tool_calls()[0]["tool"] == "create_incident"


# ------------------------------------------------------------------ the graph's nodes and their roles

# Which MCP tools each node calls through the Mcp* clients (copilot/tools.py), and the role it runs as.
NODE_CALLS = {
    ("agents/triage.py", "triage"): ["telemetry.query_promql", "telemetry.recent_changes", "tickets.create_incident"],
    ("agents/runbook.py", "runbook"): ["runbooks_kb.search"],
    ("agents/remediation.py", "remediation"): ["ops_actions.dry_run", "ops_actions.set_flag", "ops_actions.restart_service",
                                              "ops_actions.scale_service", "ops_actions.rollback", "telemetry.query_promql"],
    ("agents/comms.py", "comms"): ["tickets.save_postmortem"],
}


def test_every_tool_a_node_calls_is_allowed_for_its_role():
    hooks = Hooks(secret=SECRET)
    for (_, role), tools in NODE_CALLS.items():
        for key in tools:
            assert hooks.allowed(role, key), "%s may not call %s" % (role, key)


def test_the_nodes_that_call_tools_carry_their_role():
    import pathlib
    import re
    agents = pathlib.Path(__file__).resolve().parents[2] / "copilot"
    expected = {"triage": "triage", "runbook": "runbook", "dry_run": "remediation", "execute": "remediation",
                "verify": "remediation", "comms": "comms"}
    for path in ("agents/triage.py", "agents/runbook.py", "agents/remediation.py", "agents/comms.py"):
        source = (agents / path).read_text(encoding="utf-8")
        for node, role in expected.items():
            match = re.search(r"((?:    @[^\n]+\n)*)    async def %s\(" % node, source)
            if match:
                assert '@with_role("%s")' % role in match.group(1), "%s in %s has no role" % (node, path)
