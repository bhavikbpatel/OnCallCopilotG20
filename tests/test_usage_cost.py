"""Every model reply's US-dollar cost (OpenRouter's usage.cost) reaches the budget ledger and the result line."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from pydantic import BaseModel

from copilot.agents.base import UsageMeter, structured_call
from copilot.config import AppSettings
from eval.adapters.copilot_state import to_result_line


def reply(tokens=None, cost=None, **extra):
    usage = {k: v for k, v in (("total_tokens", tokens), ("cost", cost)) if v is not None} | extra
    return SimpleNamespace(llm_output={"token_usage": usage, "model_name": "m"}, generations=[])


def test_meter_adds_tokens_and_dollars_of_every_reply():
    m = UsageMeter()
    m.on_llm_end(reply(1200, 0.0031))
    m.on_llm_end(reply(800, "0.0009"))                      # some providers send a string
    assert m.tokens == 2000 and abs(m.usd - 0.004) < 1e-12


def test_missing_or_bad_cost_adds_nothing():
    m = UsageMeter()
    for c in (None, float("nan"), -1.0, "n/a", float("inf")):
        m.on_llm_end(reply(10, c))
    assert m.tokens == 50 and m.usd == 0.0


def test_meter_reads_the_reply_when_there_is_no_llm_output():
    msg = SimpleNamespace(usage_metadata={"total_tokens": 300},
                          response_metadata={"token_usage": {"total_tokens": 300, "cost": 0.002}})
    m = UsageMeter()
    m.on_llm_end(SimpleNamespace(llm_output=None, generations=[[SimpleNamespace(message=msg)]]))
    assert m.tokens == 300 and m.usd == 0.002


class Out(BaseModel):
    a: int


def test_structured_call_puts_the_cost_in_the_ledger_and_result_line():
    class FakeLLM:
        def with_structured_output(self, *a, **k):
            return self

        async def ainvoke(self, messages, config=None):
            for cb in (config or {}).get("callbacks", []):     # what LangChain does at the end of each call
                cb.on_llm_end(reply(500, 0.0025))
            raw = SimpleNamespace(usage_metadata={"total_tokens": 500}, tool_calls=[])
            return {"raw": raw, "parsed": Out(a=1), "parsing_error": None}

    deps = SimpleNamespace(llm=lambda agent: FakeLLM(), settings=AppSettings())
    _, ledger = asyncio.run(structured_call(deps, "comms", "comms", Out, {}))
    spent = ledger.by_agent["comms"]
    assert spent.tokens == 500 and spent.usd == 0.0025
    state = {"run_id": "r", "final_status": "escalated", "budget": ledger, "limits": {"tool_calls": 20}}
    line = to_result_line(state, case_id="D-001", config_id="T", run_id="r",
                          started_at="2026-11-02T00:00:00Z", ended_at="2026-11-02T00:01:00Z", status="completed")
    assert line.cost.usd == 0.0025 and line.cost.tokens_total == 500
