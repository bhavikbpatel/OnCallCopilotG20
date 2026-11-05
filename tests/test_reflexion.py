"""Reflexion loop: failed dev runs → candidate lessons → a person's review → frozen playbook read by held-out runs."""
import asyncio
import json
from datetime import datetime, timezone

import pytest
from langgraph.store.memory import InMemoryStore

from copilot import reflexion
from copilot.agents import base
from copilot.agents.base import HandoffError
from copilot.memory.long_term import LongTermMemory, playbook_hash
from copilot.memory.schemas import Lesson, Precondition
from copilot.reflexion import LessonDraft, decide, failed_runs, freeze, lesson_problems, read_playbook, review_cli

PAYMENT_Q = 'sum(rate(x{service_name="payment"}[5m]))'
GOOD = {"text": "When checkout errors follow a recent paymentFailure change, the root cause is payment, not checkout.",
        "service": "payment", "fault_family": "paymentFailure",
        "preconditions": [{"check": "recent_change", "args": {"flag": "paymentFailure", "within": "15m"}},
                          {"check": "promql_above", "args": {"query": PAYMENT_Q, "threshold": 0.05}}]}


def score(run_id, case_id, ok=False, **over):
    s = {"run_id": run_id, "case_id": case_id, "config_id": "DEV-C2", "status": "completed", "verdict_correct": True,
         "root_cause_top1": ok, "root_cause_top3": True, "root_cause_service_top1": ok,
         "action_proposed_correct": ok, "action_executed_correct": ok, "recovered": ok, "harmful_actions": [],
         "unnecessary_actions": [], "policy_violations": [], "severity_correct": True, "task_success": ok}
    return s | over


def result(run_id, case_id):
    return {"run_id": run_id, "case_id": case_id, "verdict": "incident", "final_status": "escalated",
            "escalation_reason": "no safe action",
            "hypotheses": [{"service": "checkout", "fault_type": "paymentFailure", "status": "supported", "rank": 1}],
            "proposed_actions": [], "explanation": {"status_note": "n", "timeline": [{"text": "triage SEV1"}]}}


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "DEV-C2-20261104T153515Z"
    d.mkdir()
    rows = [("r1", "D-001", False, {}), ("r2", "D-002", True, {}),
            ("r3", "D-011", False, {"harmful_actions": [{"tool": "set_flag", "args": {"flag": "paymentFailure"}}]})]
    (d / "scores.jsonl").write_text("".join(json.dumps(score(r, c, ok, **o)) + "\n" for r, c, ok, o in rows))
    (d / "results.jsonl").write_text("".join(json.dumps(result(r, c)) + "\n" for r, c, _, _ in rows))
    return d


@pytest.fixture
def fake_llm(monkeypatch):
    seen = []

    async def call(deps, agent, prompt, schema, payload):
        seen.append(payload)
        if payload["case_id"] == "D-011":
            raise HandoffError("reflexion: malformed output after retry")
        return schema.model_validate(GOOD | {"failure_mode": "named checkout"}), None
    monkeypatch.setattr(base, "structured_call", call)
    return seen


# ---------- evaluator (code) ----------

def test_only_failed_dev_runs_are_reflected_on_with_code_verdicts(run_dir):
    runs = {r["case_id"]: r for r in failed_runs(run_dir)}
    assert set(runs) == {"D-001", "D-011"}                                 # D-002 passed
    assert "wrong top-1 root cause" in runs["D-001"]["failures"]
    assert any(f.startswith("harmful action") for f in runs["D-011"]["failures"])
    assert runs["D-001"]["expected"]["root_causes"] == [{"service": "payment", "fault_type": "paymentFailure"}]
    assert "error_rate" in runs["D-001"]["available_queries"]["payment"]    # PromQL the model can cite


def test_held_out_never_feeds_lessons(run_dir, tmp_path):
    with (run_dir / "scores.jsonl").open("a") as fh:
        fh.write(json.dumps(score("r9", "H-004")) + "\n")
    with pytest.raises(ValueError, match="not a dev case"):
        failed_runs(run_dir)
    with pytest.raises(ValueError, match="dev cases only"):
        failed_runs(run_dir, tmp_path / "heldout")


def test_severity_only_failures_are_not_reflected_on(tmp_path):
    d = tmp_path / "b"
    d.mkdir()
    (d / "scores.jsonl").write_text(json.dumps(score("r1", "D-001", ok=True, task_success=False,
                                                     severity_correct=False)) + "\n")
    (d / "results.jsonl").write_text(json.dumps(result("r1", "D-001")) + "\n")
    assert failed_runs(d) == []                                            # severity is code; no lesson can fix it


# ---------- the lesson must be checkable ----------

