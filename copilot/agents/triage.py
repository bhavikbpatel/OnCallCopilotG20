"""3.3 Triage — impact, suspects, recent changes. Severity and noise are decided by code."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from pydantic import BaseModel

from copilot.agents.base import emit, event, guarded, now, spend, structured_call
from copilot.deps import CopilotDeps
from copilot.policy import is_noise, severity_for
from copilot.state import IncidentState, ServiceName, TriageReport, sum_ledgers

CHANGE_LOOKBACK = timedelta(minutes=30)


class TriageDraft(BaseModel):
    suspects: list[ServiceName]
    impact: str
    changes: list[str]


def make_triage(deps: CopilotDeps):
    @guarded("triage")
    async def triage(state: IncidentState) -> dict[str, Any]:
        alert = state["alert"]
        health = await deps.telemetry.service_health("5m")
        changes = await deps.telemetry.recent_changes(now() - CHANGE_LOOKBACK)
        current = await deps.telemetry.query_scalar(alert.query)
        tool_spend = spend("triage", tool_calls=3)

        draft, llm_spend = await structured_call(deps, "triage", "triage", TriageDraft, {
            "alert": alert.model_dump(mode="json"),
            "service_health": [h.model_dump() for h in health],
            "recent_changes": changes,
        })
        noise = is_noise(current, alert.threshold, len(changes))
        severity = severity_for(alert.service, alert.value, alert.threshold, [str(s) for s in draft.suspects])
        report = TriageReport(severity=severity, is_noise=noise, suspects=draft.suspects,
                              impact=draft.impact, changes=draft.changes)

        await deps.tickets.create_incident(state["incident_id"], alert, severity)
        text = "noise: alert no longer breaching, no recent changes" if noise else \
            f"{severity}; suspects {', '.join(draft.suspects)}; {draft.impact}"
        emit({"node": "triage", "text": text})
        return {
            "triage": report,
            "severity": severity,
            "final_status": "noise" if noise else None,
            "budget": sum_ledgers(tool_spend, llm_spend),
            "timeline": [event("triage", "triage", text)],
        }
    return triage
