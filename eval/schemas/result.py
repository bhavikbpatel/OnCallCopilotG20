"""Result line: what every run of the copilot must produce for scoring.

One JSON object per run, one line per object (JSONL). Persons 2 and 3 emit the
agent-side fields; the runner adds run metadata and the independent recovery
observation. The tool-call log (from Person 3's hooks) is the single source of
truth for what was actually executed.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .catalogue import AGENT_ROLES

# Keys that are not part of what was approved (tokens, bookkeeping).
_NON_PAYLOAD_KEYS = frozenset({"approval_token", "dry_run", "payload_hash"})


def canonical_action(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    clean = {k: v for k, v in args.items() if k not in _NON_PAYLOAD_KEYS}
    return {"tool": tool, "args": clean}


def payload_hash(tool: str, args: dict[str, Any]) -> str:
    """sha256 of canonical JSON (sorted keys, no spaces) of {tool, args}.

    Proposed shared definition with Person 3: ops-actions and the approval gate
    should hash exactly this, so the checker can confirm an approval matches the
    command that ran.
    """
    blob = json.dumps(canonical_action(tool, args), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


class _Open(BaseModel):
    # Unknown extra fields are kept, so teammates can add fields without breaking scoring.
    model_config = ConfigDict(extra="allow")


class Hypothesis(_Open):
    rank: int = Field(ge=1)
    service: str
    fault_type: Optional[str] = None
    status: Literal["open", "supported", "refuted"]
    evidence_ids: list[str] = Field(default_factory=list)


class SeverityResult(_Open):
    level: Literal["SEV1", "SEV2", "SEV3", "NONE"]
    rule: Optional[str] = None


class ProposedAction(_Open):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    payload_hash: Optional[str] = None


class ToolCall(_Open):
    """One entry from Person 3's hooks log, including blocked calls."""

    seq: int = Field(ge=0)
    agent: str  # instance name, e.g. "diagnosis-payment"
    agent_role: str
    server: str
    tool: str
    kind: Literal["read", "write"]
    args: dict[str, Any] = Field(default_factory=dict)
    payload_hash: Optional[str] = None  # set for writes and dry runs
    ok: bool
    blocked: bool = False
    block_reason: Optional[str] = None
    action_id: Optional[str] = None  # returned by ops-actions for executed writes

    @field_validator("agent_role")
    @classmethod
    def _role(cls, v: str) -> str:
        if v not in AGENT_ROLES:
            raise ValueError(f"unknown agent_role {v!r}; expected one of {sorted(AGENT_ROLES)}")
        return v


class Approval(_Open):
    payload_hash: str
    decision: Literal["approved", "edited", "rejected"]
    decided_by: Literal["human", "auto"] = "auto"


class Verification(_Open):
    agent_claims_recovered: Optional[bool] = None
    rolled_back: bool = False


class Budget(_Open):
    max_tool_calls: int = Field(gt=0)


class Cost(_Open):
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0


class RecoveryObservation(BaseModel):
    """Filled by the runner from Prometheus, never by the agent."""

    model_config = ConfigDict(extra="forbid")
    checked: bool = False
    recovered: Optional[bool] = None
    value: Optional[float] = None
    query: Optional[str] = None


class ResultLine(_Open):
    # Run metadata (runner)
    run_id: str
    case_id: str
    config_id: str  # e.g. "E1-C2"
    repeat: int = Field(1, ge=1)
    agent_version: str = "unknown"
    config_hash: Optional[str] = None
    status: Literal["completed", "budget_exhausted", "timeout", "error"]
    error: Optional[str] = None
    started_at: dt.datetime
    ended_at: dt.datetime
    trace_id: Optional[str] = None

    # Agent outcome (Person 2: final graph state)
    verdict: Literal["incident", "noise", "undetermined"]
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    severity: Optional[SeverityResult] = None
    proposed_actions: list[ProposedAction] = Field(default_factory=list)
    verification: Verification = Field(default_factory=Verification)
    replans: int = 0
    lessons_used: list[str] = Field(default_factory=list)
    lessons_rejected: list[str] = Field(default_factory=list)

    # Control log (Person 3: hooks and approval gate)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    approvals: list[Approval] = Field(default_factory=list)
    budget: Budget

    # Cost (Person 5: tracing)
    cost: Cost = Field(default_factory=Cost)

    # Independent check (runner)
    recovery: RecoveryObservation = Field(default_factory=RecoveryObservation)

    @property
    def latency_s(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()
