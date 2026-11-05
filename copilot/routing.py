"""Edge routing. Every arrow is chosen by code from state — the model proposes, code decides."""
from __future__ import annotations

from typing import Literal

from langgraph.types import Send

from copilot.config import AppSettings
from copilot.policy import budget_exhausted
from copilot.state import DiagnosisInput, IncidentState


def _escalating(state: IncidentState) -> bool:
    return bool(state.get("escalation_reason"))


def _replan_or_escalate(state: IncidentState, settings: AppSettings) -> Literal["commander_hypothesize", "escalate"]:
    if state.get("replans", 0) >= settings.max_replans or budget_exhausted(state.get("budget"), state["limits"]):
        return "escalate"
    return "commander_hypothesize"


def make_routes(settings: AppSettings):
    def ok_or_escalate(next_node: str):
        def route(state: IncidentState) -> str:
            return "escalate" if _escalating(state) else next_node
        route.__name__ = f"to_{next_node}"
        return route

    def after_triage(state: IncidentState) -> Literal["comms", "commander_hypothesize", "escalate"]:
        if _escalating(state):
            return "escalate"
        return "comms" if state["triage"].is_noise else "commander_hypothesize"

    def fan_out(state: IncidentState) -> list[Send] | Literal["commander_assess", "escalate"]:
        if _escalating(state):
            return "escalate"
        open_hyps = [h for h in state.get("hypotheses", []) if h.status == "open"]
        if not open_hyps:
            return "commander_assess"
        limits = state["limits"]
        spent = state["budget"].total.tool_calls if state.get("budget") else 0
        per_branch = min(limits.per_diagnosis_tool_calls, max(1, (limits.tool_calls - spent) // len(open_hyps)))
        return [Send("diagnosis", DiagnosisInput(
                    hypothesis=h, alert=state["alert"], triage=state.get("triage"),
                    evidence=[e for e in state.get("evidence", []) if e.hypothesis_id == h.id],
                    tool_call_budget=per_branch))
                for h in open_hyps]

    def after_assess(state: IncidentState) -> Literal["runbook", "commander_hypothesize", "comms", "escalate"]:
        if _escalating(state):
            return "escalate"
        if state.get("final_status") == "noise":          # only "noise" was supported: close with no action
            return "comms"
        return _replan_or_escalate(state, settings) if state.get("replan_reason") else "runbook"

    def after_propose(state: IncidentState) -> Literal["dry_run", "rollback", "escalate"]:
        if _escalating(state):
            return "escalate"
        return "dry_run" if state.get("proposed_action") else "rollback"     # no follow-up step → undo the fix

    def after_approval(state: IncidentState) -> Literal["execute", "dry_run", "rollback", "commander_hypothesize",
                                                        "escalate"]:
        if _escalating(state):
            return "escalate"
        decision = state["approval"].decision
        if decision == "approve":
            return "execute"
        if decision == "edit":
            return "dry_run"
        if state.get("action_purpose") == "follow_up":     # follow-up rejected → undo the fix still in place
            return "rollback"
        return _replan_or_escalate(state, settings)

    def after_execute(state: IncidentState) -> Literal["verify", "commander_hypothesize", "escalate"]:
        if _escalating(state):
            return "escalate"
        if state.get("action_purpose") == "rollback":
            return _replan_or_escalate(state, settings)
        return "verify"

    def after_verify(state: IncidentState) -> Literal["comms", "propose_action", "rollback", "escalate"]:
        if _escalating(state):            # recovery could not be judged: never roll back on missing data
            return "escalate"
        if state["outcome"].verified:
            return "comms"
        # verify just opened a follow-up (the unrecovered fix is the one kept in place) → one complementary step
        if state["outcome"].action == state.get("follow_up_of") and not budget_exhausted(state.get("budget"), state["limits"]):
            return "propose_action"
        return "rollback"

    return {
        "ok_or_escalate": ok_or_escalate,
        "after_triage": after_triage,
        "fan_out": fan_out,
        "after_assess": after_assess,
        "after_propose": after_propose,
        "after_approval": after_approval,
        "after_execute": after_execute,
        "after_verify": after_verify,
    }
