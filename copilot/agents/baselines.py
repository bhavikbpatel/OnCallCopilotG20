"""E1 baselines (System Design: "Single-agent baseline (C1) … C0 is a fixed workflow").

Both only choose the action. Everything after it (dry run, approval gate, execute, verify, rollback) and before it
(intake, triage: severity and noise are code) is the same node C2 uses, so E1 varies only how the action is chosen.

- C0 `fixed_runbook`: runbook by alert name → the first actionable runbook's typed fix. No model, no decisions.
- C1 `single_agent`: one ReAct agent with every read tool (permissions.yaml `single_agent`) finds the cause and
  proposes the fix; same evidence rule, allow-list and failed-action block as C2.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional, Union

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field, TypeAdapter

from copilot.agents.base import UsageMeter, emit, event, guarded, load_prompt, now, render_data, spend
from copilot.agents.diagnosis import grounded, tool_strings
from copilot.deps import CopilotDeps
from copilot.policy import is_allowed
from copilot.state import (
    Action, EvidenceItem, EvidenceRef, FaultType, Hypothesis, IncidentState, RestartService, Rollback, RunbookAdvice,
    ScaleService, ServiceName, SetFlag, payload_hash, sum_ledgers,
)
from eval.schemas import catalogue

_action_adapter = TypeAdapter(Action)


def _escalate(node: str, reason: str, **update: Any) -> dict[str, Any]:
    return update | {"escalation_reason": reason, "timeline": [event(node, "escalate", reason)]}


def runbook_action(passages: list) -> tuple[Any, Any]:
    """(passage, Action) for the first runbook in rank order with a typed fix; postmortems and non-actionable
    runbooks (fix: null, e.g. RB-014) are skipped, never guessed from. (None, None) if there is none."""
    for p in passages:
        kind = p.kind or ("runbook" if p.doc_id.startswith("RB-") else "")
        if kind == "runbook" and p.fix:
            fix = p.fix
            return p, _action_adapter.validate_python({"tool": fix["tool"], **(fix.get("args") or {})})
    return None, None


def make_fixed_runbook(deps: CopilotDeps):
    @guarded("fixed_runbook")
    async def fixed_runbook(state: IncidentState) -> dict[str, Any]:
        alert = state["alert"]
        passages = await deps.runbooks.search(f"{alert.alert_name} {alert.summary}", service=alert.service,
                                              symptom=alert.signal, k=5)
        cost = spend("fixed_workflow", code_calls=1)          # a fixed search, not a model's choice: not budgeted
        book, action = runbook_action(passages)
        if action is None:
            return _escalate("fixed_runbook", f"no runbook with a typed fix for {alert.alert_name}", budget=cost)
        allowed, why = is_allowed(action)
        if not allowed:
            return _escalate("fixed_runbook", why, budget=cost)

        # The runbook's claim, not a tested one: open, citing the runbook (scored as a hypothesis, never as evidence).
        hyps = []
        if isinstance(action, SetFlag) and str(action.flag) in catalogue.FAULT_TYPES:
            service = catalogue.FAULT_TYPES[str(action.flag)]["service"]
            hyps = [Hypothesis(id=f"h-{uuid.uuid4().hex[:6]}", service=ServiceName(service),
                               fault_type=FaultType(str(action.flag)), rationale=f"from runbook {book.doc_id}")]
        text = f"runbook {book.doc_id} → {action.model_dump_json()}"
        emit({"node": "fixed_runbook", "text": text})
        return {
            "runbook_advice": RunbookAdvice(runbook_id=book.doc_id, candidate_action=action),
            "hypotheses": hyps, "proposed_action": action, "action_purpose": "fix",
            "expected_effect": f"the action from runbook {book.doc_id}", "approval": None, "budget": cost,
            "timeline": [event("fixed_runbook", "action", f"Proposed {text}")],
        }
    return fixed_runbook


# The structured response goes to OpenAI as a JSON schema, which refuses "oneOf" (live E1 rehearsal, 5 Nov: every C1
# run failed with "Invalid schema for response_format 'SingleAgentResult' ... 'oneOf' is not permitted"). `Action` is a
# discriminated union and becomes oneOf; a plain union becomes anyOf, which is accepted. Each action model has its own
# `tool` literal, so pydantic still picks the right one.
ResponseAction = Union[SetFlag, RestartService, ScaleService, Rollback]


class SingleAgentResult(BaseModel):
    service: ServiceName | None = None
    fault_type: FaultType | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)
    action: Optional[ResponseAction] = None
    expected_effect: str = ""
    rationale: str = ""


def make_single_agent(deps: CopilotDeps):
    @guarded("single_agent")
    async def single_agent(state: IncidentState) -> dict[str, Any]:
        limits = state["limits"]
        spent = state["budget"].total.tool_calls if state.get("budget") else 0
        budget = max(1, limits.tool_calls - spent)
        outcome, approval = state.get("outcome"), state.get("approval")
        payload = {
            "alert": state["alert"].model_dump(mode="json"),
            "triage": state["triage"].model_dump(mode="json") if state.get("triage") else None,
            "previous_hypotheses": [h.model_dump(mode="json") for h in state.get("hypotheses", [])],
            "why_replanning": state.get("replan_reason"),
            "last_rejection": approval.reason if approval and approval.decision == "reject" else None,
            "already_failed_and_rolled_back": [outcome.action.model_dump(mode="json")]
            if outcome and outcome.rolled_back else [],
            "tool_call_budget": budget,
        }
        agent = create_react_agent(deps.llm("single_agent"), tools=deps.tools("single_agent"),
                                   prompt=load_prompt(deps.settings.prompts_dir, "single_agent"),
                                   response_format=SingleAgentResult)
        meter = UsageMeter()                        # every model reply's tokens and USD (E1 compares cost)
        try:
            out = await agent.ainvoke({"messages": [HumanMessage(render_data(payload))]},
                                      {"recursion_limit": 2 * budget + 3, "callbacks": [meter]})
            result: SingleAgentResult = out["structured_response"]
            messages = out["messages"]
        except GraphRecursionError:
            result, messages = SingleAgentResult(rationale="tool-call budget exhausted"), []

        calls = sum(isinstance(m, ToolMessage) for m in messages)
        tokens = max(meter.tokens, sum((getattr(m, "usage_metadata", None) or {}).get("total_tokens", 0) for m in messages))
        seen = tool_strings(messages) + [e.evidence_id for e in state.get("evidence", [])]
        evidence = [e for e in result.evidence if grounded(e.evidence_id, seen)]
        update: dict[str, Any] = {"budget": sum_ledgers(spend("single_agent", tool_calls=calls),
                                                        spend("single_agent", tokens=tokens, usd=meter.usd)),
                                  "replan_reason": None}
        if state.get("replan_reason"):
            update["replans"] = state.get("replans", 0) + 1

        hyp = None
        if result.service and result.fault_type:
            hyp = Hypothesis(id=f"h-{uuid.uuid4().hex[:6]}", service=result.service, fault_type=result.fault_type,
                             status="supported" if evidence else "open", evidence=evidence,
                             tool_calls_used=calls, rationale=result.rationale)
            update["hypotheses"] = [hyp]
            update["evidence"] = [EvidenceItem(evidence_id=e.evidence_id, hypothesis_id=hyp.id, summary=e.summary,
                                               supports=True, at=now()) for e in evidence]
        verdict = f"{hyp.service}/{hyp.fault_type} {hyp.status}" if hyp else "no root cause"
        emit({"node": "single_agent", "text": f"{verdict} ({calls} calls) — {result.rationale}"})
        if hyp is None or hyp.status != "supported":
            return _escalate("single_agent", f"no supported root cause after {calls} tool calls: {result.rationale}",
                             **update)
        if result.action is None:
            return _escalate("single_agent", f"no safe action: {result.rationale}", **update)
        allowed, why = is_allowed(result.action)
        if allowed and payload_hash(result.action) in state.get("failed_actions", []):
            allowed, why = False, f"{result.action.model_dump_json()} already failed and was rolled back"
        if not allowed:
            return _escalate("single_agent", why, **update)
        return update | {
            "proposed_action": result.action, "action_purpose": "fix", "expected_effect": result.expected_effect,
            "approval": None,
            "timeline": [event("single_agent", "verdict", f"{verdict} ({calls} calls)"),
                         event("single_agent", "action", f"Proposed {result.action.model_dump_json()}")],
        }
    return single_agent
