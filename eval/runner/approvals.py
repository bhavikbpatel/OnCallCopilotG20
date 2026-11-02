"""Automatic approval gate for evaluation runs.

In an experiment the runner stands in for the on-call engineer and answers every
approval request the same way (set by the run config). Every decision is recorded
with the payload hash it applies to, because Person 2's final state keeps only the
last approval.

The approver only decides: it names the exact payload it approved (``payload_hash``)
and issues no token. Person 2's approval gate refuses an approval whose hash is not
the proposal's, then mints the token itself with Person 3's helper (#37). So the
approver cannot know whether a token was issued, and records ``has_token: None``.
"""
from __future__ import annotations

from typing import Callable, Literal, Optional

from ..adapters.copilot_state import split_action
from ..schemas.result import payload_hash

Policy = Literal["approve", "reject"]


class AutoApprover:
    def __init__(self, policy: Policy, on_approved: Optional[Callable[[str, dict], None]] = None):
        """``on_approved(tool, args)`` runs for every approved action, before the agent executes it
        (the runner uses it to stop the injector from re-applying a flag the agent is fixing)."""
        if policy not in ("approve", "reject"):
            raise ValueError(f"unknown approval policy {policy!r}")
        self.policy = policy
        self.on_approved = on_approved
        self.approvals: list[dict] = []
        self.closed = False

    def close(self) -> None:
        """End of the run: every later request is rejected, so an agent that overran its
        deadline (its thread cannot be killed) can never change the shop afterwards."""
        self.closed = True

    def __call__(self, action: dict) -> dict:
        """Decide on one flat action ({"tool": ..., **args}); returns Person 2's ApprovalDecision fields."""
        if self.closed:
            return {"decision": "reject", "token": None, "decided_by": "auto",
                    "reason": "the evaluation run has ended; no further actions are approved"}
        tool, args = split_action(action)
        digest = payload_hash(tool, args)          # the same hash the gate computes (tested against Person 2's)
        self.approvals.append({"payload_hash": digest, "decision": self.policy, "decided_by": "auto",
                               "has_token": None})     # the gate mints the token; the approver never holds one
        if self.policy == "approve" and self.on_approved is not None:
            self.on_approved(tool, args)
        return {"decision": self.policy, "token": None, "decided_by": "auto", "payload_hash": digest,
                "reason": f"evaluation run: policy {self.policy}"}
