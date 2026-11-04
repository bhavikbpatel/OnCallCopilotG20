"""run_incident end to end on the fake shop: C0 / C1 / C2 selection, the hooks log, approvals, arm options."""
import asyncio
from datetime import datetime, timezone

import pytest
from langchain_core.messages import AIMessage

from copilot import run as run_mod
from copilot.agents import comms, remediation, triage
from copilot.agents.escalate import escalation_cause
from copilot.config import load_settings
from copilot.run import (
    CopilotAgent, apply_options, fixed_at, guard_config, memory_mode, memory_on, options_problem, run_incident,
)
from copilot.state import BudgetLimits, TimelineEvent
from eval.adapters.copilot_state import to_result_line
from eval.runner.agents import AgentContext, not_ready
from eval.runner.approvals import AutoApprover, with_gate_tokens
from eval.schemas.result import ToolCall
from tests.fake_shop import ScriptedChat, Shop, alert, fake_structured_call

SECRET = "test-secret-0123456789abcdef"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr(remediation.asyncio, "sleep", instant)          # verify's settle / poll waits
    monkeypatch.setattr(triage, "structured_call", fake_structured_call)
    monkeypatch.setattr(comms, "structured_call", fake_structured_call)
    monkeypatch.setenv("APPROVAL_SECRET", SECRET)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.setattr(run_mod, "load_dotenv", lambda *a, **k: None)


def settings():
    return load_settings().model_copy(update={"verify_window_s": 60, "verify_window_by_signal": {}})


def run(shop, options, policy="approve", limits=None, llm=None):
    approver = AutoApprover(policy)
    result = asyncio.run(run_incident(alert(), approver, run_id="run-1", limits=limits or BudgetLimits(tool_calls=20),
                                      settings=settings(), options=options, toolbox=shop.toolbox(),
                                      llm_factory=(lambda agent: llm) if llm else None))
    return result, approver


def calls(result, tool=None, **match):
    return [c for c in result.tool_calls if (tool is None or c["tool"] == tool)
            and all(c.get(k) == v for k, v in match.items())]


# ---------- C0: fixed workflow ----------

def test_c0_follows_the_first_actionable_runbook_and_recovers():
    shop = Shop()
    result, approver = run(shop, {"architecture": "C0", "memory": "off", "review_enabled": False})
    st = result.final_state
    assert st["final_status"] == "recovered" and shop.writes == [("set_flag", {"flag": "paymentFailure", "variant": "off"})]
    assert st["runbook_advice"].runbook_id == "RB-001"                 # RB-014 (no fix) and PM-001 were skipped
    assert {c["agent_role"] for c in result.tool_calls} == {"fixed_workflow"}
    for c in result.tool_calls:
        ToolCall.model_validate(c)                                     # Person 4's schema takes the hooks log as is
    write = calls(result, "set_flag")[0]
    assert write["ok"] and write["purpose"] == "fix" and write["kind"] == "write" and write["payload_hash"]
    assert "approval_token" not in write["args"]                       # the token is never logged
    assert calls(result, "dry_run")[0]["purpose"] == "fix"
    assert calls(result, "query_promql", driver="code")                 # verify polls are code-driven …
    assert result.budget["tool_calls"] < len(result.tool_calls)         # … and not budgeted
    assert result.fixed_at and not result.budget_exhausted
    assert with_gate_tokens(approver.approvals, st)[-1]["has_token"] is True


def test_c0_rolls_back_and_escalates_when_the_fix_does_not_help():
    shop = Shop(recovers=False)
    result, _ = run(shop, {"architecture": "C0"})
    st = result.final_state
    assert [w[1]["variant"] for w in shop.writes] == ["off", "50%"]   # fix, then its undo
    assert calls(result, "set_flag", purpose="rollback")
    assert st["final_status"] == "escalated" and "does not replan" in st["escalation_reason"]


def test_c0_rejection_ends_in_escalation_without_a_write():
    shop = Shop()
    result, _ = run(shop, {"architecture": "C0"}, policy="reject")
    assert shop.writes == [] and result.final_state["final_status"] == "escalated"
    assert result.fixed_at is None


# ---------- C1: single agent ----------

def scripted(evidence_id):
    from copilot.agents.baselines import SingleAgentResult
    from copilot.state import EvidenceRef, SetFlag
    return ScriptedChat(
        script=[AIMessage(content="", tool_calls=[{"name": "query_promql", "args": {"query": "payment_errors"},
                                                    "id": "c1"}])],
        structured=SingleAgentResult(service="payment", fault_type="paymentFailure",
                                     evidence=[EvidenceRef(evidence_id=evidence_id, summary="payment errors up")],
                                     action=SetFlag(flag="paymentFailure", variant="off"),
                                     expected_effect="errors stop", rationale="flag at 50%"))


