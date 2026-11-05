"""Per-node views of IncidentState — what each agent is allowed to see (agent_graph_design.md §4.1).

Agents never get the whole state. Each returns a plain JSON-able dict for the prompt's context slot.
"""
from __future__ import annotations

from typing import Any

from copilot.state import Hypothesis, IncidentState


def _dump(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, list):
        return [_dump(o) for o in obj]
    return obj.model_dump(mode="json") if hasattr(obj, "model_dump") else obj


def _prefs(state: IncidentState) -> list[str]:
    ctx = state.get("memory_context")
    return [p.text for p in ctx.engineer_prefs] if ctx else []


def _budget_left(state: IncidentState) -> dict[str, Any]:
    limits, spent = state.get("limits"), state.get("budget")
    if limits is None:
        return {}
    total = spent.total if spent else None
    return {
        "tool_calls": limits.tool_calls - (total.tool_calls if total else 0),
        "usd": round(limits.usd - (total.usd if total else 0.0), 4),
    }


def _hypothesis_brief(h: Hypothesis) -> dict[str, Any]:
    return {
        "id": h.id, "service": h.service, "fault_type": h.fault_type, "status": h.status,
        "last_evidence": _dump(h.evidence[-1]) if h.evidence else None,
    }


def commander_view(state: IncidentState) -> dict[str, Any]:
    return {
        "alert": _dump(state.get("alert")),
        "triage": _dump(state.get("triage")),
        "plan": _dump(state.get("plan")),
        "hypotheses": [_hypothesis_brief(h) for h in state.get("hypotheses", [])],
        "evidence_summary": state.get("evidence_summary", ""),
        "budget_left": _budget_left(state),
        "engineer_prefs": _prefs(state),
    }


def triage_view(state: IncidentState) -> dict[str, Any]:
    return {"alert": _dump(state.get("alert"))}


def diagnosis_view(state: IncidentState, hypothesis_id: str) -> dict[str, Any]:
    h = next(h for h in state.get("hypotheses", []) if h.id == hypothesis_id)
    limits = state.get("limits")
    return {
        "alert": _dump(state.get("alert")),
        "hypothesis": _dump(h),
        "evidence": [_dump(e) for e in state.get("evidence", []) if e.hypothesis_id == hypothesis_id],
        "tool_call_budget": (limits.per_diagnosis_tool_calls if limits else None),
    }


def runbook_view(state: IncidentState) -> dict[str, Any]:
    ctx = state.get("memory_context")
    supported = [h for h in state.get("hypotheses", []) if h.status == "supported"]
    return {
        "triage": _dump(state.get("triage")),
        "supported_hypotheses": _dump(supported),
        "similar_incidents": [
            {"incident_id": r.incident_id, "root_cause": r.root_cause, "action": r.action, "outcome": r.outcome}
            for r in (ctx.similar_incidents if ctx else [])
        ],
        # Only lessons whose preconditions passed in code are shown as applicable.
        "applicable_lessons": [{"lesson_id": l.lesson_id, "text": l.text} for l in (ctx.applicable_lessons if ctx else [])],
        "rejected_lessons": [{"lesson_id": r.lesson.lesson_id, "reason": r.reason} for r in (ctx.rejected_lessons if ctx else [])],
    }


def remediation_view(state: IncidentState) -> dict[str, Any]:
    supported = [h for h in state.get("hypotheses", []) if h.status == "supported"]
    ids = {h.id for h in supported}
    return {
        # What the action must fix and the evidence for it; without these the model saw only the runbook advice
        # and escalated "no telemetry in the proposal" when the advice had no candidate action.
        "alert": _dump(state.get("alert")),
        "supported_hypotheses": _dump(supported),
        "evidence": [{"evidence_id": e.evidence_id, "summary": e.summary}
                     for e in state.get("evidence", []) if e.hypothesis_id in ids],
        "evidence_summary": state.get("evidence_summary", ""),
        "runbook_advice": _dump(state.get("runbook_advice")),
        "proposed_action": _dump(state.get("proposed_action")),
        "approval": _dump(state.get("approval")),
    }


def comms_view(state: IncidentState) -> dict[str, Any]:
    supported = [h for h in state.get("hypotheses", []) if h.status == "supported"]
    return {
        "incident_id": state.get("incident_id"),
        "alert": _dump(state.get("alert")),
        "severity": state.get("severity"),
        "timeline": _dump(state.get("timeline", [])),
        "root_cause": _dump(supported),
        "outcome": _dump(state.get("outcome")),
        "engineer_prefs": _prefs(state),
        # The only ids a status note may cite (output guardrail drops anything else).
        "evidence": [{"evidence_id": e.evidence_id, "summary": e.summary} for e in state.get("evidence", [])],
    }
