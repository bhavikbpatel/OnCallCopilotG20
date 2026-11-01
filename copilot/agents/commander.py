"""3.2 / 3.4 / 3.6 Commander — plan, hypothesize, assess.

The model writes plan notes, proposes hypotheses and summarises evidence.
Code decides: plan versions, which hypotheses are tested, the per-branch budget, and converge/replan/escalate.
"""
from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel

from copilot.agents.base import emit, event, guarded, now, structured_call
from copilot.deps import CopilotDeps
from copilot.memory.views import commander_view
from copilot.policy import budget_exhausted
from copilot.state import FaultType, Hypothesis, IncidentPlan, IncidentState, PlanStep, ServiceName

PLAN_STEPS = ("scope", "hypotheses", "test", "act", "verify", "communicate")


class PlanDraft(BaseModel):
    assumptions: list[str]
    step_notes: dict[str, str]          # step name → one line


class HypothesisDraft(BaseModel):
    service: ServiceName
    fault_type: FaultType
    rationale: str


class HypothesesDraft(BaseModel):
    hypotheses: list[HypothesisDraft]
    assumptions: list[str]


class AssessSummary(BaseModel):
    summary: str


def _plan(version: int, draft_notes: dict[str, str], assumptions: list[str], reason: str) -> IncidentPlan:
    steps = [PlanStep(name=s, note=draft_notes.get(s, "")) for s in PLAN_STEPS]
    return IncidentPlan(version=version, steps=steps, assumptions=assumptions, reason=reason, created_at=now())


def make_commander_plan(deps: CopilotDeps):
    @guarded("commander_plan")
    async def commander_plan(state: IncidentState) -> dict[str, Any]:
        draft, cost = await structured_call(deps, "commander", "commander_plan", PlanDraft, commander_view(state))
        plan = _plan(1, draft.step_notes, draft.assumptions, "initial")
        emit({"node": "commander_plan", "plan": plan.model_dump(mode="json")})
        return {"plan": plan, "budget": cost,
                "timeline": [event("commander_plan", "plan", "Plan v1: " + " → ".join(PLAN_STEPS))]}
    return commander_plan


def make_commander_hypothesize(deps: CopilotDeps):
    @guarded("commander_hypothesize")
    async def commander_hypothesize(state: IncidentState) -> dict[str, Any]:
        existing = state.get("hypotheses", [])
        reason = state.get("replan_reason")
        view = commander_view(state) | {"replan_reason": reason}
        draft, cost = await structured_call(deps, "commander", "commander_hypothesize", HypothesesDraft, view)

        # Code filters: never re-test a refuted or already-tracked (service, fault_type); cap fan-out.
        known = {(h.service, h.fault_type) for h in existing}
        fresh = [Hypothesis(id=f"h-{uuid.uuid4().hex[:6]}", service=d.service, fault_type=d.fault_type,
                            rationale=d.rationale)
                 for d in draft.hypotheses if (d.service, d.fault_type) not in known]
        open_count = sum(h.status == "open" for h in existing)
        fresh = fresh[:max(0, deps.settings.max_parallel_diagnosis - open_count)]

        update: dict[str, Any] = {"hypotheses": fresh, "budget": cost, "replan_reason": None}
        events = [event("commander_hypothesize", "plan",
                        f"Testing: {', '.join(f'{h.service}/{h.fault_type}' for h in fresh) or 'no new hypotheses'}")]
        if reason:  # replan → new plan version, old one goes to history (Signature A trace)
            old = state["plan"]
            notes = {s.name: s.note for s in old.steps}
            update |= {"plan": _plan(old.version + 1, notes, draft.assumptions, reason),
                       "plan_history": [old], "replans": state.get("replans", 0) + 1}
            events.insert(0, event("commander_hypothesize", "replan", f"Plan v{old.version + 1}: {reason}"))
        emit({"node": "commander_hypothesize", "hypotheses": [h.model_dump(mode="json") for h in fresh], "replan": reason})
        return update | {"timeline": events}
    return commander_hypothesize


def decide(state: IncidentState, max_replans: int) -> tuple[str, str]:
    """Pure routing rule (code, not vibes). Returns (decision, reason)."""
    hyps = state.get("hypotheses", [])
    supported = [h for h in hyps if h.status == "supported" and h.evidence]
    if supported:
        return "converge", f"{supported[0].service}/{supported[0].fault_type} supported"
    if budget_exhausted(state.get("budget"), state["limits"]):
        return "escalate", "tool/token budget exhausted without a supported hypothesis"
    if state.get("replans", 0) >= max_replans:
        return "escalate", f"no supported hypothesis after {max_replans} replans"
    contradicted = [h for h in hyps if h.contradicts_plan]
    if contradicted:
        return "replan", f"evidence contradicts plan ({', '.join(f'{h.service}/{h.fault_type}' for h in contradicted)})"
    if hyps and all(h.status == "refuted" for h in hyps):
        return "replan", "all hypotheses refuted"
    return "replan", "evidence inconclusive"


def make_commander_assess(deps: CopilotDeps):
    @guarded("commander_assess")
    async def commander_assess(state: IncidentState) -> dict[str, Any]:
        summary, cost = await structured_call(deps, "commander", "commander_assess", AssessSummary, commander_view(state))
        decision, reason = decide(state, deps.settings.max_replans)
        update: dict[str, Any] = {"evidence_summary": summary.summary, "budget": cost,
                                  "timeline": [event("commander_assess", "verdict", f"{decision}: {reason}")]}
        if decision == "replan":
            update["replan_reason"] = reason
        elif decision == "escalate":
            update["escalation_reason"] = reason
        emit({"node": "commander_assess", "decision": decision, "reason": reason})
        return update
    return commander_assess
