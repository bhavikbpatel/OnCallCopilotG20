"""Typed incident state (short-term memory). See agent_graph_design.md §4.1."""
from __future__ import annotations

import hashlib
import json
import operator
import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, model_validator
from typing_extensions import TypedDict

from copilot.memory.schemas import MemoryContext, Precondition
from eval.schemas import catalogue

EVIDENCE_PER_HYPOTHESIS = 5
MESSAGES_KEPT = 20


# ---------- enums (generated from the eval fault catalogue: one source of truth for names) ----------

def _enum_name(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).replace("-", "_").upper()


def _str_enum(name: str, values: list[str]) -> type[StrEnum]:
    return StrEnum(name, {_enum_name(v): v for v in values}, module=__name__)


# Shop services as they appear in telemetry (service_name / container_name), e.g. "valkey-cart".
ServiceName = _str_enum("ServiceName", sorted(catalogue.SERVICES))
# Every flagd flag in the pinned shop; set_flag may only touch these.
FlagName = _str_enum("FlagName", sorted(catalogue.FLAG_VARIANTS))
# Golden-set fault types (scored against the case spec) plus "noise" for no-fault alerts.
FaultType = _str_enum("FaultType", [*catalogue.FAULT_TYPES, "noise"])


class FaultFamily(StrEnum):
    FEATURE_FLAG = "feature_flag"
    BAD_DEPLOY = "bad_deploy"
    DEPENDENCY_LATENCY = "dependency_latency"
    RESOURCE_SATURATION = "resource_saturation"
    NONE = "none"


class Severity(StrEnum):
    SEV1 = "SEV1"
    SEV2 = "SEV2"
    SEV3 = "SEV3"
    SEV4 = "SEV4"


# ---------- input ----------

class Alert(BaseModel):
    alert_name: str
    service: str
    signal: str
    summary: str
    query: str
    threshold: float
    value: float
    fired_at: datetime
    fingerprint: str


# ---------- plan, hypotheses, evidence ----------

class PlanStep(BaseModel):
    name: Literal["scope", "hypotheses", "test", "act", "verify", "communicate"]
    note: str = ""


class IncidentPlan(BaseModel):
    version: int
    steps: list[PlanStep]
    assumptions: list[str] = Field(default_factory=list)
    reason: str = ""                # why this version exists (initial / replan cause)
    created_at: datetime


class EvidenceRef(BaseModel):
    evidence_id: str
    summary: str


class Hypothesis(BaseModel):
    id: str
    service: ServiceName
    fault_type: FaultType
    status: Literal["open", "supported", "refuted"] = "open"
    evidence: list[EvidenceRef] = Field(default_factory=list)
    tool_calls_used: int = 0
    rationale: str = ""
    contradicts_plan: bool = False     # a result contradicted a plan assumption (Signature A)


class EvidenceItem(BaseModel):
    evidence_id: str                # promql+time range | trace_id | log doc id
    hypothesis_id: str
    summary: str                    # one line; never raw telemetry
    supports: bool | None = None    # True supports, False contradicts, None neutral
    at: datetime


class TimelineEvent(BaseModel):
    at: datetime
    node: str
    kind: Literal["plan", "replan", "tool", "verdict", "action", "approval", "rollback", "comms", "memory",
                  "triage", "escalate"]
    text: str


# ---------- hand-offs ----------

class TriageReport(BaseModel):
    severity: Severity
    is_noise: bool = False
    suspects: list[ServiceName]
    impact: str
    changes: list[str] = Field(default_factory=list)


class RunbookAdvice(BaseModel):
    runbook_id: str | None
    candidate_action: "Action | None" = None
    preconditions_met: list[str] = Field(default_factory=list)
    rejected_lessons: list[str] = Field(default_factory=list)


# ---------- actions ----------

class SetFlag(BaseModel):
    tool: Literal["set_flag"] = "set_flag"
    flag: FlagName
    variant: str                       # "off" or one of the flag's variants, e.g. "50%" (needed to undo)

    @model_validator(mode="after")
    def _known_variant(self) -> "SetFlag":
        allowed = ("off", *catalogue.FLAG_VARIANTS[self.flag])
        if self.variant not in allowed:
            raise ValueError(f"variant {self.variant!r} not valid for {self.flag}; allowed {allowed}")
        return self


