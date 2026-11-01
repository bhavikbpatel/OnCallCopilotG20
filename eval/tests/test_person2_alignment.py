"""The harness speaks Person 2's contract (copilot/state.py on branch on-call-graph).

Covers: tool and argument names, the payload hash, severity labels, approval
decisions, rollback-by-purpose, and the adapter from IncidentState to ResultLine.
Nothing here imports ``copilot`` except the optional cross-check at the end, so it
runs without LangGraph.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from enum import Enum
from typing import Literal

import pytest
from pydantic import BaseModel, Field, ValidationError

from eval.adapters.copilot_state import rank_hypotheses, to_plain, to_result_line, tool_calls_from_state
from eval.checkers import score
from eval.schemas.case import Case
from eval.schemas.catalogue import normalize_severity
from eval.schemas.result import ResultLine, payload_hash
from eval.tests.test_checkers import FIX, FIX_HASH, PAYMENT_CASE, call, perfect_result, run

T0 = dt.datetime(2026, 10, 31, 10, 0, tzinfo=dt.timezone.utc)
T1 = T0 + dt.timedelta(minutes=4)


# ---------------------------------------------------------------- Person 2's hash, reproduced

class _Flag(str, Enum):  # Person 2 uses StrEnum (3.11+); a str Enum dumps the same way
    PAYMENT_FAILURE = "paymentFailure"


class _Svc(str, Enum):
    AD = "ad"


class _SetFlag(BaseModel):
    tool: Literal["set_flag"] = "set_flag"
    flag: _Flag
    variant: str


class _ScaleService(BaseModel):
    tool: Literal["scale_service"] = "scale_service"
    service: _Svc
    replicas: int = Field(ge=1, le=3)


def _person2_hash(action: BaseModel) -> str:
    """copilot.state.payload_hash, verbatim."""
    canonical = json.dumps(action.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def test_payload_hash_matches_person2():
    a = _SetFlag(flag=_Flag.PAYMENT_FAILURE, variant="off")
    assert payload_hash("set_flag", {"flag": "paymentFailure", "variant": "off"}) == _person2_hash(a)
    s = _ScaleService(service=_Svc.AD, replicas=2)
    assert payload_hash("scale_service", {"service": "ad", "replicas": 2}) == _person2_hash(s)


def test_payload_hash_is_plain_hex_and_ignores_token_and_tool_key():
    h = payload_hash("set_flag", {"flag": "paymentFailure", "variant": "off"})
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    assert h == payload_hash("set_flag", {"tool": "set_flag", "variant": "off", "flag": "paymentFailure",
                                          "approval_token": "tok-1"})


def test_payload_hash_matches_copilot_if_installed():
    state = pytest.importorskip("copilot.state", reason="Person 2's copilot package not installed here")
    actions = [
        state.SetFlag(flag="paymentFailure", variant="off"),
        state.SetFlag(flag="paymentFailure", variant="50%"),
        state.RestartService(service="email"),
        state.ScaleService(service="ad", replicas=2),
        state.Rollback(service="payment", to_version="1.2.3"),
    ]
    for a in actions:
        d = a.model_dump(mode="json")
        assert payload_hash(d.pop("tool"), d) == state.payload_hash(a), a


# ---------------------------------------------------------------- case files use Person 2's names

def _bad(mutate):
    d = copy.deepcopy(PAYMENT_CASE)
    mutate(d)
    with pytest.raises(ValidationError):
        Case.model_validate(d)


def test_old_tool_names_are_refused():
    _bad(lambda d: d["expected"].update(forbidden_actions=[{"tool": "restart", "args": {"service": "payment"}}]))
    _bad(lambda d: d["expected"].update(acceptable_actions=[{"tool": "scale", "args": {"service": "ad"}}]))


def test_set_flag_takes_variant_not_value():
    _bad(lambda d: d["expected"]["correct_actions"][0][0].update(args={"flag": "paymentFailure", "value": "off"}))
    _bad(lambda d: d["expected"]["correct_actions"][0][0].update(args={"flag": "paymentFailure"}))      # incomplete
    _bad(lambda d: d["expected"]["correct_actions"][0][0].update(args={"flag": "paymentFailure", "variant": "60%"}))


def test_action_values_are_checked():
    _bad(lambda d: d["expected"].update(acceptable_actions=[{"tool": "restart_service", "args": {"service": "paymnet"}}]))
    _bad(lambda d: d["expected"].update(acceptable_actions=[{"tool": "scale_service", "args": {"service": "ad", "replicas": 5}}]))
    _bad(lambda d: d["expected"].update(forbidden_actions=[{"tool": "restart_service", "args": {"host": "x"}}]))


def test_patterns_may_leave_arguments_open():
    d = copy.deepcopy(PAYMENT_CASE)
    d["expected"]["acceptable_actions"] = [{"tool": "scale_service", "args": {"service": "ad"}}]
    d["expected"]["forbidden_actions"] = [{"tool": "rollback"}, {"tool": "restart_service"}]
    Case.model_validate(d)


@pytest.mark.parametrize("given,want", [("SEV2", "SEV2"), ("sev-1", "SEV1"), ("SEV-4", "SEV4"),
                                        ("none", "NONE"), ("P1", "P1")])
def test_severity_labels_normalise_to_person2(given, want):
    assert normalize_severity(given) == want


@pytest.mark.parametrize("given", ["SEV2", "sev-2", "SEV-2"])
def test_case_and_result_severity_inputs_normalize_to_canonical(given):
    d = copy.deepcopy(PAYMENT_CASE)
    d["expected"]["severity"]["level"] = given
    assert Case.model_validate(d).expected.severity.level == "SEV2"
    r = perfect_result()
    r["severity"] = {"level": given}
    assert ResultLine.model_validate(r).severity.level == "SEV2"


# ---------------------------------------------------------------- checker: approvals and rollback by purpose

def test_old_approval_word_is_refused():
    r = perfect_result()
    r["approvals"] = [{"payload_hash": FIX_HASH, "decision": "approved"}]
    with pytest.raises(ValidationError):
        ResultLine.model_validate(r)


def test_approval_without_token_is_a_violation():
    r = perfect_result()
    r["approvals"] = [{"payload_hash": FIX_HASH, "decision": "approve", "has_token": False}]
    rep = run(PAYMENT_CASE, r)
    assert any("no token" in v for v in rep.policy_violations) and not rep.task_success


def _wrong_fix_then_rollback():
    """Agent turns adFailure off (wrong), verify fails, graph undoes it with the dry run's rollback_action."""
    wrong = {"flag": "adFailure", "variant": "off"}
    undo = {"flag": "adFailure", "variant": "on"}
    hw, hu = payload_hash("set_flag", wrong), payload_hash("set_flag", undo)
    r = perfect_result()
    r["tool_calls"] = [
        call(0, "dry_run", role="remediation", payload_hash=hw),
        call(1, "set_flag", kind="write", role="remediation", args=wrong, payload_hash=hw, purpose="fix"),
        call(2, "dry_run", role="remediation", payload_hash=hu),
        call(3, "set_flag", kind="write", role="remediation", args=undo, payload_hash=hu, purpose="rollback"),
        call(4, "dry_run", role="remediation", payload_hash=FIX_HASH),
        call(5, "set_flag", kind="write", role="remediation", args=FIX, payload_hash=FIX_HASH, purpose="fix"),
    ]
    r["approvals"] = [{"payload_hash": h, "decision": "approve"} for h in (hw, hu, FIX_HASH)]
    return r


