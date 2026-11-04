"""3.8–3.10 Propose action, approval gate, remediation (dry run → execute → verify → rollback).

Only this module touches the shop, and only `execute` writes — after an approved, hash-bound token.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from langgraph.types import interrupt
from pydantic import BaseModel, TypeAdapter

from copilot.agents.base import emit, event, guarded, now, spend, structured_call
from copilot.clients import ToolError
from copilot.deps import CopilotDeps
from copilot.hooked_toolbox import with_role
from copilot.memory.views import remediation_view
from copilot.policy import is_allowed
from copilot.state import (
    Action, ActionOutcome, ApprovalDecision, ApprovalRecord, EvidenceRef, IncidentState, RestartService, SetFlag,
    describe, payload_hash,
)
from eval.telemetry import signals

_action_adapter = TypeAdapter(Action)


class ActionProposal(BaseModel):
    action: Action | None                 # None → no safe action; escalate
    expected_effect: str
    rationale: str


def make_propose_action(deps: CopilotDeps):
    @guarded("propose_action")
    async def propose_action(state: IncidentState) -> dict[str, Any]:
        outcome = state.get("outcome")
        follow_up = state.get("action_purpose") == "follow_up" and state.get("follow_up_of") is not None \
            and outcome is not None and outcome.action == state["follow_up_of"]
        view = remediation_view(state) | {
            "fix_in_place_not_recovered": state["follow_up_of"].model_dump(mode="json") if follow_up else None,
            "last_rejection": state["approval"].reason
            if state.get("approval") and state["approval"].decision == "reject" else None,
            "already_failed_and_rolled_back": [outcome.action.model_dump(mode="json")]
            if outcome and outcome.rolled_back else [],
        }
        proposal, cost = await structured_call(deps, "remediation", "propose_action", ActionProposal, view)
        if proposal.action is None and follow_up:     # no complementary step → after_propose routes to rollback
            return {"proposed_action": None, "budget": cost,
                    "timeline": [event("propose_action", "rollback", f"No follow-up step: {proposal.rationale}")]}
        if proposal.action is None:
            reason = f"no safe action: {proposal.rationale}"
            return {"escalation_reason": reason, "budget": cost, "timeline": [event("propose_action", "escalate", reason)]}
        allowed, why = is_allowed(proposal.action)
        if allowed and payload_hash(proposal.action) in state.get("failed_actions", []):
            allowed, why = False, f"{proposal.action.model_dump_json()} already failed and was rolled back"
        if allowed and follow_up and not _valid_follow_up(proposal.action, state):
            allowed, why = False, f"follow-up must be a restart of {state['alert'].service}, " \
                                  f"got {proposal.action.model_dump_json()}"
        if not allowed and follow_up:                 # unusable follow-up → undo the fix rather than leave it half-done
            return {"proposed_action": None, "budget": cost,
                    "timeline": [event("propose_action", "rollback", f"Follow-up refused: {why}")]}
        if not allowed:
            return {"escalation_reason": why, "budget": cost, "timeline": [event("propose_action", "escalate", why)]}
        return {
            "proposed_action": proposal.action, "action_purpose": "follow_up" if follow_up else "fix", "expected_effect": proposal.expected_effect,
            "approval": None, "budget": cost,
            "timeline": [event("propose_action", "action", f"Proposed {proposal.action.model_dump_json()}")],
        }
    return propose_action


def make_dry_run(deps: CopilotDeps):
    @with_role("remediation")
    @guarded("dry_run")
    async def dry_run(state: IncidentState) -> dict[str, Any]:
        action = state["proposed_action"]
        result = await deps.ops.dry_run(action)
        return {"dry_run": result, "budget": spend("remediation", code_calls=1),      # code-driven: not budgeted
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
        record = ApprovalRecord(payload_hash=digest, decision=decision.decision, token=None,
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
            elif state.get("action_purpose") == "follow_up":
                update["proposed_action"] = None            # after_approval → roll back the fix still in place
            else:
                update |= {"replan_reason": f"engineer rejected action: {decision.reason}", "proposed_action": None}
        elif decision.payload_hash != digest:
            # Approval is bound to the exact payload the approver saw: a missing or different hash is refused.
            why = ("approval_without_payload_hash" if not decision.payload_hash else
                   f"stale_approval: approved {decision.payload_hash[:12]}..., proposal is {digest[:12]}...")
            update["escalation_reason"] = f"approval refused: {why}"
            text += f" (refused: {why})"
        else:
            token, why = mint_token(action, digest, decision.decided_by or "engineer", decision.payload_hash)
            if token is None and decision.token and why.startswith("approval_not_configured"):
                token, why = decision.token, ""          # no APPROVAL_SECRET here: the approver's token; the server checks it
                text += " (token from the approver)"
            if token is None:
                update["escalation_reason"] = f"approval token not issued: {why}"
            update["approval"] = record.model_copy(update={"token": token})
        return update | {"timeline": [event("approval_gate", "approval", text)]}
    return approval_gate


def mint_token(action: Action, digest: str, decided_by: str, shown_hash: str) -> tuple[str | None, str]:
    """The gate mints the token for this exact action with Person 3's helper (APPROVAL_SECRET); an approver
    only decides. ``shown_hash`` is the hash the approver approved; resolve() refuses it as stale_approval if it
    is not the proposal's. Returns (token, "") or (None, reason)."""
    from mcp_servers.common.decision import propose, resolve

    try:
        proposal = propose(action.model_dump(mode="json"))
    except ValueError as exc:
        return None, f"refused by the ops allow-list: {exc}"
    if proposal["payload_hash"] != digest:
        return None, "payload hash differs between the graph and the ops server"
    out = resolve(proposal, {"decision": "approve", "decided_by": decided_by, "payload_hash": shown_hash})
    return out["token"], "" if out["token"] else str(out.get("reason"))


