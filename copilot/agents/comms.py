"""3.11 Comms — status note + postmortem draft. Timeline comes from state, never from the model.

Output guardrail (code): every cited evidence_id must exist in the incident's evidence.
External publishing (tickets.publish_status) is gated and not done here; the note is a draft.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from copilot.agents.base import emit, event, guarded, structured_call
from copilot.deps import CopilotDeps
from copilot.memory.views import comms_view
from copilot.state import IncidentState, Postmortem, ProposedLesson, StatusNote, TimelineEvent


class CommsDraft(BaseModel):
    impact: str                       # who is affected and how (model)
    cause: str                        # supported root cause, or what is known so far (model)
    evidence_ids: list[str]
    summary: str
    root_cause: str
    lessons_proposed: list[ProposedLesson]


def actions_taken(timeline: list[TimelineEvent]) -> str:
    done = [e.text for e in timeline if e.node == "execute" and e.kind in ("action", "rollback")]
    return "; ".join(done) if done else "none taken"


def status_line(state: IncidentState, timeline: list[TimelineEvent]) -> str:
    final = state.get("final_status")
    if final == "recovered":
        verdict = next((e.text for e in reversed(timeline) if e.node == "verify"), "")
        return f"recovered ({verdict})" if verdict else "recovered"
    if final == "noise":
        return "no action needed, the alert was judged noise"
    if final == "escalated" or state.get("escalation_reason"):
        return f"escalated to the on-call engineer: {state.get('escalation_reason') or 'see timeline'}"
    return "in progress"


def make_comms(deps: CopilotDeps):
    @guarded("comms")
    async def comms(state: IncidentState) -> dict[str, Any]:
        timeline = sorted(state.get("timeline", []), key=lambda e: e.at)   # parallel branches finish out of order
        view = comms_view(state) | {"timeline": [e.model_dump(mode="json") for e in timeline],
                                    "final_status": state.get("final_status"),
                                    "escalation_reason": state.get("escalation_reason")}
        draft, cost = await structured_call(deps, "comms", "comms", CommsDraft, view)

        known = {e.evidence_id for e in state.get("evidence", [])} | \
                {r.evidence_id for h in state.get("hypotheses", []) for r in h.evidence}
        unknown = [i for i in draft.evidence_ids if i not in known]
        # Impact and cause come from the model; action and status are facts, written by code from state.
        text = f"Impact: {draft.impact.strip()} Cause: {draft.cause.strip()} " \
               f"Action: {actions_taken(timeline)}. Status: {status_line(state, timeline)}."
        note = StatusNote(text=text, evidence_ids=[i for i in draft.evidence_ids if i in known])

        outcome = state.get("outcome")
        postmortem = Postmortem(
            summary=draft.summary, root_cause=draft.root_cause,
            action=outcome.action if outcome else None,
            timeline=timeline,
            lessons_proposed=draft.lessons_proposed,
        )
        await deps.tickets.save_postmortem(state["incident_id"], postmortem)

        events = [event("comms", "comms", "Status note and postmortem drafted")]
        if unknown:
            events.append(event("comms", "comms", f"guardrail: dropped uncited evidence ids {unknown}"))
        emit({"node": "comms", "status_note": note.text})
        return {"status_note": note, "postmortem": postmortem, "budget": cost, "timeline": events}
    return comms
