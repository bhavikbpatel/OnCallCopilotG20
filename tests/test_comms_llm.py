"""Status note facts, timeline order and the OpenRouter provider-error retry — no LLM needed."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import openai
import pytest
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from copilot.agents import comms
from copilot.config import AppSettings
from copilot.llm import RetryingChatOpenAI
from copilot.state import RestartService, SetFlag, TimelineEvent, describe

T0 = datetime(2026, 11, 1, 9, 32, tzinfo=timezone.utc)


def ev(sec, node, kind, text):
    return TimelineEvent(at=T0 + timedelta(seconds=sec), node=node, kind=kind, text=text)


EMAIL_TIMELINE = [
    ev(10, "execute", "action", f"Executed: {describe(SetFlag(flag='emailMemoryLeak', variant='off'))}"),
    ev(20, "verify", "verdict", "NOT recovered: email memory_mb = 620.0"),
    ev(30, "execute", "action", f"Executed: {describe(RestartService(service='email'))}"),
    ev(40, "verify", "verdict", "recovered: email memory_mb = 140.0 (threshold 300.0)"),
]


def test_note_lists_every_action_and_the_recovery():
    assert comms.actions_taken(EMAIL_TIMELINE) == \
        "Executed: set flag emailMemoryLeak to off; Executed: restarted email"
    email = SimpleNamespace(service="email", signal="memory_mb", threshold=300.0)
    assert comms.status_line({"final_status": "recovered", "alert": email}, EMAIL_TIMELINE) == \
        "recovered; email memory back below 300 MB"


def test_note_on_rollback_and_escalation():
    tl = [ev(1, "execute", "action", "Executed: set flag paymentFailure to off"),
          ev(2, "execute", "rollback", "Rolled back: set flag paymentFailure to 50%")]
    st = {"final_status": "escalated", "escalation_reason": "no supported hypothesis after 2 replans"}
    assert "Rolled back: set flag paymentFailure to 50%" in comms.actions_taken(tl)
    assert comms.status_line(st, tl) == "escalated to the on-call engineer: no supported hypothesis after 2 replans"
    assert comms.actions_taken([]) == "none taken"


def test_comms_writes_all_four_parts_and_sorts_the_timeline(monkeypatch):
    async def fake_call(deps, agent, prompt, schema, view):
        times = [e["at"] for e in view["timeline"]]
        assert times == sorted(times)
        return schema(impact="Email delivery is at risk.", cause="emailMemoryLeak flag at 100x.",
                      evidence_ids=["promql:email_memory:15m", "made-up"], summary="s", root_cause="r",
                      lessons_proposed=[]), {}

    monkeypatch.setattr(comms, "structured_call", fake_call)

    class Tickets:
        async def save_postmortem(self, *a):
            pass

    shuffled = [EMAIL_TIMELINE[2], EMAIL_TIMELINE[0], EMAIL_TIMELINE[3], EMAIL_TIMELINE[1]]
    st = {"incident_id": "INC-1", "final_status": "recovered", "timeline": shuffled, "hypotheses": [],
          "evidence": [SimpleNamespace(evidence_id="promql:email_memory:15m", summary="x")]}
    out = asyncio.run(comms.make_comms(SimpleNamespace(tickets=Tickets()))(st))
    text = out["status_note"].text
    for part in ("Impact:", "Cause:", "Action: Executed: set flag emailMemoryLeak to off", "restarted email",
                 "Status: recovered"):
        assert part in text
    assert out["status_note"].evidence_ids == ["promql:email_memory:15m"]     # guardrail drops "made-up"
    assert [e.at for e in out["postmortem"].timeline] == sorted(e.at for e in shuffled)


def _bad_request(message):
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return openai.BadRequestError(message, response=httpx.Response(400, request=req), body={"message": message})


@pytest.mark.parametrize("message,calls,ok", [
    ("Provider returned error", 2, True),            # upstream 400 → retried once, then succeeds
    ("Invalid schema for function", 1, False),       # a real request error → raised at once
])
def test_provider_error_is_retried_other_400s_are_not(monkeypatch, message, calls, ok):
    seen = []

    async def flaky(self, *a, **k):
        seen.append(1)
        if len(seen) == 1:
            raise _bad_request(message)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="hi"))])

    monkeypatch.setattr(ChatOpenAI, "_agenerate", flaky)
    model = RetryingChatOpenAI(model="openai/gpt-5-mini", api_key="x", agent="comms", retry_backoff_s=0)
    if ok:
        assert asyncio.run(model._agenerate([])).generations[0].message.content == "hi"
    else:
        with pytest.raises(openai.BadRequestError):
            asyncio.run(model._agenerate([]))
    assert len(seen) == calls


def test_deps_builds_the_retrying_model(monkeypatch):
    from copilot.deps import CopilotDeps
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    deps = CopilotDeps(settings=AppSettings(), telemetry=None, runbooks=None, ops=None, tickets=None,
                       store=None, tools_factory=None)
    assert isinstance(deps.llm("comms"), RetryingChatOpenAI)


def test_structured_retry_answers_the_tool_call():
    """The validation retry must answer the model's tool call (OpenAI 400 'No tool output found' otherwise)."""
    from langchain_core.messages import ToolMessage
    from pydantic import BaseModel

    from copilot.agents.base import structured_call

    class Out(BaseModel):
        a: int

    sent = []

    class FakeLLM:
        def with_structured_output(self, *a, **k):
            return self

        async def ainvoke(self, messages):
            sent.append(list(messages))
            raw = AIMessage(content="", tool_calls=[{"id": "call_1", "name": "Out", "args": {"a": "x"}}])
            if len(sent) == 1:
                return {"raw": raw, "parsed": None, "parsing_error": "a: not an int"}
            return {"raw": raw, "parsed": Out(a=1), "parsing_error": None}

    deps = SimpleNamespace(llm=lambda agent: FakeLLM(), settings=AppSettings())
    out, _ = asyncio.run(structured_call(deps, "comms", "comms", Out, {}))
    assert out.a == 1
    retry = sent[1]
    assert isinstance(retry[-1], ToolMessage) and retry[-1].tool_call_id == "call_1"


@pytest.mark.parametrize("signal,threshold,want", [
    ("error_rate", 0.05, "recovered; checkout error rate back below 5%"),
    ("latency_p95", 800.0, "recovered; checkout p95 latency back below 800 ms"),
    ("memory", 300.0, "recovered; checkout memory back below 300 MB"),
    ("throughput_rps", 2.0, "recovered; checkout throughput back above 2 requests/s"),
    ("odd_signal", 7.0, "recovered; checkout odd signal back below 7"),
])
def test_recovered_status_is_plain_words_without_the_raw_reading(signal, threshold, want):
    a = SimpleNamespace(service="checkout", signal=signal, threshold=threshold)
    raw = [ev(5, "verify", "verdict", "recovered: checkout error_rate since fix = 0.0 (0/12 requests) (threshold 0.05)")]
    line = comms.status_line({"final_status": "recovered", "alert": a}, raw)
    assert line == want
    assert "since fix" not in line and "requests)" not in line and "threshold" not in line


def test_recovered_status_without_an_alert_still_says_recovered():
    assert comms.status_line({"final_status": "recovered"}, EMAIL_TIMELINE) == "recovered"