def make_execute(deps: CopilotDeps):
    @with_role("remediation")
    async def execute(state: IncidentState) -> dict[str, Any]:
        action, approval = state["proposed_action"], state["approval"]
        purpose = state.get("action_purpose", "fix")
        try:
            result = await deps.ops.execute(action, approval.token)
        except ToolError as exc:
            reason = f"execute failed: {exc}"
            return {"escalation_reason": reason, "budget": spend("remediation", code_calls=1),
                    "timeline": [event("execute", "escalate", reason)]}

        update: dict[str, Any] = {"budget": spend("remediation", code_calls=1)}   # the approved write: code, not budgeted
        if purpose == "rollback":
            # Our fix did not help and is now undone → refute the hypothesis it was based on, then replan.
            prev = state["outcome"]
            supported = [h for h in state.get("hypotheses", []) if h.status == "supported"]
            refuted = [h.model_copy(update={"status": "refuted", "evidence": [*h.evidence, EvidenceRef(
                evidence_id=f"action:{prev.approved_hash[:12]}", summary="approved fix did not recover the SLO")]})
                for h in supported]
            first = state.get("follow_up_of")
            update |= {
                "hypotheses": refuted,
                "failed_actions": [prev.approved_hash, *([payload_hash(first)] if first else [])],
                "follow_up_of": None, "undo_action": None,
                "outcome": prev.model_copy(update={"rolled_back": True}),
                "replan_reason": "fix did not recover the metric; rolled back",
                "proposed_action": None,
                "timeline": [event("execute", "rollback", f"Rolled back: {describe(action)}")],
            }
        else:
            # A follow-up (e.g. restart) usually has no undo; keep the first fix's undo so rollback restores the start.
            if state["dry_run"].rollback_action is not None and not (purpose == "follow_up" and state.get("undo_action")):
                update["undo_action"] = state["dry_run"].rollback_action
            update |= {
                "executed_at": now(),
                "outcome": ActionOutcome(action=action, approved_hash=approval.payload_hash, verified=False, rolled_back=False),
                "timeline": [event("execute", "action", f"Executed: {describe(action)}")],
            }
        emit({"node": "execute", "purpose": purpose, "result": result})
        return update
    return execute


SIGNAL_ALIASES = {"latency_p95": "latency_p95_ms", "cpu": "cpu_percent", "memory": "memory_mb"}
HIGHER_IS_HEALTHY = {"throughput_rps"}


def _signal(alert_signal: str) -> str:
    return SIGNAL_ALIASES.get(alert_signal, alert_signal)


def _healthy(signal: str, value: float, threshold: float) -> bool:
    return value > threshold if _signal(signal) in HIGHER_IS_HEALTHY else value < threshold


def follow_up_allowed(state: IncidentState, follow_up_signals: list[str]) -> bool:
    """A second step only for leftover state: flag turned off but memory still high (emailMemoryLeak).
    Anything else that did not recover is rolled back — a guessed restart can be a forbidden action."""
    fix = state["outcome"].action
    return (state.get("follow_up_of") is None and _signal(state["alert"].signal) in follow_up_signals
            and isinstance(fix, SetFlag) and fix.variant == "off")


