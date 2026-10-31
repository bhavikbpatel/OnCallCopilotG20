"""3.8–3.10 Propose action, approval gate, remediation (dry run → execute → verify → rollback).

Only this module touches the shop, and only `execute` writes — after an approved, hash-bound token.
"""
from __future__ import annotations

import asyncio
from typing import Any

from langgraph.types import interrupt
from pydantic import BaseModel, TypeAdapter

from copilot.agents.base import emit, event, guarded, now, spend, structured_call
from copilot.clients import ToolError
from copilot.deps import CopilotDeps
from copilot.memory.views import remediation_view
from copilot.policy import is_allowed
from copilot.state import (
    Action, ActionOutcome, ApprovalDecision, ApprovalRecord, EvidenceRef, IncidentState, payload_hash,
)

_action_adapter = TypeAdapter(Action)


class ActionProposal(BaseModel):
    action: Action | None                 # None → no safe action; escalate
    expected_effect: str
    rationale: str


def make_propose_action(deps: CopilotDeps):
    @guarded("propose_action")
    async def propose_action(state: IncidentState) -> dict[str, Any]:
        view = remediation_view(state) | {"last_rejection": state["approval"].reason
                                          if state.get("approval") and state["approval"].decision == "reject" else None}
        proposal, cost = await structured_call(deps, "remediation", "propose_action", ActionProposal, view)
        if proposal.action is None:
            reason = f"no safe action: {proposal.rationale}"
            return {"escalation_reason": reason, "budget": cost, "timeline": [event("propose_action", "escalate", reason)]}
        allowed, why = is_allowed(proposal.action)
        if not allowed:
            return {"escalation_reason": why, "budget": cost, "timeline": [event("propose_action", "escalate", why)]}
        return {
            "proposed_action": proposal.action, "action_purpose": "fix", "expected_effect": proposal.expected_effect,
            "approval": None, "budget": cost,
            "timeline": [event("propose_action", "action", f"Proposed {proposal.action.model_dump_json()}")],
        }
    return propose_action


def make_dry_run(deps: CopilotDeps):
    @guarded("dry_run")
    async def dry_run(state: IncidentState) -> dict[str, Any]:
        action = state["proposed_action"]
        result = await deps.ops.dry_run(action)
        return {"dry_run": result, "budget": spend("remediation", tool_calls=1),
                "timeline": [event("dry_run", "action", f"Dry run: {result.would_do}")]}
    return dry_run


def make_approval_gate(deps: CopilotDeps):
    async def approval_gate(state: IncidentState) -> dict[str, Any]:
        # No side effects before interrupt(): this node re-runs from the top on resume.
        action, dry = state["proposed_action"], state["dry_run"]
        digest = payload_hash(action)
        raw = interrupt({
            "kind": "approval",
            "incident_id": state["incident_id"],
            "purpose": state.get("action_purpose", "fix"),
            "action": action.model_dump(mode="json"),
            "dry_run": dry.model_dump(mode="json"),
            "expected_effect": state.get("expected_effect") or dry.expected_effect,
            "rollback_plan": dry.rollback_action.model_dump(mode="json") if dry.rollback_action else None,
            "verify_query": state["alert"].query,
            "payload_hash": digest,
        })
        decision = ApprovalDecision.model_validate(raw)
        record = ApprovalRecord(payload_hash=digest, decision=decision.decision, token=decision.token,
                                reason=decision.reason, decided_by=decision.decided_by, decided_at=now())
        update: dict[str, Any] = {"approval": record}
        text = f"{decision.decision} by {decision.decided_by or 'engineer'}" + (f": {decision.reason}" if decision.reason else "")

        if decision.decision == "edit":
            edited = _action_adapter.validate_python(decision.edited_action or {})
            allowed, why = is_allowed(edited)
            if not allowed:
                update["escalation_reason"] = f"edited action rejected: {why}"
            else:
                # New action → new hash → old token is useless; back to dry run and a fresh approval.
                update |= {"proposed_action": edited, "dry_run": None}
                text += f" → {edited.model_dump_json()}"
        elif decision.decision == "reject":
            if state.get("action_purpose") == "rollback":
                update["escalation_reason"] = f"rollback rejected: {decision.reason}"
            else:
                update |= {"replan_reason": f"engineer rejected action: {decision.reason}", "proposed_action": None}
        elif not decision.token:
            update["escalation_reason"] = "approved without a token"
        return update | {"timeline": [event("approval_gate", "approval", text)]}
    return approval_gate