def test_c1_single_agent_investigates_through_the_hooks_and_recovers():
    shop = Shop()
    result, _ = run(shop, {"architecture": "C1"}, llm=scripted("promql:payment_errors"))
    st = result.final_state
    assert st["final_status"] == "recovered" and shop.writes[0][0] == "set_flag"
    model = [c for c in calls(result, "query_promql", driver="model") if c["args"] == {"query": "payment_errors"}]
    assert len(model) == 1 and model[0]["agent_role"] == "single_agent" and model[0]["ok"]
    hyp = st["hypotheses"][-1]
    assert hyp.status == "supported" and hyp.evidence[0].evidence_id == "promql:payment_errors"


def test_c1_takes_no_action_on_evidence_no_tool_returned():
    shop = Shop()
    result, _ = run(shop, {"architecture": "C1"}, llm=scripted("promql:made-up"))
    assert shop.writes == [] and result.final_state["final_status"] == "escalated"
    assert "no supported root cause" in result.final_state["escalation_reason"]


# ---------- C2: the team ----------

def test_c2_team_runs_every_role_through_the_hooks(monkeypatch):
    from copilot.agents import commander, runbook
    from copilot.state import EvidenceRef, HypothesisResult
    for mod in (commander, runbook, remediation):
        monkeypatch.setattr(mod, "structured_call", fake_structured_call)
    llm = ScriptedChat(
        script=[AIMessage(content="", tool_calls=[{"name": "query_promql", "args": {"query": "payment_errors"},
                                                    "id": "d1"}])],
        structured=HypothesisResult(hypothesis_id="h", verdict="supported", confidence_basis="errors follow the flag",
                                    evidence=[EvidenceRef(evidence_id="promql:payment_errors", summary="up")]))
    shop = Shop()
    result, _ = run(shop, {"architecture": "C2", "memory": "off", "review_enabled": False}, llm=llm)
    assert result.final_state["final_status"] == "recovered" and shop.writes[0][0] == "set_flag"
    roles = {c["agent_role"] for c in result.tool_calls}
    assert {"triage", "diagnosis", "remediation", "comms"} <= roles and "fixed_workflow" not in roles
    diag = [c for c in result.tool_calls if c["agent_role"] == "diagnosis"]
    assert diag[0]["agent"] == "diagnosis-payment-paymentFailure" and diag[0]["driver"] == "model"
    assert not any(c["blocked"] for c in result.tool_calls)               # permissions.yaml covers every role


def test_c2_still_fixes_with_e4s_smallest_budget(monkeypatch):
    """E4 budget 5: only calls a model chose count. Triage's queries, runbook search, tickets, the dry run and the
    approved write are code-driven, so they neither use up the agents' budget nor get blocked by it."""
    from copilot.agents import commander, runbook
    from copilot.state import EvidenceRef, HypothesisResult
    for mod in (commander, runbook, remediation):
        monkeypatch.setattr(mod, "structured_call", fake_structured_call)
    llm = ScriptedChat(
        script=[AIMessage(content="", tool_calls=[{"name": "query_promql", "args": {"query": "payment_errors"},
                                                    "id": "d1"}])],
        structured=HypothesisResult(hypothesis_id="h", verdict="supported", confidence_basis="errors follow the flag",
                                    evidence=[EvidenceRef(evidence_id="promql:payment_errors", summary="up")]))
    shop = Shop()
    result, _ = run(shop, {"architecture": "C2", "memory": "off", "review_enabled": False}, llm=llm,
                    limits=BudgetLimits(tool_calls=5))
    assert result.final_state["final_status"] == "recovered"
    assert shop.writes == [("set_flag", {"flag": "paymentFailure", "variant": "off"})]
    assert not any(c["blocked"] for c in result.tool_calls) and not result.budget_exhausted
    model = calls(result, driver="model")
    assert model and all(c["agent_role"] == "diagnosis" for c in model)  # only the diagnosis agent's own calls …
    assert result.budget["tool_calls"] == len(model)                    # … are what the budget counts
    assert calls(result, "set_flag", driver="code") and calls(result, "dry_run", driver="code")
    ledger = result.final_state["budget"].total
    assert ledger.tool_calls == len(model) and ledger.code_calls > 0      # the graph's own ledger agrees


def test_c0_makes_no_model_calls_so_uses_no_budget():
    shop = Shop()
    result, _ = run(shop, {"architecture": "C0", "memory": "off", "review_enabled": False},
                    limits=BudgetLimits(tool_calls=1))
    assert result.final_state["final_status"] == "recovered" and not result.budget_exhausted
    assert result.budget["tool_calls"] == 0 and not any(c["blocked"] for c in result.tool_calls)


# ---------- runner integration ----------