def _valid_follow_up(action: Action, state: IncidentState) -> bool:
    return isinstance(action, RestartService) and str(action.service) == state["alert"].service


async def _measure(deps: CopilotDeps, state: IncidentState) -> tuple[float | None, bool, str, int]:
    """One recovery reading → (value, judgeable, description, tool calls).

    error_rate counts errors from settle_s after the fix (checkout sees ~2.5 requests/min, too few for a fixed
    window; errors from before the fix keep arriving for a while, so the first settle_s are skipped);
    other signals use the agreed signal map. Unknown signals fall back to the alert rule's own query.
    """
    alert, s = state["alert"], deps.settings
    signal = SIGNAL_ALIASES.get(alert.signal, alert.signal)
    if signal == "error_rate":
        since = state["executed_at"] + timedelta(seconds=s.verify_settle_s)
        c = await deps.telemetry.errors_since(alert.service, since)
        judgeable = c.error_rate is not None and (c.requests or 0) >= s.verify_min_requests
        return c.error_rate, judgeable, f"{alert.service} error_rate since fix = {c.error_rate} " \
                                        f"({c.errors}/{c.requests} requests)", 2
    if signal in signals.SIGNALS:
        value = await deps.telemetry.signal_value(signal, alert.service)
        return value, value is not None, f"{alert.service} {signal} = {value}", 1
    value = await deps.telemetry.query_scalar(alert.query)
    return value, value is not None, f"{alert.query} = {value}", 1


def make_verify(deps: CopilotDeps):
    @with_role("remediation")
    async def verify(state: IncidentState) -> dict[str, Any]:
        """Code decides recovery. Never rolls back on missing data: too little traffic → escalate."""
        alert, s = state["alert"], deps.settings
        poll_s = max(1, s.verify_poll_s)
        window = s.verify_window_by_signal.get(_signal(alert.signal), s.verify_window_s)
        first = s.verify_settle_s + s.verify_initial_delay_s          # settled, and Prometheus has caught up
        max_polls = max(1, (window - first) // poll_s + 1)
        await asyncio.sleep(first)
        calls, polls = 0, 0
        value, judgeable, desc = None, False, ""
        while polls < max_polls:
            value, judgeable, desc, n = await _measure(deps, state)
            calls, polls = calls + n, polls + 1
            emit({"node": "verify", "reading": desc, "threshold": alert.threshold})
            if judgeable and _healthy(alert.signal, value, alert.threshold):
                break
            if polls < max_polls:
                await asyncio.sleep(poll_s)

        recovered = judgeable and _healthy(alert.signal, value, alert.threshold)
        update: dict[str, Any] = {"outcome": state["outcome"].model_copy(update={"verified": recovered}),
                                  # Verify's readings are code-driven: logged as code_calls, outside the
                                  # agent's tool-call budget, so E4's budget 5 is not used up by polling.
                                  "budget": spend("remediation", code_calls=calls)}
        if recovered:
            text = f"recovered: {desc} (threshold {alert.threshold})"
            update["final_status"] = "recovered"
        elif judgeable and follow_up_allowed(state, s.follow_up_signals):
            # Leftover state (emailMemoryLeak: flag off, memory still high): keep the fix, allow one restart.
            text = f"NOT recovered: {desc} (threshold {alert.threshold}); fix kept, one follow-up step allowed"
            update |= {"follow_up_of": state["outcome"].action, "action_purpose": "follow_up"}
        elif judgeable:
            text = f"NOT recovered: {desc} (threshold {alert.threshold})"
        else:
            text = f"recovery could not be judged (too little data): {desc}"
            update["escalation_reason"] = text
        return update | {"timeline": [event("verify", "verdict", text)]}
    return verify


def make_rollback(deps: CopilotDeps):
    async def rollback(state: IncidentState) -> dict[str, Any]:
        undo = state.get("undo_action")
        if undo is None:
            reason = "fix did not recover and no rollback action is available"
            return {"escalation_reason": reason, "timeline": [event("rollback", "escalate", reason)]}
        return {"proposed_action": undo, "action_purpose": "rollback", "approval": None,
                "expected_effect": "restore the state before our action",
                "timeline": [event("rollback", "rollback", f"Rolling back with {undo.model_dump_json()}")]}
    return rollback
