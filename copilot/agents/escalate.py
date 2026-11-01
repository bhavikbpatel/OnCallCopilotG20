"""Escalate — hand the incident to the engineer without acting (budget out, no safe action, errors)."""
from __future__ import annotations

from typing import Any

from copilot.agents.base import emit, event
from copilot.deps import CopilotDeps
from copilot.policy import budget_exhausted
from copilot.state import IncidentState


def escalation_cause(state: IncidentState, max_replans: int) -> str:
    """Routing can escalate without writing state (replan cap, budget); recover the reason here."""
    if state.get("escalation_reason"):
        return state["escalation_reason"]
    if budget_exhausted(state.get("budget"), state["limits"]):
        total = state["budget"].total
        return f"budget exhausted ({total.tool_calls} tool calls, {total.tokens} tokens)"
    if state.get("replans", 0) >= max_replans:
        return f"replan limit reached ({max_replans}); last reason: {state.get('replan_reason') or 'n/a'}"
    return "unknown"


def make_escalate(deps: CopilotDeps):
    async def escalate(state: IncidentState) -> dict[str, Any]:
        reason = escalation_cause(state, deps.settings.max_replans)
        emit({"node": "escalate", "reason": reason})
        return {"final_status": "escalated", "escalation_reason": reason,
                "timeline": [event("escalate", "escalate", f"Escalated to engineer: {reason}")]}
    return escalate
