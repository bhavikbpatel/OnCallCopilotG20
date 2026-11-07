"""3.3 Triage — impact, suspects, recent changes. Severity and noise are decided by code."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from pydantic import BaseModel

from copilot import toolctx
from copilot.agents.base import emit, event, guarded, now, spend, structured_call
from copilot.deps import CopilotDeps
from copilot.policy import SEVERITY_WINDOW_MIN, classify_severity, is_noise
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
        try:                                                # the policy's 10-minute request sample (2 queries)
            with toolctx.scope(driver=toolctx.CODE):
                sample = await deps.telemetry.errors_since(alert.service, now() - timedelta(minutes=SEVERITY_WINDOW_MIN))
        except Exception:                                   # no sample: the classifier treats it as unknown
            sample = None
        # Triage's queries are fixed, not chosen by a model: code-driven, outside the agents' tool-call budget.
        # service_health is two PromQL queries, plus recent_changes, the alert query and the 2-query severity sample.
        tool_spend = spend("triage", code_calls=6)

        draft, llm_spend = await structured_call(deps, "triage", "triage", TriageDraft, {
            "alert": alert.model_dump(mode="json"),
            "service_health": [h.model_dump() for h in health],
            "recent_changes": changes,
        })
        noise = is_noise(current, alert.threshold, len(changes))
        severity, rule, reason = classify_severity(alert, current, health, changes, sample, noise)
        report = TriageReport(severity=severity, severity_rule=rule, severity_reason=reason, is_noise=noise,
                              suspects=draft.suspects, impact=draft.impact, changes=draft.changes)

        await deps.tickets.create_incident(state["incident_id"], alert, severity)
        text = "noise: alert no longer breaching, no recent changes" if noise else \
            f"{severity} ({rule}); suspects {', '.join(draft.suspects)}; {draft.impact}"
        emit({"node": "triage", "text": text})
        return {
            "triage": report,
            "severity": severity,
            "final_status": "noise" if noise else None,
            "budget": sum_ledgers(tool_spend, llm_spend),
            "timeline": [event("triage", "triage", text)],
        }
    return triage
