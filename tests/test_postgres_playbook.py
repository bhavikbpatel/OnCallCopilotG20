"""Against a real Postgres (opt-in: COPILOT_PG_TESTS=1 and POSTGRES_DSN in the environment or .env).

A lesson saved in the shared namespace is used by a `memory: playbook` run with its own run_id, through the real
AsyncPostgresStore and checkpointer. Uses its own lesson ids and deletes them; never withdraws real lessons.
"""
import asyncio
import os

import pytest
from dotenv import dotenv_values

from copilot.config import ROOT

DSN = os.getenv("POSTGRES_DSN") or dotenv_values(ROOT / ".env").get("POSTGRES_DSN")
pytestmark = pytest.mark.skipif(os.getenv("COPILOT_PG_TESTS") != "1" or not DSN,
                                reason="set COPILOT_PG_TESTS=1 and POSTGRES_DSN to run against a real Postgres")


def test_playbook_lesson_reaches_a_run_through_real_postgres(monkeypatch):
    from langchain_core.messages import AIMessage

    from copilot.agents import commander, comms, remediation, runbook, triage
    from copilot.config import load_settings
    from copilot.memory import make_store
    from copilot.memory.backends import loop_factory
    from copilot.memory.long_term import LongTermMemory
    from copilot.memory.schemas import Lesson, Precondition
    from copilot.run import run_incident
    from copilot.state import BudgetLimits, EvidenceRef, HypothesisResult
    from eval.runner.approvals import AutoApprover
    from tests.fake_shop import ScriptedChat, Shop, alert, fake_structured_call

    async def instant(_):
        return None
    monkeypatch.setattr(remediation.asyncio, "sleep", instant)
    for mod in (triage, comms, commander, runbook, remediation):
        monkeypatch.setattr(mod, "structured_call", fake_structured_call)
    monkeypatch.setenv("APPROVAL_SECRET", "test-secret-0123456789abcdef")
    monkeypatch.setenv("POSTGRES_DSN", DSN)

    lesson = Lesson(lesson_id="L-TEST-pg-1", text="When paymentFailure is on, act on payment.", service="payment",
                    fault_family="paymentFailure", source_incident="test", reviewed_by="test",
                    preconditions=[Precondition(check="recent_change", args={"flag": "paymentFailure", "within": "15m"})])
    llm = ScriptedChat(
        script=[AIMessage(content="", tool_calls=[{"name": "query_promql", "args": {"query": "payment_errors"}, "id": "p"}])],
        structured=HypothesisResult(hypothesis_id="h", verdict="supported", confidence_basis="flag",
                                    evidence=[EvidenceRef(evidence_id="promql:payment_errors", summary="up")]))

    async def go():
        async with make_store(DSN) as store:
            await LongTermMemory(store).save_lesson(lesson)                 # the freeze step's shared namespace
            try:
                res = await run_incident(
                    alert(), AutoApprover("approve"), run_id="E3-after-PGTEST-r1", limits=BudgetLimits(tool_calls=20),
                    settings=load_settings().model_copy(update={"verify_window_s": 60, "verify_window_by_signal": {}}),
                    options={"architecture": "C2", "memory": "playbook", "review_enabled": False},
                    toolbox=Shop().toolbox(), llm_factory=lambda a: llm)     # its own make_store(POSTGRES_DSN)
            finally:
                await store.adelete(("lessons", "payment", "paymentFailure"), lesson.lesson_id)
            return res

    res = asyncio.run(go(), loop_factory=loop_factory(DSN))
    used = [l.lesson_id for l in res.final_state["memory_context"].applicable_lessons]
    assert "L-TEST-pg-1" in used and res.final_state["final_status"] == "recovered"
