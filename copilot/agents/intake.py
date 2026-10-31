"""3.1 Intake — turn the Alert into a fresh incident state. Code only."""
from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from copilot.agents.base import event
from copilot.deps import CopilotDeps
from copilot.memory import MemoryContext
from copilot.state import BudgetLedger, IncidentState


def make_intake(deps: CopilotDeps):
    async def intake(state: IncidentState, config: RunnableConfig) -> dict[str, Any]:
        conf = config.get("configurable", {})
        alert = state["alert"]
        prefs = await deps.memory(conf.get("run_id")).list_prefs(conf.get("engineer_id"))
        return {
            "incident_id": conf["thread_id"],
            "run_id": conf.get("run_id"),
            "engineer_id": conf.get("engineer_id"),
            "limits": deps.settings.limits,
            "budget": BudgetLedger(),
            "replans": 0,
            "replan_reason": None,
            "hypotheses": [],
            "evidence_summary": "",
            "escalation_reason": None,
            "final_status": None,
            "memory_context": MemoryContext(engineer_prefs=prefs),
            "timeline": [event("intake", "plan", f"Alert {alert.alert_name} on {alert.service}: {alert.summary}")],
        }
    return intake