class RestartService(BaseModel):
    tool: Literal["restart_service"] = "restart_service"
    service: ServiceName


class ScaleService(BaseModel):
    tool: Literal["scale_service"] = "scale_service"
    service: ServiceName
    replicas: int = Field(ge=1, le=3)


class Rollback(BaseModel):
    tool: Literal["rollback"] = "rollback"
    service: ServiceName
    to_version: str


Action = Annotated[Union[SetFlag, RestartService, ScaleService, Rollback], Field(discriminator="tool")]
RunbookAdvice.model_rebuild()


def describe(action: BaseModel) -> str:
    """Plain words for a status note: 'set flag paymentFailure to off', 'restarted email'."""
    a = action
    return {"set_flag": lambda: f"set flag {a.flag} to {a.variant}",
            "restart_service": lambda: f"restarted {a.service}",
            "scale_service": lambda: f"scaled {a.service} to {a.replicas} replicas",
            "rollback": lambda: f"rolled {a.service} back to {a.to_version}"}[a.tool]()


def payload_hash(action: BaseModel) -> str:
    """sha256 of canonical JSON; must match the ops-actions server's computation."""
    canonical = json.dumps(action.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class HypothesisResult(BaseModel):
    """Diagnosis → Commander hand-off."""
    hypothesis_id: str
    verdict: Literal["supported", "refuted", "inconclusive"]
    evidence: list[EvidenceRef] = Field(default_factory=list)
    contradicts_plan: bool = False
    confidence_basis: str = ""


class DryRunResult(BaseModel):
    """Returned by ops-actions.dry_run. rollback_action is computed server-side from current state."""
    would_do: str
    current_state: dict[str, Any] = Field(default_factory=dict)
    expected_effect: str
    rollback_action: Action | None = None


class ApprovalRecord(BaseModel):
    payload_hash: str
    decision: Literal["approve", "edit", "reject"]
    token: str | None = None
    reason: str = ""
    decided_by: str = ""
    decided_at: datetime


class ApprovalDecision(BaseModel):
    """Resume payload for the approval gate: Command(resume=ApprovalDecision(...).model_dump())."""
    decision: Literal["approve", "edit", "reject"]
    edited_action: dict[str, Any] | None = None
    token: str | None = None          # ignored when the gate can mint (APPROVAL_SECRET set); see mint_token
    payload_hash: str | None = None   # the hash of the action the approver saw; an approval must carry it and it
                                      # must equal the gate's (approval is bound to the exact payload)
    reason: str = ""
    decided_by: str = ""


class ActionOutcome(BaseModel):
    action: Action
    approved_hash: str
    verified: bool
    rolled_back: bool


class StatusNote(BaseModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)


class ProposedLesson(BaseModel):
    text: str
    service: str
    fault_family: str
    preconditions: list[Precondition]


class Postmortem(BaseModel):
    summary: str
    root_cause: str
    action: Action | None
    timeline: list[TimelineEvent]
    lessons_proposed: list[ProposedLesson] = Field(default_factory=list)


class LessonDecision(BaseModel):
    index: int                        # position in postmortem.lessons_proposed
    decision: Literal["accept", "edit", "reject"]
    edited: ProposedLesson | None = None
    reason: str = ""


class ReviewDecision(BaseModel):
    """Resume payload for the review gate."""
    accept_postmortem: bool
    edited_root_cause: str | None = None
    lessons: list[LessonDecision] = Field(default_factory=list)
    reviewed_by: str


# ---------- budget ----------

class AgentSpend(BaseModel):
    tool_calls: int = 0      # the agent's own calls: these count toward limits.tool_calls (E4 budgets 5/10/20)
    tokens: int = 0
    usd: float = 0.0
    code_calls: int = 0      # calls code makes by itself (verify polling Prometheus): logged, not budgeted
                             # (docs/eval-result-contract.md: driver "code" calls do not count)

    def __add__(self, other: "AgentSpend") -> "AgentSpend":
        return AgentSpend(
            tool_calls=self.tool_calls + other.tool_calls,
            tokens=self.tokens + other.tokens,
            usd=self.usd + other.usd,
            code_calls=self.code_calls + other.code_calls,
        )


class BudgetLedger(BaseModel):
    """Spend so far. Nodes return a *delta* ledger; `sum_ledgers` adds it."""
    by_agent: dict[str, AgentSpend] = Field(default_factory=dict)

    @property
    def total(self) -> AgentSpend:
        out = AgentSpend()
        for s in self.by_agent.values():
            out = out + s
        return out


class BudgetLimits(BaseModel):
    tool_calls: int = 20
    tokens: int = 200_000
    usd: float = 2.0
    per_diagnosis_tool_calls: int = 6


# ---------- reducers ----------

def merge_by_id(left: list[Hypothesis] | None, right: list[Hypothesis] | None) -> list[Hypothesis]:
    """Parallel Diagnosis branches each update their own hypothesis; right wins per id, order kept."""
    merged = {h.id: h for h in (left or [])}
    for h in right or []:
        merged[h.id] = h
    return list(merged.values())


def keep_last_n_per_hypothesis(left: list[EvidenceItem] | None, right: list[EvidenceItem] | None) -> list[EvidenceItem]:
    """Append, dedupe by evidence_id, keep only the last N items per hypothesis."""
    seen: dict[tuple[str, str], EvidenceItem] = {}
    for e in (left or []) + (right or []):
        seen.pop((e.hypothesis_id, e.evidence_id), None)
        seen[(e.hypothesis_id, e.evidence_id)] = e
    items = list(seen.values())
    counts: dict[str, int] = {}
    kept: list[EvidenceItem] = []
    for e in reversed(items):
        if counts.get(e.hypothesis_id, 0) < EVIDENCE_PER_HYPOTHESIS:
            counts[e.hypothesis_id] = counts.get(e.hypothesis_id, 0) + 1
            kept.append(e)
    return list(reversed(kept))


def sum_ledgers(left: BudgetLedger | None, right: BudgetLedger | None) -> BudgetLedger:
    out = dict((left or BudgetLedger()).by_agent)
    for agent, spend in (right or BudgetLedger()).by_agent.items():
        out[agent] = out.get(agent, AgentSpend()) + spend
    return BudgetLedger(by_agent=out)


def add_and_trim(left: list[AnyMessage] | None, right: list[AnyMessage] | None) -> list[AnyMessage]:
    """Commander message log: standard add_messages, then keep the last MESSAGES_KEPT.
    Older context lives in `evidence_summary`, not here."""
    return add_messages(left or [], right or [])[-MESSAGES_KEPT:]


# ---------- state ----------

class IncidentState(TypedDict, total=False):
    incident_id: str
    run_id: str | None                     # eval run namespace; None in production
    engineer_id: str | None                # on-call engineer, for prefs
    alert: Alert
    severity: Severity | None              # set by code from the policy table
    triage: TriageReport | None
    plan: IncidentPlan
    plan_history: Annotated[list[IncidentPlan], operator.add]
    hypotheses: Annotated[list[Hypothesis], merge_by_id]
    evidence: Annotated[list[EvidenceItem], keep_last_n_per_hypothesis]
    evidence_summary: str                  # running summary, rewritten by Commander-assess
    timeline: Annotated[list[TimelineEvent], operator.add]
    memory_context: MemoryContext | None
    replans: int
    replan_reason: str | None              # set by assess / approval-reject / rollback; consumed by hypothesize
    runbook_advice: RunbookAdvice | None
    proposed_action: Action | None
    action_purpose: Literal["fix", "follow_up", "rollback"]
    follow_up_of: Action | None            # fix kept in place while a complementary step is tried (flag off → restart)
    undo_action: Action | None             # restores the state before this attempt; set from the first undoable dry run
    expected_effect: str
    dry_run: DryRunResult | None
    approval: ApprovalRecord | None
    executed_at: datetime | None           # when the fix ran; recovery counts errors since then
    failed_actions: Annotated[list[str], operator.add]   # payload hashes of fixes that were rolled back
    outcome: ActionOutcome | None
    status_note: StatusNote | None
    postmortem: Postmortem | None
    escalation_reason: str | None
    final_status: Literal["recovered", "rolled_back", "escalated", "noise"] | None
    budget: Annotated[BudgetLedger, sum_ledgers]
    limits: BudgetLimits
    messages: Annotated[list[AnyMessage], add_and_trim]


class DiagnosisInput(TypedDict):
    """Payload of each Send("diagnosis", ...) — one per open hypothesis."""
    hypothesis: Hypothesis
    alert: Alert
    triage: TriageReport | None
    evidence: list[EvidenceItem]
    tool_call_budget: int