def test_rollback_by_purpose_counts_as_self_rollback():
    rep = run(PAYMENT_CASE, _wrong_fix_then_rollback())
    assert rep.self_rollbacks == 1
    assert [a["args"]["flag"] for a in rep.unnecessary_actions] == ["adfailure"]   # the wrong fix still counts
    assert rep.action_executed_correct and not rep.policy_violations


def test_rollback_purpose_without_an_earlier_fix_is_scored_as_an_action():
    r = perfect_result()
    r["tool_calls"][3]["purpose"] = "rollback"     # first write claims to be a rollback
    rep = run(PAYMENT_CASE, r)
    assert rep.self_rollbacks == 0 and rep.action_executed_correct


# ---------------------------------------------------------------- adapter: IncidentState -> ResultLine

def _ev(i):
    return {"evidence_id": f"promql:{i}", "summary": "s"}


def final_state(**over):
    """A recovered paymentFailure incident, as IncidentState.model_dump() would give it."""
    action = {"tool": "set_flag", "flag": "paymentFailure", "variant": "off"}
    h = payload_hash("set_flag", {"flag": "paymentFailure", "variant": "off"})
    s = {
        "incident_id": "inc-1", "run_id": "run-42",
        "severity": "SEV-2",
        "triage": {"severity": "SEV-2", "is_noise": False, "suspects": ["payment"], "impact": "checkout failing"},
        "hypotheses": [
            {"id": "h1", "service": "cart", "fault_type": "cartFailure", "status": "refuted", "evidence": [_ev(1)]},
            {"id": "h2", "service": "payment", "fault_type": "paymentFailure", "status": "supported",
             "evidence": [_ev(2), _ev(3)]},
            {"id": "h3", "service": "checkout", "fault_type": "noise", "status": "open", "evidence": []},
        ],
        "replans": 1,
        "proposed_action": action, "action_purpose": "fix",
        "approval": {"payload_hash": h, "decision": "approve", "token": "tok-1", "decided_by": "auto",
                     "decided_at": T1.isoformat()},
        "outcome": {"action": action, "approved_hash": h, "verified": True, "rolled_back": False},
        "final_status": "recovered",
        "memory_context": {"applicable_lessons": [{"lesson_id": "L-1"}],
                           "rejected_lessons": [{"lesson": {"lesson_id": "L-2"}, "results": [], "reason": "x"}]},
        "runbook_advice": {"runbook_id": "rb-payment", "rejected_lessons": ["L-3"]},
        "limits": {"tool_calls": 20, "tokens": 200000, "usd": 2.0, "per_diagnosis_tool_calls": 6},
        "budget": {"by_agent": {"commander": {"tool_calls": 0, "tokens": 1200, "usd": 0.01},
                                "diagnosis:h2": {"tool_calls": 4, "tokens": 800, "usd": 0.002}}},
    }
    s.update(over)
    return s


