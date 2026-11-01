"""Person 2's final graph state -> one ResultLine for scoring.

The copilot (``copilot/state.py``: ``IncidentState``) and the harness describe a run
in different shapes. This module is the only place that knows both, so neither side
has to change its models to suit the other.

It never imports ``copilot``: it reads the state as plain data (pydantic objects or
the dicts ``model_dump()`` gives), so the harness runs without LangGraph installed
and a change in the graph's internals breaks one adapter test, not the scoring.

Mapping (Person 2 -> harness):
- ``final_status`` noise, or ``triage.is_noise``         -> verdict "noise"
- an open/supported hypothesis, a proposed or executed action -> verdict "incident"
- otherwise (e.g. escalated with every hypothesis refuted)  -> verdict "undetermined"
- ``hypotheses`` (no rank)  -> ranked: supported first, then open, then refuted,
  keeping the graph's order inside each group
- ``severity`` "SEV-2"      -> severity.level "SEV2"; noise -> "NONE"
- ``severity_rule`` (or ``triage.severity_rule``) -> severity.rule (Person 1's rule ID, e.g. "S1.1")
- ``outcome.verified`` / ``outcome.rolled_back`` -> verification
- ``follow_up_of`` (a fix kept in place while a follow-up step runs) -> both steps in the
  proposed actions, the rebuilt log and the implied approvals
- ``limits.tool_calls``     -> budget.max_tool_calls
- ``budget`` ledger          -> cost.tokens_total / cost.usd
- ``memory_context`` lessons -> lessons_used / lessons_rejected

What the final state cannot tell us, the caller passes in:
- ``tool_calls``: the full tool log (Person 3's hooks, or the runner's capture).
  The state keeps only the last action, so it cannot show every call.
- ``approvals``: every approval-gate decision. In evaluation the runner is the
  approver, so it records each one; the state keeps only the last.
If they are not passed, a best-effort log is rebuilt from the state and the line is
marked ``tool_log_source="state"`` so reports can tell it apart.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any, Iterable, Mapping, Optional

from ..schemas.result import ResultLine, payload_hash

DEFAULT_MAX_TOOL_CALLS = 20  # Person 2's BudgetLimits default
_STATUS_ORDER = {"supported": 0, "open": 1, "refuted": 2}


def to_plain(value: Any) -> Any:
    """Pydantic models, enums and datetimes -> JSON-like Python values."""
    if hasattr(value, "model_dump"):
        return to_plain(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    return value


def split_action(action: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """{"tool": "set_flag", "flag": ..., "variant": ...} -> ("set_flag", {"flag": ..., "variant": ...})."""
    action = dict(action)
    tool = action.pop("tool")
    return tool, action


def rank_hypotheses(hypotheses: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    items = [to_plain(h) for h in hypotheses or []]
    order = sorted(range(len(items)), key=lambda i: (_STATUS_ORDER.get(items[i].get("status"), 1), i))
    out = []
    for rank, i in enumerate(order, start=1):
        h = items[i]
        out.append({
            "rank": rank,
            "service": h.get("service"),
            "fault_type": h.get("fault_type"),
            "status": h.get("status", "open"),
            "evidence_ids": [e.get("evidence_id") for e in h.get("evidence") or [] if e.get("evidence_id")],
            "hypothesis_id": h.get("id"),
        })
    return out


def _verdict(state: Mapping[str, Any], hypotheses: list[dict], actions: list[dict]) -> str:
    triage = state.get("triage") or {}
    if state.get("final_status") == "noise" or triage.get("is_noise"):
        return "noise"
    if actions or state.get("outcome") or any(h["status"] != "refuted" for h in hypotheses):
        return "incident"
    return "undetermined"


def _first_fix(state: Mapping[str, Any]) -> Optional[dict]:
    """The fix kept in place while a follow-up step runs (Person 2's ``follow_up_of``), if any.

    Only meaningful while the follow-up is the latest action: after a rollback the graph clears
    ``follow_up_of``, and the first fix can no longer be recovered from the state alone.
    """
    first = state.get("follow_up_of")
    outcome = state.get("outcome")
    if first and outcome and outcome.get("action") != first:
        return first
    return None


def _actions(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Fix and follow-up actions the graph proposed or executed (rollbacks are not proposals)."""
    seen, out = set(), []
    candidates = []
    if _first_fix(state):
        candidates.append(_first_fix(state))
    if state.get("outcome"):
        candidates.append(state["outcome"]["action"])
    if state.get("proposed_action") and state.get("action_purpose", "fix") in ("fix", "follow_up"):
        candidates.append(state["proposed_action"])
    for action in candidates:
        tool, args = split_action(action)
        h = payload_hash(tool, args)
        if h not in seen:
            seen.add(h)
            out.append({"tool": tool, "args": args, "payload_hash": h})
    return out


def _approvals_from_state(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The last approval record, plus the fix's approval when a later one replaced it.

    After a rollback, ``approval`` holds the rollback's decision; the fix was approved
    too (the graph executes nothing without it), and ``outcome.approved_hash`` says
    for which payload. The token for that one is no longer known.
    """
    out = []
    a = state.get("approval")
    if a:
        out.append({"payload_hash": a["payload_hash"], "decision": a["decision"],
                    "decided_by": a.get("decided_by") or "auto", "has_token": bool(a.get("token"))})
    outcome = state.get("outcome")
    if outcome and outcome.get("approved_hash") and all(x["payload_hash"] != outcome["approved_hash"] for x in out):
        out.insert(0, {"payload_hash": outcome["approved_hash"], "decision": "approve",
                       "decided_by": "implied-by-outcome", "has_token": None})
    first = _first_fix(state)
    if first:                                       # the first step of a two-step fix was approved too
        h = payload_hash(*split_action(first))
        if all(x["payload_hash"] != h for x in out):
            out.insert(0, {"payload_hash": h, "decision": "approve", "decided_by": "implied-by-outcome",
                           "has_token": None})
    return out


def tool_calls_from_state(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Best-effort log of the shop-changing calls, rebuilt from the final state.

    The graph only executes after a dry run of the same action (the undo of a failed
    fix goes through dry_run and the approval gate too), so each executed action gets
    a dry-run entry before it. Reads (telemetry, runbooks) are not
    in the state and are missing here.
    """
    calls: list[dict[str, Any]] = []

    def add(tool: str, args: dict, *, kind: str, purpose: Optional[str] = None) -> None:
        calls.append({
            "seq": len(calls), "agent": "remediation", "agent_role": "remediation",
            "server": "ops_actions", "tool": tool, "kind": kind, "args": args,
            "payload_hash": payload_hash(*split_action(args)) if tool == "dry_run" else payload_hash(tool, args),
            "ok": True, "purpose": purpose,
        })

    outcome = state.get("outcome")
    first = _first_fix(state)
    if first:                                       # two-step fix: first step, then the follow-up
        ftool, fargs = split_action(first)
        add("dry_run", {"tool": ftool, **fargs}, kind="read")
        add(ftool, fargs, kind="write", purpose="fix")
    if outcome:
        tool, args = split_action(outcome["action"])
        add("dry_run", {"tool": tool, **args}, kind="read")
        add(tool, args, kind="write", purpose="follow_up" if first else "fix")
        undo = state.get("proposed_action")
        if outcome.get("rolled_back") and undo and state.get("action_purpose") == "rollback":
            utool, uargs = split_action(undo)       # rollback -> dry_run -> approval -> execute
            add("dry_run", {"tool": utool, **uargs}, kind="read")
            add(utool, uargs, kind="write", purpose="rollback")
    return calls


def _lessons(state: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    mem = state.get("memory_context") or {}
    used = [l["lesson_id"] for l in mem.get("applicable_lessons") or [] if l.get("lesson_id")]
    rejected = [r["lesson"]["lesson_id"] for r in mem.get("rejected_lessons") or []
                if (r.get("lesson") or {}).get("lesson_id")]
    advice = state.get("runbook_advice") or {}
    for lid in advice.get("rejected_lessons") or []:
        if lid not in rejected:
            rejected.append(lid)
    return used, rejected


def agent_texts(state: Mapping[str, Any]) -> list[str]:
    """Every free text the graph produced, for the red-team leak check (canary search)."""
    out: list[str] = []

    def add(x: Any) -> None:
        if isinstance(x, str) and x.strip():
            out.append(x)

    add((state.get("status_note") or {}).get("text"))
    pm = state.get("postmortem") or {}
    add(pm.get("summary"))
    add(pm.get("root_cause"))
    for lesson in pm.get("lessons_proposed") or []:
        add(lesson.get("text"))
    add(state.get("escalation_reason"))
    add(state.get("evidence_summary"))
    add(state.get("expected_effect"))
    add((state.get("triage") or {}).get("impact"))
    for e in state.get("evidence") or []:
        add(e.get("summary"))
    for h in state.get("hypotheses") or []:
        add(h.get("rationale"))
    for t in state.get("timeline") or []:
        add(t.get("text"))
    return out


def _cost(state: Mapping[str, Any]) -> dict[str, Any]:
    ledger = (state.get("budget") or {}).get("by_agent") or {}
    return {"tokens_total": int(sum(s.get("tokens", 0) for s in ledger.values())),
            "usd": float(sum(s.get("usd", 0.0) for s in ledger.values()))}


def to_result_line(
    state: Any,
    *,
    case_id: str,
    config_id: str,
    started_at: dt.datetime,
    ended_at: dt.datetime,
    run_id: Optional[str] = None,
    status: str = "completed",
    error: Optional[str] = None,
    repeat: int = 1,
    agent_version: str = "unknown",
    config_hash: Optional[str] = None,
    trace_id: Optional[str] = None,
    tool_calls: Optional[list[Any]] = None,
    approvals: Optional[list[Any]] = None,
    recovery: Optional[Mapping[str, Any]] = None,
    max_tool_calls: Optional[int] = None,
    tool_log_source: Optional[str] = None,
    attack_id: Optional[str] = None,
) -> ResultLine:
    """Build the result line for one run from the copilot's final state.

    ``max_tool_calls`` overrides the state's ``limits`` (the runner's config is the
    source of truth for the budget a run was given). ``tool_log_source`` labels a
    caller-supplied log (default "hooks").
    """
    s = to_plain(state) or {}
    run_id = run_id or s.get("run_id")
    if not run_id:
        raise ValueError("run_id is needed: pass it, or set it in the graph's input state")

    hypotheses = rank_hypotheses(s.get("hypotheses") or [])
    actions = _actions(s)
    verdict = _verdict(s, hypotheses, actions)
    # Rule ID from Person 1's policy, if the graph records it (state "severity_rule", or the triage report's).
    rule = s.get("severity_rule") or (s.get("triage") or {}).get("severity_rule")
    if verdict == "noise":
        severity = {"level": "NONE", "rule": rule or None}
    elif s.get("severity"):
        severity = {"level": s["severity"], "rule": rule}
    else:
        severity = None
    outcome = s.get("outcome") or {}
    used, rejected = _lessons(s)

    if tool_calls is None:
        log, source = tool_calls_from_state(s), "state"
    else:
        log, source = to_plain(tool_calls), tool_log_source or "hooks"
    budget = max_tool_calls or int((s.get("limits") or {}).get("tool_calls") or DEFAULT_MAX_TOOL_CALLS)

    return ResultLine.model_validate({
        "run_id": run_id, "case_id": case_id, "config_id": config_id, "repeat": repeat,
        "agent_version": agent_version, "config_hash": config_hash,
        "status": status, "error": error, "started_at": started_at, "ended_at": ended_at, "trace_id": trace_id,
        "verdict": verdict,
        "final_status": s.get("final_status"),
        "escalation_reason": s.get("escalation_reason"),
        "incident_id": s.get("incident_id"),
        "hypotheses": hypotheses,
        "severity": severity,
        "proposed_actions": actions,
        "verification": {"agent_claims_recovered": outcome.get("verified") if outcome else None,
                         "rolled_back": bool(outcome.get("rolled_back"))},
        "replans": int(s.get("replans") or 0),
        "lessons_used": used,
        "lessons_rejected": rejected,
        "tool_calls": log,
        "tool_log_source": source,
        "approvals": to_plain(approvals) if approvals is not None else _approvals_from_state(s),
        "budget": {"max_tool_calls": budget},
        "cost": _cost(s),
        "recovery": dict(recovery) if recovery else {},
        "agent_texts": agent_texts(s),
        "attack_id": attack_id,
    })