def test_copilot_agent_runs_under_the_runner_and_its_result_line_scores(monkeypatch):
    shop = Shop()

    async def connect(*a, **k):
        return shop.toolbox()
    monkeypatch.setattr(run_mod.McpToolbox, "connect", connect)
    agent = CopilotAgent(settings())
    opts = {"architecture": "C0", "memory": "off", "defences": "on", "review_enabled": False}
    assert not_ready(agent, options=opts) is None
    ctx = AgentContext(run_id="E1-C0-D-001-r1", alert=alert(), approve=AutoApprover("approve"),
                       deadline=0, max_tool_calls=20, clock=lambda: 0.0, options=opts)
    res = agent.run(ctx)
    now = datetime.now(timezone.utc)
    line = to_result_line(res.state, case_id="D-001", config_id="E1-C0", started_at=now, ended_at=now,
                          tool_calls=res.tool_calls, tool_log_source="agent", max_tool_calls=20)
    assert line.tool_log_source == "agent" and len(line.tool_calls) == len(res.tool_calls)
    assert res.fixed_at is not None


# ---------- arm options ----------

def test_arm_options_are_validated():
    s, limits = load_settings(), BudgetLimits(tool_calls=5)
    assert apply_options(s, limits, {"architecture": "C1", "review_enabled": False}).architecture == "C1"
    assert apply_options(s, limits, {}).limits.tool_calls == 5
    for bad in ({"architecture": "C3"}, {"temperature": 1}, {"review_enabled": "maybe"}):
        with pytest.raises(ValueError):
            apply_options(s, limits, bad)
    assert memory_on(None) and memory_on("playbook") and memory_on("on") and not memory_on("off")
    assert guard_config("off").permissions is False and guard_config("on").injection_filter is True
    with pytest.raises(ValueError):
        memory_on("sometimes")


def test_playbook_is_read_only_and_needs_postgres():
    s, limits = load_settings(), BudgetLimits(tool_calls=20)
    assert [memory_mode(m) for m in (None, "on", "off", "playbook", "PLAYBOOK")] == ["on", "on", "off", "playbook", "playbook"]
    assert apply_options(s, limits, {"memory": "playbook"}).memory_read_only is True      # frozen: never written to
    assert apply_options(s, limits, {"memory": "on"}).memory_read_only is s.memory_read_only
    assert "POSTGRES_DSN" in options_problem({"memory": "playbook"}, env={})
    assert options_problem({"memory": "playbook"}, env={"POSTGRES_DSN": "postgresql://x"}) is None
    assert options_problem({"memory": "off"}, env={}) is None and options_problem(None, env={}) is None


def test_playbook_without_postgres_refuses_to_run_rather_than_run_with_empty_memory():
    shop = Shop()
    with pytest.raises(ValueError, match="POSTGRES_DSN"):
        run(shop, {"architecture": "C2", "memory": "playbook", "review_enabled": False})
    assert shop.writes == []


def test_runner_preflight_stops_a_playbook_arm_without_postgres(monkeypatch):
    agent = CopilotAgent(settings())
    monkeypatch.setattr(run_mod, "ROOT", run_mod.ROOT / "no-such-dir")     # no .env with a DSN either
    why = not_ready(agent, options={"architecture": "C2", "memory": "playbook"})
    assert why and "POSTGRES_DSN" in why
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://x")
    assert not_ready(agent, options={"architecture": "C2", "memory": "playbook"}) is None


def test_agent_version_changes_with_prompts_and_permissions(tmp_path):
    from copilot.config import config_hash
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "triage.yaml").write_text("role: a\n")
    perms = tmp_path / "permissions.yaml"
    perms.write_text("triage: []\n")
    s = load_settings().model_copy(update={"prompts_dir": prompts, "permissions_path": perms})
    first = config_hash(b"app", s)
    assert config_hash(b"app", s) == first                               # stable
    (prompts / "triage.yaml").write_text("role: b\n")
    second = config_hash(b"app", s)
    perms.write_text("triage: [telemetry.query_promql]\n")
    assert len({first, second, config_hash(b"app", s), config_hash(b"app2", s)}) == 4


def test_fixed_at_is_the_last_executed_fix_not_the_rollback():
    t = datetime(2026, 11, 3, tzinfo=timezone.utc)
    ev = lambda sec, kind: TimelineEvent(at=t.replace(second=sec), node="execute", kind=kind, text="x")  # noqa: E731
    assert fixed_at({"timeline": [ev(1, "action"), ev(5, "action"), ev(9, "rollback")]}) == t.replace(second=5).timestamp()
    assert fixed_at({"timeline": []}) is None


def test_escalation_reason_for_a_workflow_that_does_not_replan():
    st = {"limits": BudgetLimits(), "replan_reason": "fix did not recover the metric; rolled back"}
    assert escalation_cause(st, max_replans=2).endswith("this workflow does not replan")
