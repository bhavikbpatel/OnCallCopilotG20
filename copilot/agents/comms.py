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
from copilot.state import IncidentState, Postmortem, ProposedLesson, StatusNote


class CommsDraft(BaseModel):
    status_note: StatusNote
    summary: str
    root_cause: str
    lessons_proposed: list[ProposedLesson]


def make_comms(deps: CopilotDeps):
    @guarded("comms")
    async def comms(state: IncidentState) -> dict[str, Any]:
        view = comms_view(state) | {"final_status": state.get("final_status"),
                                    "escalation_reason": state.get("escalation_reason")}
        draft, cost = await structured_call(deps, "comms", "comms", CommsDraft, view)

        known = {e.evidence_id for e in state.get("evidence", [])} | \
                {r.evidence_id for h in state.get("hypotheses", []) for r in h.evidence}
        unknown = [i for i in draft.status_note.evidence_ids if i not in known]
        note = draft.status_note.model_copy(update={
            "evidence_ids": [i for i in draft.status_note.evidence_ids if i in known]})

        outcome = state.get("outcome")
        postmortem = Postmortem(
            summary=draft.summary, root_cause=draft.root_cause,
            action=outcome.action if outcome else None,
            timeline=state.get("timeline", []),
            lessons_proposed=draft.lessons_proposed,
        )
        await deps.tickets.save_postmortem(state["incident_id"], postmortem)

        events = [event("comms", "comms", "Status note and postmortem drafted")]
        if unknown:
            events.append(event("comms", "comms", f"guardrail: dropped uncited evidence ids {unknown}"))
        emit({"node": "comms", "status_note": note.text})
        return {"status_note": note, "postmortem": postmortem, "budget": cost, "timeline": events}
    return comms