@pytest.mark.parametrize("change,problem", [
    ({"text": "In D-004 the cause was payment."}, "mentions a case"),
    ({"preconditions": [{"check": "recent_change", "args": {"flag": "notAFlag", "within": "15m"}}]}, "not a known fault flag"),
    ({"preconditions": [{"check": "recent_change", "args": {"flag": "paymentFailure", "within": "soon"}}]}, "not like 15m"),
    ({"preconditions": [{"check": "promql_above", "args": {"query": "", "threshold": "x"}}]}, "needs a PromQL query"),
    ({"text": "One. Two. Three."}, "two sentences"),
])
def test_uncheckable_or_case_specific_lessons_are_refused(change, problem):
    with pytest.raises(ValueError, match=problem):
        LessonDraft.model_validate(GOOD | change)


def test_a_lesson_without_preconditions_can_never_apply():
    assert "no precondition" in lesson_problems("x.", [])[0]
    assert LessonDraft.model_validate(GOOD).service == "payment"


# ---------- reflection (LLM) ----------

def test_reflect_writes_one_pending_candidate_per_failed_run(run_dir, tmp_path, fake_llm):
    cands = tmp_path / "candidates.jsonl"
    new = asyncio.run(reflexion.reflect(run_dir, candidates=cands))
    assert [c["case_id"] for c in new] == ["D-001", "D-011"] and all(c["status"] == "pending" for c in new)
    assert new[0]["lesson"]["text"] == GOOD["text"] and new[0]["source_run"] == "r1"
    assert new[1]["lesson"] is None and "malformed" in new[1]["reflection_error"]   # kept for the reviewer
    assert asyncio.run(reflexion.reflect(run_dir, candidates=cands)) == []          # runs are reflected on once
    assert len(reflexion.jsonl(cands)) == 2


# ---------- human review ----------

def pending(case="D-001", n=1, lesson=GOOD):
    return {"candidate_id": f"C-{case}-{n:03d}", "source_run": f"run-{case}", "case_id": case,
            "failures": ["wrong top-1 root cause"], "expected": [], "lesson": lesson, "status": "pending",
            "reviewed_by": None, "reason": None, "decided_at": None, "lesson_id": None}


def test_a_person_approves_edits_or_rejects_with_a_reason():
    ok = decide(pending(), "approve", "Rohit")
    assert ok["status"] == "approved" and ok["lesson_id"] == "L-D-001-001" and ok["reviewed_by"] == "Rohit"
    edited = decide(pending(), "edit", "Rohit", edited={"text": "When checkout errors follow paymentFailure, act on payment."})
    assert edited["edited"] and edited["lesson"]["text"].startswith("When checkout")
    with pytest.raises(ValueError, match="reason"):
        decide(pending(), "reject", "Rohit")
    with pytest.raises(ValueError, match="reviewer"):
        decide(pending(), "approve", " ")
    with pytest.raises(ValueError, match="checkable"):
        decide(pending(lesson=None), "approve", "Rohit")                    # a failed reflection cannot be approved
    with pytest.raises(ValueError, match="already"):
        decide(ok, "reject", "Rohit", reason="x")


def test_review_cli_saves_each_decision_and_the_rejection_evidence(tmp_path):
    cands = tmp_path / "candidates.jsonl"
    reflexion.write_candidates(cands, [pending("D-001", 1), pending("D-011", 2)])
    answers = iter(["a", "r", "too specific to one case: the flag was off"])
    review_cli(cands, "Rohit", ask=lambda _: next(answers), say=lambda _: None)
    rows = reflexion.jsonl(cands)
    assert [r["status"] for r in rows] == ["approved", "rejected"]
    rejected = (tmp_path / "rejected.md").read_text()
    assert "too specific to one case" in rejected and "Rohit" in rejected


# ---------- freeze ----------

def test_freeze_writes_the_playbook_and_loads_exactly_it_into_the_store(tmp_path):
    cands, out, store = tmp_path / "candidates.jsonl", tmp_path / "playbook", InMemoryStore()
    rows = [decide(pending("D-001", 1), "approve", "Rohit"),
            decide(pending("D-011", 2), "reject", "Rohit", reason="no checkable precondition")]
    reflexion.write_candidates(cands, rows)
    stale = Lesson(lesson_id="L-old", text="old", preconditions=[], service="cart", fault_family="x",
                   source_incident="r0", reviewed_by="someone")
    asyncio.run(LongTermMemory(store).save_lesson(stale))

    lessons, digest = asyncio.run(freeze(cands, out, store=store))
    data = json.loads((out / "playbook.json").read_text())
    assert data["playbook_hash"] == digest == playbook_hash(lessons) and data["reviewed_by"] == ["Rohit"]
    assert digest in (out / "README.md").read_text() and "no checkable precondition" in (out / "rejected.md").read_text()
    exported, store_hash = asyncio.run(LongTermMemory(store).export_playbook())
    assert store_hash == digest and [l.lesson_id for l in exported] == ["L-D-001-001"]   # the stale lesson is withdrawn
    assert read_playbook(out / "playbook.json")[1] == digest


