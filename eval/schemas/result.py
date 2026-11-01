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

from .catalogue import AGENT_ROLES, normalize_severity

# Keys that are not part of what was approved (tokens, bookkeeping).
_NON_PAYLOAD_KEYS = frozenset({"approval_token", "dry_run", "payload_hash"})


def canonical_action(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """The action as Person 2's models dump it: one flat object, tool included.

    e.g. ``{"tool": "set_flag", "flag": "paymentFailure", "variant": "off"}``.
    """
    clean = {k: v for k, v in args.items() if k not in _NON_PAYLOAD_KEYS and k != "tool"}
    return {"tool": tool, **clean}


def payload_hash(tool: str, args: dict[str, Any]) -> str:
    """sha256 hex of canonical JSON (sorted keys, no spaces) of the flat action.

    Identical to Person 2's ``copilot.state.payload_hash(action)``, which hashes
    ``action.model_dump(mode="json")``; the approval gate, ops-actions and the
    checker therefore agree on which command an approval belongs to.
    """
    blob = json.dumps(canonical_action(tool, args), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


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
    level: Literal["SEV1", "SEV2", "SEV3", "SEV4", "NONE"]
    rule: Optional[str] = None

    @field_validator("level", mode="before")
    @classmethod
    def _norm(cls, v: Any) -> Any:
        return normalize_severity(v) if isinstance(v, str) else v


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
    # Person 2's graph undoes a fix that did not verify by executing the dry run's
    # rollback_action with purpose "rollback" (often a set_flag, not the rollback tool).
    purpose: Optional[Literal["fix", "rollback"]] = None

    @field_validator("agent_role")
    @classmethod
    def _role(cls, v: str) -> str:
        if v not in AGENT_ROLES:
            raise ValueError(f"unknown agent_role {v!r}; expected one of {sorted(AGENT_ROLES)}")
        return v


class Approval(_Open):
    """One approval-gate decision (Person 2: ApprovalRecord / ApprovalDecision)."""

    payload_hash: str
    decision: Literal["approve", "edit", "reject"]
    decided_by: str = "auto"  # "auto" (runner) or the engineer's name
    # Whether a token came with the approval. The token itself is never stored.
    has_token: Optional[bool] = None


class Verification(_Open):
    agent_claims_recovered: Optional[bool] = None
    rolled_back: bool = False


class Budget(_Open):
    max_tool_calls: int = Field(gt=0)


class Cost(_Open):
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_total: int = 0  # Person 2's budget ledger counts tokens without an in/out split
    usd: float = 0.0


class RecoveryObservation(BaseModel):
    """Filled by the runner from Prometheus (eval/recovery), never by the agent.

    ``recovered`` is None when recovery could not be judged (no data, too little
    traffic, no baseline, Prometheus unreachable); ``reason`` then says why.
    "No data" is never counted as healthy.
    """

    model_config = ConfigDict(extra="forbid")
    checked: bool = False
    recovered: Optional[bool] = None
    value: Optional[float] = None
    query: Optional[str] = None
    signal: Optional[str] = None
    service: Optional[str] = None
    measure: Optional[Literal["window", "since_fix"]] = None
    op: Optional[Literal["lt", "gt"]] = None
    limit: Optional[float] = None  # the threshold, or baseline_factor x baseline
    baseline: Optional[float] = None  # measured just before injection (baseline form only)
    errors: Optional[float] = None  # since_fix only
    requests: Optional[float] = None  # since_fix only
    points: list[tuple[float, float]] = Field(default_factory=list)  # (unix time, value), distinct readings
    reason: Optional[str] = None
    observed_at: Optional[float] = None  # unix time of the reading the verdict rests on


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
    # Person 2's IncidentState.final_status / escalation_reason, kept as reported.
    final_status: Optional[Literal["recovered", "rolled_back", "escalated", "noise"]] = None
    escalation_reason: Optional[str] = None
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