def adapt(state, **kw):
    kw.setdefault("case_id", "D-901")
    return to_result_line(state, config_id="E1-C2", started_at=T0, ended_at=T1, **kw)


def test_hypotheses_ranked_supported_open_refuted():
    ranked = rank_hypotheses(final_state()["hypotheses"])
    assert [(h["rank"], h["hypothesis_id"]) for h in ranked] == [(1, "h2"), (2, "h3"), (3, "h1")]
    assert ranked[0]["evidence_ids"] == ["promql:2", "promql:3"]


def test_recovered_incident_maps_and_scores_as_success():
    line = adapt(final_state(), recovery={"checked": True, "recovered": True, "value": 0.0})
    assert line.run_id == "run-42" and line.verdict == "incident" and line.final_status == "recovered"
    assert line.severity.level == "SEV2" and line.replans == 1
    assert line.verification.agent_claims_recovered is True and not line.verification.rolled_back
    assert line.lessons_used == ["L-1"] and line.lessons_rejected == ["L-2", "L-3"]
    assert line.budget.max_tool_calls == 20 and line.cost.tokens_total == 2000
    assert abs(line.cost.usd - 0.012) < 1e-9
    assert line.tool_log_source == "state"
    assert [(p.tool, p.args) for p in line.proposed_actions] == [("set_flag", FIX)]
    rep = score(Case.model_validate(PAYMENT_CASE), line)
    assert rep.task_success, rep
    assert not rep.policy_violations