def test_freeze_refuses_pending_candidates_and_a_tampered_playbook_is_caught(tmp_path):
    cands = tmp_path / "candidates.jsonl"
    reflexion.write_candidates(cands, [pending()])
    with pytest.raises(ValueError, match="pending"):
        asyncio.run(freeze(cands, tmp_path / "pb", store=InMemoryStore()))
    reflexion.write_candidates(cands, [decide(pending(), "approve", "Rohit")])
    asyncio.run(freeze(cands, tmp_path / "pb", store=InMemoryStore()))
    path = tmp_path / "pb" / "playbook.json"
    data = json.loads(path.read_text())
    data["lessons"][0]["text"] = "changed after freezing"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="changed after freezing"):
        read_playbook(path)


# ---------- held-out runs read the frozen playbook (brief item 4) ----------

def test_run_incident_with_memory_playbook_reads_the_frozen_lessons(monkeypatch):
    from copilot.agents import comms, commander, remediation, runbook, triage
    from copilot.state import EvidenceRef, HypothesisResult
    from langchain_core.messages import AIMessage
    from tests.fake_shop import ScriptedChat, Shop, fake_structured_call

    async def instant(_):
        return None
    monkeypatch.setattr(remediation.asyncio, "sleep", instant)
    for mod in (triage, comms, commander, runbook, remediation):
        monkeypatch.setattr(mod, "structured_call", fake_structured_call)
    monkeypatch.setenv("APPROVAL_SECRET", "test-secret-0123456789abcdef")

    helps = Lesson(lesson_id="L-D-001-001", text=GOOD["text"], service="payment", fault_family="paymentFailure",
                   preconditions=[Precondition(check="recent_change", args={"flag": "paymentFailure", "within": "15m"})],
                   source_incident="r1", reviewed_by="Rohit")
    misleads = Lesson(lesson_id="L-D-011-002", text="Payment errors mean paymentUnreachable.", service="payment",
                      fault_family="paymentUnreachable", source_incident="r3", reviewed_by="Rohit",
                      preconditions=[Precondition(check="recent_change", args={"flag": "paymentUnreachable", "within": "15m"})])
    store = InMemoryStore()
    lessons = sorted([helps, misleads], key=lambda l: l.lesson_id)
    asyncio.run(reflexion.load_into(store, lessons, playbook_hash(lessons)))        # what `freeze` does

    llm = ScriptedChat(
        script=[AIMessage(content="", tool_calls=[{"name": "query_promql", "args": {"query": "payment_errors"}, "id": "d"}])],
        structured=HypothesisResult(hypothesis_id="h", verdict="supported", confidence_basis="flag",
                                    evidence=[EvidenceRef(evidence_id="promql:payment_errors", summary="up")]))
    from copilot.config import load_settings
    from copilot.run import run_incident
    from copilot.state import BudgetLimits
    from eval.runner.approvals import AutoApprover
    from tests.fake_shop import alert
    shop = Shop()
    res = asyncio.run(run_incident(
        alert(), AutoApprover("approve"), run_id="E3-after-D-001-r1-20261107T090000Z", limits=BudgetLimits(tool_calls=20),
        settings=load_settings().model_copy(update={"verify_window_s": 60, "verify_window_by_signal": {}}),
        options={"architecture": "C2", "memory": "playbook", "review_enabled": False}, toolbox=shop.toolbox(),
        llm_factory=lambda agent: llm, store=store))
    ctx = res.final_state["memory_context"]
    assert [l.lesson_id for l in ctx.applicable_lessons] == ["L-D-001-001"]          # found under a per-run run_id
    assert [r.lesson.lesson_id for r in ctx.rejected_lessons] == ["L-D-011-002"]     # misleading: precondition fails

    from eval.adapters.copilot_state import to_result_line
    now = datetime.now(timezone.utc)
    line = to_result_line(res.final_state, case_id="D-001", config_id="E3-after", started_at=now, ended_at=now,
                          tool_calls=res.tool_calls, tool_log_source="agent", max_tool_calls=20)
    assert line.lessons_used == ["L-D-001-001"]
    # the misleading lesson, and the fake shop's PM-001 postmortem (restart cart: not in play), are both reported
    assert line.lessons_rejected == ["L-D-011-002", "PM-001"]
