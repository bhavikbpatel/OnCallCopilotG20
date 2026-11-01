"""Automatic approval gate for evaluation runs.

In an experiment the runner stands in for the on-call engineer and answers every
approval request the same way (set by the run config). Every decision is recorded
with the payload hash it applies to, because Person 2's final state keeps only the
last approval. Tokens are random, single-use strings and are never written to results.
"""
from __future__ import annotations

import uuid
from typing import Literal

from ..adapters.copilot_state import split_action
from ..schemas.result import payload_hash

Policy = Literal["approve", "reject"]


class AutoApprover:
    def __init__(self, policy: Policy):
        if policy not in ("approve", "reject"):
            raise ValueError(f"unknown approval policy {policy!r}")
        self.policy = policy
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
        digest = payload_hash(tool, args)
        token = f"auto-{uuid.uuid4().hex}" if self.policy == "approve" else None
        self.approvals.append({"payload_hash": digest, "decision": self.policy, "decided_by": "auto",
                               "has_token": token is not None})
        return {"decision": self.policy, "token": token, "decided_by": "auto",
                "reason": f"evaluation run: policy {self.policy}"}