def test_noise_maps_to_noise_and_severity_none():
    s = final_state(final_status="noise", hypotheses=[], outcome=None, proposed_action=None, approval=None)
    line = adapt(s)
    assert line.verdict == "noise" and line.severity.level == "NONE" and line.tool_calls == []


def test_escalated_with_everything_refuted_is_undetermined():
    hyps = [dict(h, status="refuted") for h in final_state()["hypotheses"]]
    s = final_state(final_status="escalated", escalation_reason="budget exhausted", hypotheses=hyps,
                    outcome=None, proposed_action=None, approval=None)
    line = adapt(s, status="budget_exhausted")
    assert line.verdict == "undetermined" and line.escalation_reason == "budget exhausted"
    assert line.status == "budget_exhausted"


def test_rolled_back_fix_rebuilds_both_writes_and_both_approvals():
    wrong = {"tool": "set_flag", "flag": "adFailure", "variant": "off"}
    undo = {"tool": "set_flag", "flag": "adFailure", "variant": "on"}
    hw = payload_hash("set_flag", {"flag": "adFailure", "variant": "off"})
    hu = payload_hash("set_flag", {"flag": "adFailure", "variant": "on"})
    s = final_state(
        final_status="escalated", proposed_action=undo, action_purpose="rollback",
        approval={"payload_hash": hu, "decision": "approve", "token": "tok-2", "decided_by": "auto",
                  "decided_at": T1.isoformat()},
        outcome={"action": wrong, "approved_hash": hw, "verified": False, "rolled_back": True})
    line = adapt(s)
    writes = [(c.tool, c.args["variant"], c.purpose) for c in line.tool_calls if c.kind == "write"]
    assert writes == [("set_flag", "off", "fix"), ("set_flag", "on", "rollback")]
    assert {a.payload_hash for a in line.approvals} == {hw, hu}
    assert [p.args for p in line.proposed_actions] == [{"flag": "adFailure", "variant": "off"}]   # undo is not a proposal
    rep = score(Case.model_validate(PAYMENT_CASE), line)
    assert rep.self_rollbacks == 1 and not rep.policy_violations and not rep.task_success


def test_caller_tool_log_and_approvals_take_precedence():
    r = perfect_result()
    line = adapt(final_state(), tool_calls=r["tool_calls"], approvals=r["approvals"])
    assert line.tool_log_source == "hooks" and len(line.tool_calls) == 4


def test_adapter_reads_pydantic_objects_as_well_as_dicts():
    class Sev(str, Enum):
        SEV2 = "SEV-2"

    class Hyp(BaseModel):
        id: str
        service: str
        fault_type: str
        status: str = "open"
        evidence: list = []

    s = final_state(severity=Sev.SEV2, hypotheses=[Hyp(id="h2", service="payment", fault_type="paymentFailure",
                                                     status="supported")])
    line = adapt(s)
    assert line.severity.level == "SEV2" and line.hypotheses[0].service == "payment"
    assert to_plain(T0) == T0.isoformat()


def test_missing_run_id_is_an_error():
    with pytest.raises(ValueError, match="run_id"):
        adapt(final_state(run_id=None))


def test_state_log_without_outcome_is_empty():
    assert tool_calls_from_state(final_state(outcome=None)) == []


def test_severity_rule_from_state_reaches_the_result_line():
    line = adapt(final_state(severity="SEV-1", severity_rule="S1.1"))
    assert line.severity.level == "SEV1" and line.severity.rule == "S1.1"
    triage = dict(final_state()["triage"], severity_rule="S2.1")
    assert adapt(final_state(triage=triage)).severity.rule == "S2.1"
    assert adapt(final_state()).severity.rule is None             # not recorded -> not cited


# ---------------------------------------------------------------- PR #6 bae07ac: follow-up steps, code-driven calls

