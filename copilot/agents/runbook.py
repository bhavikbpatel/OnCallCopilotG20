"""3.7 Runbook — runbook passages + similar incidents + lessons (preconditions checked by code)."""
from __future__ import annotations

from typing import Any

from copilot.agents.base import emit, event, guarded, spend, structured_call
from copilot.deps import CopilotDeps
from copilot.memory.views import runbook_view
from copilot.state import IncidentState, RunbookAdvice, sum_ledgers


def make_runbook(deps: CopilotDeps):
    @guarded("runbook")
    async def runbook(state: IncidentState) -> dict[str, Any]:
        alert = state["alert"]
        supported = [h for h in state.get("hypotheses", []) if h.status == "supported"]
        services = [str(h.service) for h in supported]
        families = None   # fault family mapping lives with role 2; filter by service only for now

        # Query decomposition: one sub-query per supported hypothesis, plus the raw symptom.
        queries = [(f"{h.service} {h.fault_type} {alert.signal}", str(h.service)) for h in supported]
        queries.append((alert.summary, None))
        passages = {}
        for q, svc in queries:
            for p in await deps.runbooks.search(q, service=svc, symptom=alert.signal, k=5):
                passages.setdefault(p.passage_id, p)

        memory_context = await deps.memory(state.get("run_id")).build_memory_context(
            symptom=alert.summary, suspects=services, fault_families=families,
            telemetry=deps.telemetry, engineer_id=state.get("engineer_id"),
        )
        prefs = (state.get("memory_context") or memory_context).engineer_prefs
        memory_context.engineer_prefs = prefs

        view = runbook_view({**state, "memory_context": memory_context}) | {
            "passages": [p.model_dump() for p in sorted(passages.values(), key=lambda p: -p.score)[:8]],
        }
        advice, cost = await structured_call(deps, "runbook", "runbook", RunbookAdvice, view)
        # Code owns the lesson verdicts; never trust the model's copy.
        advice.rejected_lessons = [r.lesson.lesson_id for r in memory_context.rejected_lessons]

        text = (f"runbook {advice.runbook_id or 'none'}; lessons applicable "
                f"{[l.lesson_id for l in memory_context.applicable_lessons]}, rejected {advice.rejected_lessons}")
        emit({"node": "runbook", "text": text})
        return {
            "runbook_advice": advice,
            "memory_context": memory_context,
            "budget": sum_ledgers(spend("runbook", tool_calls=len(queries)), cost),
            "timeline": [event("runbook", "verdict", text)],
        }
    return runbook
