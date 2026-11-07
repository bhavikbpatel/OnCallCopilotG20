"""3.7 Runbook — runbook passages + similar incidents + lessons (preconditions checked by code)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from copilot import toolctx
from copilot.agents.base import emit, event, guarded, now, spend, structured_call
from copilot.clients import Passage, ToolError
from copilot.deps import CopilotDeps
from copilot.memory.views import runbook_view
from copilot.state import IncidentState, RunbookAdvice, sum_ledgers


POSTMORTEM_LOOKBACK = timedelta(minutes=30)


def lesson_scope(alert_service: str, supported: list, hypotheses: list) -> list[str]:
    """Services whose lessons are checked: the chosen cause first, then the alert's service, then every other
    hypothesis; each once."""
    out: list[str] = []
    for svc in [*(str(h.service) for h in supported), str(alert_service), *(str(h.service) for h in hypotheses)]:
        if svc not in out:
            out.append(svc)
    return out


def postmortem_applies(passage: Passage, active_flags: set[str], services: set[str]) -> bool:
    """A past incident's fix is a lesson too: it applies only when its target is in play now (code, not the model).
    set_flag → that flag is currently on; restart / scale / rollback → that service is a supported suspect."""
    fix = passage.fix or {}
    args = fix.get("args") or {}
    if fix.get("tool") == "set_flag":
        return args.get("flag") in active_flags
    return bool(args.get("service")) and args["service"] in services


async def gate_postmortems(passages: dict[str, Passage], deps: CopilotDeps,
                           services: list[str]) -> tuple[dict[str, Passage], list[str]]:
    """Runbooks pass through. Postmortems need long-term memory on (E4 'memory off' sees none) and must apply now;
    the rest are dropped and their ids returned as rejected."""
    pms = {k: p for k, p in passages.items() if p.kind == "postmortem"}
    if not pms:
        return passages, []
    if deps.store is None:
        return {k: p for k, p in passages.items() if k not in pms}, []
    try:
        changes = await deps.telemetry.recent_changes(now() - POSTMORTEM_LOOKBACK)
    except ToolError:                # no flag reading: no postmortem can be shown to apply; the incident goes on
        changes = []
    active = {c.get("flag") for c in changes if c.get("flag") and c.get("variant", "on") != "off"}
    keep = {k: p for k, p in pms.items() if postmortem_applies(p, active, set(services))}
    rejected = sorted({p.doc_id for k, p in pms.items() if k not in keep})
    return {k: p for k, p in passages.items() if k not in pms or k in keep}, rejected


def make_runbook(deps: CopilotDeps):
    @guarded("runbook")
    async def runbook(state: IncidentState) -> dict[str, Any]:
        alert = state["alert"]
        supported = [h for h in state.get("hypotheses", []) if h.status == "supported"]
        services = [str(h.service) for h in supported]
        # Lessons are read for every service in play, not only the chosen cause: the alert's service and every
        # hypothesis (supported, open or refuted). Code-checked preconditions still decide which apply, so a lesson
        # that would mislead here is read and REJECTED (E4), e.g. payment lessons on a checkout/paymentUnreachable fault.
        lesson_services = lesson_scope(alert.service, supported, state.get("hypotheses", []))
        families = None   # fault family mapping lives with role 2; filter by service only for now

        # Query decomposition: one sub-query per supported hypothesis, plus the raw symptom.
        queries = [(f"{h.service} {h.fault_type} {alert.signal}", str(h.service)) for h in supported]
        queries.append((alert.summary, None))
        passages = {}
        for q, svc in queries:
            for p in await deps.runbooks.search(q, service=svc, symptom=alert.signal, k=5):
                passages.setdefault(p.passage_id, p)

        with toolctx.scope(driver=toolctx.CODE):        # lesson preconditions: code checks, not budgeted
            memory_context = await deps.memory(state.get("run_id")).build_memory_context(
                symptom=alert.summary, suspects=lesson_services, fault_families=families,
                telemetry=deps.telemetry, engineer_id=state.get("engineer_id"),
            )
            passages, rejected_pm = await gate_postmortems(passages, deps, services)
        prefs = (state.get("memory_context") or memory_context).engineer_prefs
        memory_context.engineer_prefs = prefs

        view = runbook_view({**state, "memory_context": memory_context}) | {
            "passages": [p.model_dump() for p in sorted(passages.values(), key=lambda p: -p.score)[:8]],
        }
        advice, cost = await structured_call(deps, "runbook", "runbook", RunbookAdvice, view)
        # Code owns the lesson verdicts; never trust the model's copy.
        advice.rejected_lessons = [r.lesson.lesson_id for r in memory_context.rejected_lessons] + rejected_pm

        text = (f"runbook {advice.runbook_id or 'none'}; lessons applicable "
                f"{[l.lesson_id for l in memory_context.applicable_lessons]}, rejected {advice.rejected_lessons}")
        emit({"node": "runbook", "text": text})
        return {
            "runbook_advice": advice,
            "memory_context": memory_context,
            "budget": sum_ledgers(spend("runbook", code_calls=len(queries)), cost),   # code's searches: not budgeted
            "timeline": [event("runbook", "verdict", text)],
        }
    return runbook