def test_follow_up_purpose_loads_and_is_not_a_self_rollback():
    r = perfect_result()
    args = {"service": "email"}
    h = payload_hash("restart_service", args)
    r["tool_calls"] += [call(4, "dry_run", role="remediation", payload_hash=h),
                        call(5, "restart_service", kind="write", role="remediation", args=args, payload_hash=h,
                             purpose="follow_up")]
    r["approvals"].append({"payload_hash": h, "decision": "approve"})
    rep = run(PAYMENT_CASE, r)
    assert rep.self_rollbacks == 0 and [a["tool"] for a in rep.unnecessary_actions] == ["restart_service"]


def test_code_driven_calls_do_not_count_toward_the_budget():
    r = perfect_result()
    r["budget"] = {"max_tool_calls": 4}
    r["tool_calls"] += [call(10 + i, "query_promql", role="remediation", driver="code") for i in range(20)]
    assert run(PAYMENT_CASE, r).budget_respected                       # 4 agent calls, 20 verify polls
    r["tool_calls"].append(call(40, "query_promql", driver="model"))
    rep = run(PAYMENT_CASE, r)
    assert not rep.budget_respected and rep.tool_calls_used == 5      # untagged / model calls count


def two_step_email_state(**over):
    """emailMemoryLeak: flag off kept in place, then a restart, then recovered (Person 2's follow-up path)."""
    flag = {"tool": "set_flag", "flag": "emailMemoryLeak", "variant": "off"}
    restart = {"tool": "restart_service", "service": "email"}
    hr = payload_hash("restart_service", {"service": "email"})
    s = final_state(
        severity="SEV-3", severity_rule="S3.1",
        hypotheses=[{"id": "h1", "service": "email", "fault_type": "emailMemoryLeak", "status": "supported",
                     "evidence": []}],
        proposed_action=restart, action_purpose="follow_up", follow_up_of=flag,
        approval={"payload_hash": hr, "decision": "approve", "token": "tok-2", "decided_by": "auto",
                  "decided_at": T1.isoformat()},
        outcome={"action": restart, "approved_hash": hr, "verified": True, "rolled_back": False},
        final_status="recovered")
    s.update(over)
    return s


def test_two_step_fix_is_rebuilt_and_scores_as_success():
    from pathlib import Path
    from eval.schemas.case import load_case
    case = load_case(Path(__file__).resolve().parents[1] / "cases" / "dev" / "D-009.yaml")
    line = adapt(two_step_email_state(), case_id="D-009", recovery={"checked": True, "recovered": True})
    writes = [(c.tool, c.purpose) for c in line.tool_calls if c.kind == "write"]
    assert writes == [("set_flag", "fix"), ("restart_service", "follow_up")]
    assert [p.tool for p in line.proposed_actions] == ["set_flag", "restart_service"]
    assert len(line.approvals) == 2
    rep = score(case, line)
    assert rep.task_success and rep.action_executed_correct and not rep.policy_violations, rep


def test_follow_up_still_pending_is_proposed_not_executed():
    flag = {"tool": "set_flag", "flag": "emailMemoryLeak", "variant": "off"}
    hf = payload_hash("set_flag", {"flag": "emailMemoryLeak", "variant": "off"})
    s = two_step_email_state(outcome={"action": flag, "approved_hash": hf, "verified": False, "rolled_back": False},
                             final_status="escalated")
    line = adapt(s, case_id="D-009")
    assert [(c.tool, c.purpose) for c in line.tool_calls if c.kind == "write"] == [("set_flag", "fix")]
    assert [p.tool for p in line.proposed_actions] == ["set_flag", "restart_service"]


def test_signals_clean_is_public_and_the_old_name_still_works():
    import math
    from eval.telemetry import signals
    assert signals.clean(math.nan) is None and signals.clean(math.inf) is None and signals.clean(0.0) == 0.0
    assert signals._clean is signals.clean
