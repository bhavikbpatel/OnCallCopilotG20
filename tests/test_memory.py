from datetime import datetime, timedelta, timezone

import pytest
from langgraph.store.memory import InMemoryStore

from copilot.memory import (
    IncidentRecord, Lesson, LongTermMemory, MemoryReadOnlyError, Precondition, evaluate_lesson,
)
from copilot.memory.long_term import playbook_hash

NOW = datetime(2026, 11, 3, 10, 15, tzinfo=timezone.utc)


class FakeTelemetry:
    def __init__(self, changes=None, values=None, fail=False):
        self.changes, self.values, self.fail = changes or [], values or {}, fail

    async def recent_changes(self, since):
        if self.fail:
            raise TimeoutError("telemetry down")
        return [c for c in self.changes if c["at"] >= since]

    async def query_scalar(self, query):
        return self.values.get(query)


PAY_Q = "rate(payment_errors_total[5m])"


def l007(**kw):
    return Lesson(
        lesson_id="L-007",
        text="paymentFailure flag change → switch it off",
        preconditions=[
            Precondition(check="recent_change", args={"flag": "paymentFailure", "within": "15m"}),
            Precondition(check="promql_above", args={"query": PAY_Q, "threshold": 0.05}),
        ],
        service="payment", fault_family="feature_flag", source_incident="INC-dev-012", reviewed_by="rohit", **kw,
    )


def incident(iid, services, family="feature_flag"):
    return IncidentRecord(
        incident_id=iid, alert_name="CheckoutErrorRateHigh", services=services, fault_family=family,
        symptom_text="checkout errors", root_cause="x", action=None, outcome="recovered",
        reviewed_by="rohit", stored_at=NOW,
    )


# ---------- preconditions ----------

async def test_lesson_applies_when_all_preconditions_pass():
    tel = FakeTelemetry(changes=[{"flag": "paymentFailure", "at": NOW - timedelta(minutes=3)}], values={PAY_Q: 0.12})
    ok, results = await evaluate_lesson(l007(), tel, NOW)
    assert ok and all(r.passed for r in results)


async def test_misleading_lesson_rejected_when_flag_not_changed():
    # demo 3: payment errors from latency toxic, flag untouched
    tel = FakeTelemetry(values={PAY_Q: 0.12})
    ok, results = await evaluate_lesson(l007(), tel, NOW)
    assert not ok and not results[0].passed


async def test_old_change_outside_window_fails():
    tel = FakeTelemetry(changes=[{"flag": "paymentFailure", "at": NOW - timedelta(hours=1)}], values={PAY_Q: 0.12})
    ok, _ = await evaluate_lesson(l007(), tel, NOW)
    assert not ok


async def test_tool_error_and_no_data_count_as_failed():
    ok, results = await evaluate_lesson(l007(), FakeTelemetry(fail=True), NOW)
    assert not ok
    assert "check error" in results[0].detail and "no data" in results[1].detail


async def test_lesson_without_preconditions_never_applies():
    ok, _ = await evaluate_lesson(l007().model_copy(update={"preconditions": []}), FakeTelemetry(), NOW)
    assert not ok


# ---------- store ----------

async def test_incident_search_filters_by_service():
    mem = LongTermMemory(InMemoryStore())
    await mem.save_incident(incident("INC-1", ["payment"]))
    await mem.save_incident(incident("INC-2", ["cart"]))
    found = await mem.search_incidents("checkout errors", services=["payment"])
    assert [r.incident_id for r in found] == ["INC-1"]


async def test_withdrawn_lessons_not_returned():
    mem = LongTermMemory(InMemoryStore())
    await mem.save_lesson(l007())
    assert len(await mem.list_lessons(service="payment")) == 1
    assert await mem.withdraw_lesson("payment", "feature_flag", "L-007")
    assert await mem.list_lessons(service="payment") == []
    assert len(await mem.list_lessons(service="payment", include_withdrawn=True)) == 1


async def test_build_memory_context_splits_lessons():
    mem = LongTermMemory(InMemoryStore())
    await mem.save_lesson(l007())
    await mem.save_incident(incident("INC-1", ["payment"]))
    await mem.add_pref("eng1", "always show the PromQL used")

    ctx = await mem.build_memory_context(
        symptom="checkout errors", suspects=["payment", "cart"], fault_families=None,
        telemetry=FakeTelemetry(values={PAY_Q: 0.12}), engineer_id="eng1", now=NOW,
    )
    assert ctx.applicable_lessons == []
    assert ctx.rejected_lessons[0].lesson.lesson_id == "L-007"
    assert "paymentFailure" in ctx.rejected_lessons[0].reason
    assert ctx.similar_incidents[0].incident_id == "INC-1"
    assert ctx.engineer_prefs[0].text == "always show the PromQL used"


async def test_memory_off_returns_empty():
    mem = LongTermMemory(None)
    await mem.save_lesson(l007())  # no-op
    ctx = await mem.build_memory_context(
        symptom="x", suspects=["payment"], fault_families=None, telemetry=FakeTelemetry(), engineer_id="eng1",
    )
    assert ctx.applicable_lessons == ctx.rejected_lessons == ctx.similar_incidents == ctx.engineer_prefs == []


async def test_run_id_isolates_namespaces():
    store = InMemoryStore()
    await LongTermMemory(store, run_id="run-a").save_lesson(l007())
    assert await LongTermMemory(store, run_id="run-b").list_lessons() == []
    assert len(await LongTermMemory(store, run_id="run-a").list_lessons()) == 1


async def test_frozen_playbook_is_read_only_and_hash_stable():
    store = InMemoryStore()
    src = LongTermMemory(store, run_id="dev")
    await src.save_lesson(l007())
    lessons, h = await src.export_playbook()
    assert h == playbook_hash(lessons)

    target = LongTermMemory(store, run_id="heldout")
    await target.load_playbook(lessons)
    frozen = LongTermMemory(store, run_id="heldout", read_only=True)
    assert (await frozen.export_playbook())[1] == h
    with pytest.raises(MemoryReadOnlyError):
        await frozen.save_lesson(l007())


async def test_prefs_add_and_delete():
    mem = LongTermMemory(InMemoryStore())
    p = await mem.add_pref("eng1", " show PromQL ")
    assert p.text == "show PromQL"
    await mem.delete_pref("eng1", p.pref_id)
    assert await mem.list_prefs("eng1") == []
