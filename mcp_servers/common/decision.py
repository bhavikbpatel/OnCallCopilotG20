"""Token minting for the approval gate: one decision about one exact action.

This is a helper, not a second gate. The gate node is Person 2's (copilot/agents/remediation.py); it calls
propose() and resolve() here. The graph (Person 2) pauses with interrupt(proposal) and resumes with a decision; the
runner approves automatically in evaluation. Both call resolve(), so the rules are the same:

  approve -> a token for THIS payload hash (10-minute expiry, single use at the server)
  edit    -> a new action means a new hash: the old token does not verify; the graph
             must dry_run the edited action and ask again
  reject  -> no token; the reason goes back to the commander, which replans

Inside the graph's gate node:
    proposal = propose(action, expected_effect=..., rollback_plan=..., rollback_action=...)
    decision = interrupt(proposal)            # engineer: {"decision", "decided_by", "payload_hash", "edited_action"?}
    outcome  = resolve(proposal, decision)    # outcome["token"] goes to the tool call; outcome["record"] to approvals
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from eval.schemas import payload_hash

from mcp_servers.common.approval import DEFAULT_TTL_S, ApprovalConfigError, make_token
from mcp_servers.ops_actions.allowlist import split_action, validate_action

DECISIONS = ("approve", "edit", "reject")


def propose(action: Dict[str, Any], expected_effect: str = "", rollback_plan: str = "",
            rollback_action: Optional[Dict[str, Any]] = None, verify_query: Optional[str] = None) -> Dict[str, Any]:
    """What the engineer is shown. The hash is over the flat action and nothing else."""
    tool, args = split_action(action)
    if tool != "publish_status":
        problem = validate_action(action)
        if problem:
            raise ValueError("%s: %s" % problem)
    return {"action": dict({"tool": tool}, **args), "payload_hash": payload_hash(str(tool), args),
            "expected_effect": expected_effect, "rollback_plan": rollback_plan,
            "rollback_action": rollback_action, "verify_query": verify_query}


def _record(digest: str, decision: str, who: str, has_token: bool) -> Dict[str, Any]:
    # Matches eval.schemas.result.Approval. The token itself is never stored in the record.
    return {"payload_hash": digest, "decision": decision, "decided_by": who, "has_token": has_token}


def resolve(proposal: Dict[str, Any], decision: Dict[str, Any], secret: Optional[str] = None,
            now: Optional[float] = None, ttl_s: int = DEFAULT_TTL_S) -> Dict[str, Any]:
    """Turn the engineer's decision into {status, record, token, next_proposal, reason}."""
    digest = proposal["payload_hash"]
    kind = decision.get("decision")
    who = str(decision.get("decided_by") or "unknown")
    base = {"token": None, "next_proposal": None, "reason": decision.get("reason")}
    if kind not in DECISIONS:
        return dict(base, status="rejected", reason="bad_decision", record=_record(digest, "reject", who, False))
    if kind == "reject":
        return dict(base, status="rejected", record=_record(digest, "reject", who, False))
    if kind == "edit":
        edited = decision.get("edited_action")
        if not isinstance(edited, dict):
            return dict(base, status="rejected", reason="edit_without_action", record=_record(digest, "reject", who, False))
        try:
            new = propose(edited, proposal.get("expected_effect", ""), proposal.get("rollback_plan", ""),
                          proposal.get("rollback_action"), proposal.get("verify_query"))
        except ValueError as exc:
            return dict(base, status="rejected", reason="edited_action_invalid: %s" % exc,
                        record=_record(digest, "reject", who, False))
        return dict(base, status="edited", next_proposal=new, record=_record(digest, "edit", who, False))
    # approve
    shown = decision.get("payload_hash")
    if shown and shown != digest:
        return dict(base, status="rejected", reason="stale_approval", record=_record(digest, "reject", who, False))
    try:
        token = make_token(digest, ttl_s, secret, now)
    except ApprovalConfigError as exc:
        return dict(base, status="rejected", reason="approval_not_configured: %s" % exc,
                    record=_record(digest, "reject", who, False))
    return dict(base, status="approved", token=token, record=_record(digest, "approve", who, True))


def auto_decision(proposal: Dict[str, Any], decision: str = "approve", decided_by: str = "auto",
                  edited_action: Optional[Dict[str, Any]] = None, reason: Optional[str] = None) -> Dict[str, Any]:
    """The runner's stand-in for the engineer in evaluation (decided_by 'auto')."""
    out = {"decision": decision, "decided_by": decided_by, "payload_hash": proposal["payload_hash"]}
    if edited_action is not None:
        out["edited_action"] = edited_action
    if reason:
        out["reason"] = reason
    return out
