"""3.5 Diagnosis — one ReAct branch per open hypothesis (Send API), read-only telemetry tools.

The ReAct message history is private to the branch and discarded; only the HypothesisResult
(verdict + evidence ids) is merged into the parent state.
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent

from copilot.agents.base import emit, event, load_prompt, now, render_data, spend
from copilot.deps import CopilotDeps
from copilot.state import DiagnosisInput, EvidenceItem, HypothesisResult, sum_ledgers


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return [value]
        return [value, *_strings(decoded)] if not isinstance(decoded, str) else [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return [str(value)] if value is not None else []


def tool_strings(messages: list) -> list[str]:
    """Every string value in this branch's tool results (JSON decoded, so quotes in PromQL ids match)."""
    return [s for m in messages if isinstance(m, ToolMessage) for s in _strings(m.content)]


def grounded(evidence_id: str, seen: list[str]) -> bool:
    """An evidence id counts only if a tool returned exactly that value. A substring match would accept short
    ids like "payment" or "error_rate", which appear inside almost any tool output."""
    eid = evidence_id.strip()
    return bool(eid) and eid in {s.strip() for s in seen}


def make_diagnosis(deps: CopilotDeps):
    async def diagnosis(inp: DiagnosisInput) -> dict[str, Any]:
        hyp = inp["hypothesis"]
        agent_key = f"diagnosis:{hyp.id}"
        budget = max(1, inp["tool_call_budget"])
        agent = create_react_agent(
            deps.llm("diagnosis"),
            tools=deps.tools("diagnosis"),          # read-only by construction (permissions.yaml + hooks)
            prompt=load_prompt(deps.settings.prompts_dir, "diagnosis"),
            response_format=HypothesisResult,
        )
        payload = {
            "hypothesis": hyp.model_dump(mode="json"),
            "alert": inp["alert"].model_dump(mode="json"),
            "triage": inp["triage"].model_dump(mode="json") if inp.get("triage") else None,
            "prior_evidence": [e.model_dump(mode="json") for e in inp.get("evidence", [])],
            "tool_call_budget": budget,
        }
        # Each tool call = model step + tool step; +2 for the final answer and structured response.
        config = {"recursion_limit": 2 * budget + 3}
        try:
            out = await agent.ainvoke({"messages": [HumanMessage(render_data(payload))]}, config)
            result: HypothesisResult = out["structured_response"]
            messages = out["messages"]
        except GraphRecursionError:
            result = HypothesisResult(hypothesis_id=hyp.id, verdict="inconclusive",
                                      confidence_basis="tool-call budget exhausted")
            messages = []

        tool_calls = sum(isinstance(m, ToolMessage) for m in messages)
        seen = tool_strings(messages) + [e.evidence_id for e in inp.get("evidence", [])]   # prior items were checked
        dropped = [e.evidence_id for e in result.evidence if not grounded(e.evidence_id, seen)]
        if dropped:                                  # cited but never returned by a tool in this branch
            result = result.model_copy(update={
                "evidence": [e for e in result.evidence if e.evidence_id not in dropped],
                "confidence_basis": f"{result.confidence_basis} [dropped evidence not from a tool result: {dropped}]"})
        tokens = sum((getattr(m, "usage_metadata", None) or {}).get("total_tokens", 0) for m in messages)
        status = {"supported": "supported", "refuted": "refuted"}.get(result.verdict, "open")
        if status == "supported" and not result.evidence:
            status = "open"                          # a claim without cited evidence does not count
        updated = hyp.model_copy(update={
            "status": status,
            "evidence": [*hyp.evidence, *result.evidence],
            "tool_calls_used": hyp.tool_calls_used + tool_calls,
            "contradicts_plan": result.contradicts_plan,
        })
        supports = {"supported": True, "refuted": False}.get(result.verdict)
        evidence = [EvidenceItem(evidence_id=e.evidence_id, hypothesis_id=hyp.id, summary=e.summary,
                                 supports=supports, at=now())
                    for e in result.evidence]
        text = f"{hyp.service}/{hyp.fault_type}: {result.verdict} ({tool_calls} calls) — {result.confidence_basis}"
        emit({"node": "diagnosis", "hypothesis_id": hyp.id, "text": text})
        return {
            "hypotheses": [updated],
            "evidence": evidence,
            "budget": sum_ledgers(spend(agent_key, tool_calls=tool_calls), spend(agent_key, tokens=tokens)),
            "timeline": [event("diagnosis", "verdict", text)],
        }
    return diagnosis