def make_execute(deps: CopilotDeps):
    async def execute(state: IncidentState) -> dict[str, Any]:
        action, approval = state["proposed_action"], state["approval"]
        purpose = state.get("action_purpose", "fix")
        try:
            result = await deps.ops.execute(action, approval.token)
        except ToolError as exc:
            reason = f"execute failed: {exc}"
            return {"escalation_reason": reason, "budget": spend("remediation", tool_calls=1),
                    "timeline": [event("execute", "escalate", reason)]}

        update: dict[str, Any] = {"budget": spend("remediation", tool_calls=1)}
        if purpose == "rollback":
            # Our fix did not help and is now undone → refute the hypothesis it was based on, then replan.
            prev = state["outcome"]
            supported = [h for h in state.get("hypotheses", []) if h.status == "supported"]
            refuted = [h.model_copy(update={"status": "refuted", "evidence": [*h.evidence, EvidenceRef(
                evidence_id=f"action:{prev.approved_hash[:12]}", summary="approved fix did not recover the SLO")]})
                for h in supported]
            update |= {
                "hypotheses": refuted,
                "outcome": prev.model_copy(update={"rolled_back": True}),
                "replan_reason": "fix did not recover the metric; rolled back",
                "proposed_action": None,
                "timeline": [event("execute", "rollback", f"Rolled back with {action.model_dump_json()}: {result}")],
            }
        else:
            update |= {
                "outcome": ActionOutcome(action=action, approved_hash=approval.payload_hash, verified=False, rolled_back=False),
                "timeline": [event("execute", "action", f"Executed {action.model_dump_json()}: {result}")],
            }
        emit({"node": "execute", "purpose": purpose, "result": result})
        return update
    return execute


def make_verify(deps: CopilotDeps):
    async def verify(state: IncidentState) -> dict[str, Any]:
        """Code decides recovery: the alert's own query back under its threshold within the window."""
        alert, s = state["alert"], deps.settings
        poll_s = max(1, s.verify_poll_s)
        max_polls = max(1, s.verify_window_s // poll_s + 1)
        polls, value = 0, None
        while polls < max_polls:
            value = await deps.telemetry.query_scalar(alert.query)
            polls += 1
            emit({"node": "verify", "value": value, "threshold": alert.threshold})
            if value is not None and value < alert.threshold:
                break
            if polls < max_polls:
                await asyncio.sleep(poll_s)

        recovered = value is not None and value < alert.threshold
        outcome = state["outcome"].model_copy(update={"verified": recovered})
        text = f"{'recovered' if recovered else 'NOT recovered'}: {alert.query} = {value} (threshold {alert.threshold})"
        update: dict[str, Any] = {"outcome": outcome, "budget": spend("remediation", tool_calls=polls),
                                  "timeline": [event("verify", "verdict", text)]}
        if recovered:
            update["final_status"] = "recovered"
        return update
    return verify


def make_rollback(deps: CopilotDeps):
    async def rollback(state: IncidentState) -> dict[str, Any]:
        undo = state["dry_run"].rollback_action if state.get("dry_run") else None
        if undo is None:
            reason = "fix did not recover and no rollback action is available"
            return {"escalation_reason": reason, "timeline": [event("rollback", "escalate", reason)]}
        return {"proposed_action": undo, "action_purpose": "rollback", "approval": None,
                "expected_effect": "restore the state before our action",
                "timeline": [event("rollback", "rollback", f"Rolling back with {undo.model_dump_json()}")]}
    return rollback
