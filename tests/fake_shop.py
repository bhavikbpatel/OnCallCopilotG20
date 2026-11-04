"""An in-process stand-in for the four MCP servers, for driving run_incident end to end without a shop.

Tools answer in the servers' {ok, result} envelope with the same field names, so the graph's real clients,
the toolbox and the hooks are all exercised. Prometheus answers by query shape: error counts drop to 0 once
paymentFailure is off (unless `recovers=False`).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool

from copilot.config import ROOT
from copilot.state import BudgetLedger, ServiceName
from copilot.tools import McpToolbox, load_permissions
from eval.schemas import payload_hash
from mcp_servers.common.approval import check_token

ANY_ARGS = {"type": "object", "properties": {}, "additionalProperties": True}
PASSAGES = [   # rank order: a non-actionable runbook and a postmortem come before the actionable runbook
    {"passage_id": "RB-014#action", "doc_id": "RB-014", "text": "intermittent", "score": 1.0, "kind": "runbook",
     "fix": None},
    {"passage_id": "PM-001#fix", "doc_id": "PM-001", "text": "past incident", "score": 0.5, "kind": "postmortem",
     "fix": {"tool": "restart_service", "args": {"service": "cart"}}},
    {"passage_id": "RB-001#action", "doc_id": "RB-001", "text": "payment errors", "score": 0.33, "kind": "runbook",
     "fix": {"tool": "set_flag", "args": {"flag": "paymentFailure", "variant": "off"}}},
]


def ok(result: Any) -> dict:
    return {"ok": True, "result": result}


class FakeTool(BaseTool):
    name: str
    description: str = "fake MCP tool"
    handler: Any = None
    args_schema: Any = ANY_ARGS

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("async only")

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> str:   # noqa: A002
        return json.dumps(self.handler(dict(input or {})))


class Shop:
    def __init__(self, recovers: bool = True):
        self.recovers = recovers
        self.flags = {"paymentFailure": "50%"}
        self.writes: list[tuple[str, dict]] = []

    def promql(self, a: dict) -> dict:
        q = a.get("query", "")
        fixed = self.flags["paymentFailure"] == "off" and self.recovers
        if "increase(" in q:
            value = (0.0 if fixed else 6.0) if "STATUS_CODE_ERROR" in q else 12.0
        elif "histogram_quantile" in q:
            value = 120.0
        else:
            value = 0.01 if fixed else 0.4
        return ok({"series": [{"labels": {"service_name": "checkout"}, "values": [[0, str(value)]]}],
                   "evidence_id": f"promql:{q}"})

    def changes(self, a: dict) -> dict:
        variant = self.flags["paymentFailure"]
        return ok({"changes": [] if variant == "off" else [
            {"flag": "paymentFailure", "variant": variant, "at": datetime.now(timezone.utc).isoformat(),
             "evidence_id": f"flag:paymentFailure={variant}"}]})

    def dry_run(self, a: dict) -> dict:
        undo = {"tool": "set_flag", "flag": a["flag"], "variant": self.flags.get(a["flag"], "off")} \
            if a.get("tool") == "set_flag" else None
        return ok({"would_do": json.dumps(a, sort_keys=True), "expected_effect": "errors stop",
                   "current_state": {"flags": dict(self.flags)}, "rollback_action": undo})

    def write(self, tool: str) -> Callable[[dict], dict]:
        def run(a: dict) -> dict:
            args = {k: v for k, v in a.items() if k != "approval_token"}
            code = check_token(payload_hash(tool, args), a.get("approval_token"))   # as ops_actions does
            if code:
                return {"ok": False, "error": {"code": code, "message": "refused"}}
            if tool == "set_flag":
                self.flags[args["flag"]] = args["variant"]
            self.writes.append((tool, args))
            return ok({"action_id": f"act-{len(self.writes)}"})
        return run

    def toolbox(self) -> McpToolbox:
        handlers = {
            "telemetry.query_promql": self.promql, "telemetry.recent_changes": self.changes,
            "telemetry.search_logs": lambda a: ok({"hits": []}), "telemetry.get_traces": lambda a: ok({"traces": []}),
            "runbooks_kb.search": lambda a: ok({"passages": PASSAGES}),
            "runbooks_kb.get": lambda a: ok({"passages": PASSAGES[-1:]}),
            "tickets.create_incident": lambda a: ok({"incident_id": a.get("incident_id"), "warnings": []}),
            "tickets.get_incident": lambda a: ok({}), "tickets.append_timeline": lambda a: ok({}),
            "tickets.save_postmortem": lambda a: ok({}), "tickets.draft_status": lambda a: ok({}),
            "ops_actions.dry_run": self.dry_run,
            **{f"ops_actions.{t}": self.write(t) for t in ("set_flag", "restart_service", "scale_service", "rollback")},
        }
        tools = {q: FakeTool(name=q.split(".", 1)[1], handler=h) for q, h in handlers.items()}
        return McpToolbox(tools, load_permissions(ROOT / "config" / "permissions.yaml"))


def alert(**over: Any) -> dict:
    return {"alert_name": "CheckoutErrorRateHigh", "service": "checkout", "signal": "error_rate",
            "summary": "Checkout error rate above 5%", "query": "checkout_error_ratio", "threshold": 0.05,
            "value": 0.4, "fired_at": datetime.now(timezone.utc).isoformat(), "fingerprint": "fp-1", **over}


async def fake_structured_call(deps: Any, agent: str, prompt: str, schema: type, payload: dict):
    """triage and comms without an LLM: fixed, valid drafts."""
    name = schema.__name__
    if name == "TriageDraft":
        return schema(suspects=[ServiceName("payment")], impact="checkout failing", changes=["paymentFailure 50%"]), \
            BudgetLedger()
    if name == "CommsDraft":
        return schema(impact="Checkout failing.", cause="paymentFailure at 50%.", evidence_ids=[], summary="s",
                      root_cause="paymentFailure flag", lessons_proposed=[]), BudgetLedger()
    from copilot.state import SetFlag
    fix = SetFlag(flag="paymentFailure", variant="off")
    drafts = {   # C2's commander, runbook and remediation hand-offs
        "PlanDraft": lambda: schema(assumptions=["payment path"], step_notes={}),
        "HypothesesDraft": lambda: schema(hypotheses=[{"service": "payment", "fault_type": "paymentFailure",
                                                       "rationale": "flag at 50%"}], assumptions=[]),
        "AssessSummary": lambda: schema(summary="payment errors follow the flag"),
        "RunbookAdvice": lambda: schema(runbook_id="RB-001", candidate_action=fix),
        "ActionProposal": lambda: schema(action=fix, expected_effect="errors stop", rationale="runbook RB-001"),
    }
    if name in drafts:
        return drafts[name](), BudgetLedger()
    raise AssertionError(f"unexpected structured call {name}")


class ScriptedChat(BaseChatModel):
    """A chat model that plays back AIMessages (tool calls, then a final answer) and a fixed structured response."""
    script: list = []
    structured: Any = None

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        msg = self.script.pop(0) if self.script else AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChat":
        return self

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        return RunnableLambda(lambda _: self.structured)
