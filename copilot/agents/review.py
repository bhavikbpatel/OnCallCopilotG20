"""3.12 Review + memory — the only writer of long-term memory, and only after a person says yes."""
from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from copilot.agents.base import event, now
from copilot.deps import CopilotDeps
from copilot.memory import IncidentRecord, Lesson
from copilot.state import IncidentState, ReviewDecision


def make_review(deps: CopilotDeps):
    async def review(state: IncidentState) -> dict[str, Any]:
        postmortem = state.get("postmortem")
        if not deps.settings.review_enabled or postmortem is None:
            return {"timeline": [event("review", "memory", "review disabled; nothing stored")]}

        # No side effects before interrupt(): this node re-runs from the top on resume.
        raw = interrupt({
            "kind": "review",
            "incident_id": state["incident_id"],
            "postmortem": postmortem.model_dump(mode="json"),
            "status_note": state["status_note"].model_dump(mode="json") if state.get("status_note") else None,
        })
        decision = ReviewDecision.model_validate(raw)
        if not decision.accept_postmortem:
            return {"timeline": [event("review", "memory", f"postmortem rejected by {decision.reviewed_by}; nothing stored")]}

        memory = deps.memory(state.get("run_id"))
        incident_id = state["incident_id"]
        supported = [h for h in state.get("hypotheses", []) if h.status == "supported"]
        alert = state["alert"]
        await memory.save_incident(IncidentRecord(
            incident_id=incident_id,
            alert_name=alert.alert_name,
            services=sorted({str(h.service) for h in supported} | {alert.service}),
            fault_family=postmortem.lessons_proposed[0].fault_family if postmortem.lessons_proposed else "unknown",
            symptom_text=alert.summary,
            root_cause=decision.edited_root_cause or postmortem.root_cause,
            action=postmortem.action.model_dump(mode="json") if postmortem.action else None,
            outcome=state.get("final_status") or "escalated",
            timeline=[t.model_dump(mode="json") for t in postmortem.timeline],
            reviewed_by=decision.reviewed_by,
            stored_at=now(),
        ))

        stored, rejected = [], []
        by_index = {d.index: d for d in decision.lessons}
        for i, proposed in enumerate(postmortem.lessons_proposed):
            d = by_index.get(i)
            if d is None or d.decision == "reject":
                rejected.append(f"#{i}: {d.reason if d else 'not reviewed'}")
                continue
            chosen = d.edited if d.decision == "edit" and d.edited else proposed
            lesson = Lesson(lesson_id=f"L-{incident_id}-{i}", text=chosen.text, preconditions=chosen.preconditions,
                            service=chosen.service, fault_family=chosen.fault_family,
                            source_incident=incident_id, reviewed_by=decision.reviewed_by)
            await memory.save_lesson(lesson)
            stored.append(lesson.lesson_id)

        text = f"stored incident; lessons stored {stored}; rejected {rejected}"
        return {"timeline": [event("review", "memory", text)]}
    return review
