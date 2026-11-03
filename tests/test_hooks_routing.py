"""Every tool call goes through the incident's Hooks: code-driven (Mcp* clients) and model-driven (wrapped tools)."""
import asyncio
import json

import pytest

from copilot import toolctx
from copilot.clients import ToolError
from copilot.graph import calls_as
from copilot.guardrails import GuardConfig
from copilot.hooks import Hooks, load_permissions
from copilot.config import ROOT
from tests.fake_shop import Shop

PERMS = load_permissions(str(ROOT / "config" / "permissions.yaml"))


def hooks(**kw):
    return Hooks(permissions=PERMS, max_tool_calls=kw.pop("max_tool_calls", 20), config=kw.pop("config", GuardConfig()),
                 **kw)


def tool(box, agent, name):
    return next(t for t in box.for_agent(agent) if t.name == name)


def call_model_tool(box, h, role, agent_tools, name, **args):
    async def go():
        with toolctx.scope(hooks=h, agent=role, role=role):
            return json.loads(await tool(box, agent_tools, name).ainvoke(args))
    return asyncio.run(go())


def call_code(box, h, qualified, args, **ctx):
    async def go():
        with toolctx.scope(hooks=h, **ctx):
            return await box.call(qualified, args)
    return asyncio.run(go())


def test_models_get_only_read_tools_and_wildcards_work():
    box = Shop().toolbox()
    assert sorted(t.name for t in box.for_agent("diagnosis")) == \
        ["get_traces", "query_promql", "recent_changes", "search_logs"]
    single = {t.name for t in box.for_agent("single_agent")}            # "telemetry.*", "ops_actions.*", ...
    assert {"query_promql", "search", "get", "dry_run", "get_incident"} <= single
    assert not single & {"set_flag", "restart_service", "create_incident", "save_postmortem", "publish_status"}
    assert box.for_agent("commander") == []


def test_model_calls_are_logged_with_role_and_driver():
    box, h = Shop().toolbox(), hooks()
    out = call_model_tool(box, h, "diagnosis", "diagnosis", "query_promql", query="up")
    assert out["ok"] and out["result"]["evidence_id"] == "promql:up"
    entry = h.tool_calls()[0]
    assert (entry["agent_role"], entry["server"], entry["tool"], entry["driver"]) == \
        ("diagnosis", "telemetry", "query_promql", "model")


def test_permissions_block_unless_defences_are_off():
    box = Shop().toolbox()
    on = hooks()
    out = call_model_tool(box, on, "triage", "diagnosis", "search_logs", service="payment")
    assert out["ok"] is False and out["error"]["code"] == "blocked"
    assert on.tool_calls()[0]["block_reason"] == "permission_denied"
    off = hooks(config=GuardConfig.all_off())                            # E2 defences off: same call goes through
    assert call_model_tool(box, off, "triage", "diagnosis", "search_logs", service="payment")["ok"]


def test_budget_counts_model_calls_only():
    box, h = Shop().toolbox(), hooks(max_tool_calls=2)
    for q in ("a", "b"):
        assert call_model_tool(box, h, "diagnosis", "diagnosis", "query_promql", query=q)["ok"]
    third = call_model_tool(box, h, "diagnosis", "diagnosis", "query_promql", query="c")
    assert third["ok"] is False and h.tool_calls()[-1]["block_reason"] == "budget_exceeded"
    # verify keeps polling after the agent's budget is spent: code-driven calls are not budgeted
    assert call_code(box, h, "telemetry.query_promql", {"query": "d"}, role="remediation", driver=toolctx.CODE)


def test_loop_check_applies_to_model_calls_not_to_polling():
    box, h = Shop().toolbox(), hooks()
    results = [call_model_tool(box, h, "diagnosis", "diagnosis", "query_promql", query="same")["ok"] for _ in range(3)]
    assert results == [True, True, False] and h.tool_calls()[-1]["block_reason"] == "loop_detected"
    h2 = hooks()
    for _ in range(4):        # verify asks the same question every poll
        call_code(box, h2, "telemetry.query_promql", {"query": "same"}, role="remediation", driver=toolctx.CODE)
    assert not any(e["blocked"] for e in h2.tool_calls())


def test_a_write_without_a_token_is_blocked_before_the_shop():
    shop = Shop()
    box, h = shop.toolbox(), hooks()
    with pytest.raises(ToolError) as exc:
        call_code(box, h, "ops_actions.set_flag", {"flag": "paymentFailure", "variant": "off"}, role="remediation")
    assert exc.value.code == "blocked" and shop.writes == []
    assert h.tool_calls()[0]["block_reason"] == "approval_required"


def test_a_tool_exception_is_logged_and_raised_as_a_tool_error():
    box, h = Shop().toolbox(), hooks()
    box.tools["telemetry.query_promql"].handler = lambda a: 1 / 0
    with pytest.raises(ToolError) as exc:
        call_code(box, h, "telemetry.query_promql", {"query": "x"}, role="triage")
    assert exc.value.code == "tool_exception" and h.tool_calls()[0]["ok"] is False


def test_without_hooks_calls_go_straight_to_the_tool():
    box = Shop().toolbox()
    assert asyncio.run(box.call("telemetry.query_promql", {"query": "x"}))["evidence_id"] == "promql:x"


@pytest.mark.parametrize("node,role,state,expect", [
    ("verify", "remediation", {}, {"driver": "code", "agent": "remediation"}),
    ("execute", "remediation", {"action_purpose": "rollback"}, {"purpose": "rollback", "driver": "model"}),
    ("dry_run", "remediation", {}, {"purpose": "fix"}),
    ("triage", "fixed_workflow", {}, {"role": "fixed_workflow", "purpose": None}),
])
def test_nodes_tag_their_calls(node, role, state, expect):
    seen = {}

    async def fn(st):
        ctx = toolctx.current()
        seen.update(agent=ctx.agent, role=ctx.role, driver=ctx.driver, purpose=ctx.purpose)
        return {}
    asyncio.run(calls_as(node, role, fn)(state))
    assert {k: seen[k] for k in expect} == expect
    assert toolctx.current().role == "unknown"                           # the scope ends with the node


def test_diagnosis_branches_get_their_own_agent_name():
    from types import SimpleNamespace
    seen = {}

    async def fn(inp):
        seen["agent"] = toolctx.current().agent
        return {}
    hyp = SimpleNamespace(service="payment", fault_type="paymentFailure")
    asyncio.run(calls_as("diagnosis", "diagnosis", fn)({"hypothesis": hyp}))
    assert seen["agent"] == "diagnosis-payment-paymentFailure"
