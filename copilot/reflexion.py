"""Offline Reflexion loop (capstone L12): scored dev runs → candidate lessons → a person's review → frozen playbook.

    python -m copilot.reflexion reflect eval/results/runs/DEV-C2-20261104T153515Z   # one candidate per failed run
    python -m copilot.reflexion review --reviewer Rohit                               # approve / edit / reject
    python -m copilot.reflexion freeze                                                # playbook.json + hash (+ Postgres)
    python -m copilot.reflexion load docs/playbook/playbook.json                      # same playbook into another Postgres

Code decides what failed (the checker's score line against the case's expected answer); the model only writes the
lesson and its preconditions. Every lesson needs a precondition `evaluate_lesson` can check from telemetry, or it can
never apply. Only dev cases (D-…) are read; a held-out case anywhere in the input stops the loop.

The review record is docs/playbook/candidates.jsonl (pending → approved / rejected, who, why). `freeze` turns the
approved ones into Lessons, writes docs/playbook/playbook.json with their hash, README.md and rejected.md, and loads
them into the shared (unprefixed) lesson namespace of the Postgres store, checking the store's hash matches.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field, ValidationError, model_validator

from copilot.config import ROOT, AppSettings, load_settings
from copilot.memory.backends import loop_factory
from copilot.memory.long_term import LongTermMemory, playbook_hash
from copilot.memory.preconditions import parse_duration
from copilot.memory.schemas import Lesson, Precondition
from copilot.state import ServiceName
from eval.schemas import catalogue
from eval.schemas.case import load_cases
from eval.telemetry import signals

PLAYBOOK_DIR = ROOT / "docs" / "playbook"
CANDIDATES = PLAYBOOK_DIR / "candidates.jsonl"
DEV_CASES = ROOT / "eval" / "cases" / "dev"
DEV_CASE = re.compile(r"^D-\d{3}$")
CASE_OR_RUN_REF = re.compile(r"\b[DH]-\d{3}\b|\bINC-\w+|\brun[_ -]?id\b", re.IGNORECASE)
MAX_LESSON_CHARS = 400
QUERY_SIGNALS = ("error_rate", "latency_p95_ms", "memory_mb", "cpu_percent", "consumer_lag")

# Checks a lesson can fix. Severity and verification honesty are decided by code, so no lesson can change them.
REFLECTABLE = [("verdict_correct", "wrong verdict (incident vs noise)"),
               ("root_cause_top1", "wrong top-1 root cause"),
               ("action_proposed_correct", "no correct action proposed"),
               ("action_executed_correct", "no correct action executed"),
               ("recovered", "the service did not recover")]


# ---------- the lesson the model writes ----------

def lesson_problems(text: str, preconditions: Iterable[Precondition]) -> list[str]:
    """Why a lesson cannot go into the playbook as written (empty = fine). Code, not the model, decides."""
    problems = []
    if CASE_OR_RUN_REF.search(text):
        problems.append("mentions a case or run; a lesson must say when it applies, not where it came from")
    if len(text) > MAX_LESSON_CHARS or len(re.findall(r"[.!?](\s|$)", text.strip())) > 2:
        problems.append(f"longer than two sentences / {MAX_LESSON_CHARS} characters")
    pres = list(preconditions)
    if not pres:
        problems.append("no precondition: a lesson without one can never apply")
    for p in pres:
        a = p.args
        if p.check == "recent_change":
            if ("flag" in a) == ("service" in a):
                problems.append("recent_change needs exactly one of flag / service")
            elif "flag" in a and a["flag"] not in catalogue.FAULT_TYPES:
                problems.append(f"recent_change flag {a['flag']!r} is not a known fault flag")
            try:
                parse_duration(str(a.get("within", "")))
            except ValueError:
                problems.append(f"recent_change within {a.get('within')!r} is not like 15m")
        else:
            if not isinstance(a.get("query"), str) or not a["query"].strip():
                problems.append(f"{p.check} needs a PromQL query")
            try:
                float(a.get("threshold"))
            except (TypeError, ValueError):
                problems.append(f"{p.check} threshold {a.get('threshold')!r} is not a number")
    return problems


class LessonDraft(BaseModel):
    text: str
    service: ServiceName
    fault_family: str
    preconditions: list[Precondition] = Field(min_length=1, max_length=3)
    failure_mode: str = ""

    @model_validator(mode="after")
    def _checkable(self) -> "LessonDraft":
        problems = lesson_problems(self.text, self.preconditions)
        if problems:                     # structured_call feeds this back to the model once
            raise ValueError("; ".join(problems))
        return self


# ---------- evaluator (code) ----------

def jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def failures(score: dict[str, Any]) -> list[str]:
    """What went wrong, from the checker's score line: every false check plus harmful / unnecessary actions."""
    out = [msg for field, msg in REFLECTABLE if score.get(field) is False]
    out += [f"harmful action: {json.dumps(a, sort_keys=True)}" for a in score.get("harmful_actions") or []]
    out += [f"unnecessary action: {json.dumps(a, sort_keys=True)}" for a in score.get("unnecessary_actions") or []]
    out += [f"policy violation: {v}" for v in score.get("policy_violations") or []]
    return out


def available_queries(services: Iterable[str]) -> dict[str, dict[str, str]]:
    """PromQL the telemetry server accepts (eval/telemetry/signals.py), for the services in question."""
    out: dict[str, dict[str, str]] = {}
    for svc in sorted({s for s in services if s}):
        qs = {sig: signals.promql(sig, svc, "5m") for sig in QUERY_SIGNALS if signals.supported(sig, svc)}
        if qs:
            out[svc] = qs
    return out


def failed_runs(run_dir: Path, cases_dir: Path = DEV_CASES) -> list[dict[str, Any]]:
    """One reflection input per failed dev run: verdict by code, expected answer, the run's trajectory."""
    if "heldout" in str(cases_dir).replace("-", "").lower():
        raise ValueError(f"{cases_dir}: lessons are built from dev cases only, never held-out")
    cases = {c.case_id: c for c in load_cases(cases_dir)}
    results = {r["run_id"]: r for r in jsonl(run_dir / "results.jsonl")}
    scores = jsonl(run_dir / "scores.jsonl")
    if not scores:
        raise FileNotFoundError(f"{run_dir / 'scores.jsonl'}: no scored runs")
    out = []
    for score in scores:
        case_id = score["case_id"]
        if not DEV_CASE.match(case_id):
            raise ValueError(f"run {score['run_id']}: case {case_id} is not a dev case; held-out runs never feed lessons")
        what = failures(score)
        result = results.get(score["run_id"])
        if score.get("task_success") or not what or result is None or case_id not in cases:
            continue
        expected = cases[case_id].expected
        hyps = result.get("hypotheses") or []
        explanation = result.get("explanation") or {}
        services = [cases[case_id].alert.service,
                    *(r.service for r in expected.root_causes), *(h.get("service") for h in hyps[:3])]
        out.append({
            "run_id": score["run_id"], "case_id": case_id, "failures": what,
            "expected": {"verdict": expected.verdict,
                         "root_causes": [r.model_dump(mode="json") for r in expected.root_causes],
                         "correct_actions": [[a.model_dump(mode="json") for a in alt] for alt in expected.correct_actions],
                         "forbidden_actions": [a.model_dump(mode="json") for a in expected.forbidden_actions]},
            "observed": {"verdict": result.get("verdict"), "final_status": result.get("final_status"),
                         "escalation_reason": result.get("escalation_reason"), "hypotheses": hyps,
                         "proposed_actions": result.get("proposed_actions") or [],
                         "lessons_used": result.get("lessons_used") or [], "replans": result.get("replans", 0),
                         "status_note": explanation.get("status_note"),
                         "timeline": [e.get("text") for e in (explanation.get("timeline") or [])][:40]},
            "available_queries": available_queries(services),
            "fault_flags": sorted(catalogue.FAULT_TYPES),
        })
    return out


# ---------- reflection (LLM) ----------

def spend_of(ledger: Any) -> dict[str, Any] | None:
    """Tokens and US dollars of one reflection (E3 reports what the lessons cost to make); None when unknown."""
    if ledger is None:
        return None
    total = ledger.total
    return {"tokens": total.tokens, "usd": round(total.usd, 6)}


def generation_cost(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """What writing the candidate lessons cost, over every reflection (approved, rejected and failed alike).
    A reflection whose cost is unknown (a failed call) is counted, never guessed."""
    known = [r["cost"] for r in rows if r.get("cost")]
    return {"reflections": len(rows), "with_cost": len(known),
            "tokens": sum(c.get("tokens", 0) for c in known),
            "usd": round(sum(c.get("usd", 0.0) for c in known), 6)}


async def reflect(run_dir: Path, cases_dir: Path = DEV_CASES, candidates: Path = CANDIDATES,
                  settings: AppSettings | None = None, llm_factory: Any = None) -> list[dict[str, Any]]:
    """Append one pending candidate per failed run not reflected on yet. Returns the new candidates."""
    from copilot.agents.base import HandoffError, structured_call
    from copilot.deps import CopilotDeps

    settings = settings or load_settings()
    deps = CopilotDeps(settings=settings, telemetry=None, runbooks=None, ops=None, tickets=None, store=None,
                       tools_factory=lambda agent: [], llm_factory=llm_factory)
    existing = jsonl(candidates)
    done = {c["source_run"] for c in existing}
    new = []
    for run in failed_runs(run_dir, cases_dir):
        if run["run_id"] in done:
            continue
        base = {"candidate_id": f"C-{run['case_id']}-{len(existing) + len(new) + 1:03d}", "source_run": run["run_id"],
                "case_id": run["case_id"], "failures": run["failures"], "expected": run["expected"]["root_causes"],
                "observed_top": (run["observed"]["hypotheses"] or [None])[0], "status": "pending",
                "reviewed_by": None, "reason": None, "decided_at": None, "lesson_id": None}
        try:
            draft, spent = await structured_call(deps, "reflexion", "reflexion", LessonDraft, run)
            base |= {"lesson": draft.model_dump(mode="json", exclude={"failure_mode"}), "failure_mode": draft.failure_mode,
                     "cost": spend_of(spent)}
        except HandoffError as exc:       # kept, so the reviewer sees the failure; it can only be rejected or edited
            base |= {"lesson": None, "failure_mode": "", "reflection_error": str(exc), "cost": None}
        new.append(base)
    write_candidates(candidates, existing + new)
    return new


def write_candidates(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")


# ---------- human review ----------

def decide(candidate: dict[str, Any], decision: str, reviewer: str, reason: str = "",
           edited: dict[str, Any] | None = None, lesson_id: str | None = None) -> dict[str, Any]:
    """A person's decision on one candidate. Approve / edit must leave a checkable lesson; reject needs a reason."""
    if not reviewer.strip():
        raise ValueError("a person must be named as the reviewer")
    if candidate["status"] != "pending":
        raise ValueError(f"{candidate['candidate_id']} was already {candidate['status']}")
    now = datetime.now(timezone.utc).isoformat()
    if decision == "reject":
        if not reason.strip():
            raise ValueError("a rejection needs its reason (the guide asks for it as evidence)")
        return candidate | {"status": "rejected", "reviewed_by": reviewer, "reason": reason.strip(), "decided_at": now}
    if decision not in ("approve", "edit"):
        raise ValueError(f"unknown decision {decision!r}")
    lesson = dict(candidate.get("lesson") or {}) | (edited or {})
    try:
        checked = LessonDraft.model_validate(lesson)
    except ValidationError as exc:
        raise ValueError(f"not a checkable lesson: {exc.errors()[0]['msg']}") from exc
    return candidate | {"status": "approved", "reviewed_by": reviewer, "reason": reason.strip() or None,
                        "decided_at": now, "edited": decision == "edit",
                        "lesson": checked.model_dump(mode="json", exclude={"failure_mode"}),
                        "lesson_id": lesson_id or f"L-{candidate['case_id']}-{candidate['candidate_id'][-3:]}"}


def to_lesson(candidate: dict[str, Any]) -> Lesson:
    lesson = candidate["lesson"]
    return Lesson(lesson_id=candidate["lesson_id"], text=lesson["text"], preconditions=lesson["preconditions"],
                  service=lesson["service"], fault_family=lesson["fault_family"],
                  source_incident=candidate["source_run"], reviewed_by=candidate["reviewed_by"], status="active")


def show(c: dict[str, Any]) -> str:
    lesson = c.get("lesson") or {}
    pres = "\n".join(f"      - {p['check']} {json.dumps(p['args'])}" for p in lesson.get("preconditions") or [])
    return (f"\n{c['candidate_id']}  from {c['source_run']}\n"
            f"  failed:   {'; '.join(c['failures'])}\n"
            f"  expected: {json.dumps(c['expected'])}\n"
            f"  lesson:   {lesson.get('text') or '(reflection failed: ' + c.get('reflection_error', '') + ')'}\n"
            f"  about:    {lesson.get('service')} / {lesson.get('fault_family')}\n  preconditions:\n{pres}")


def review_cli(candidates: Path, reviewer: str, ask: Any = input, say: Any = print) -> None:
    rows = jsonl(candidates)
    for i, c in enumerate(rows):
        if c["status"] != "pending":
            continue
        say(show(c))
        choice = ask("  [a]pprove / [e]dit / [r]eject / [s]kip / [q]uit? ").strip().lower()[:1]
        try:
            if choice == "q":
                break
            if choice == "a":
                rows[i] = decide(c, "approve", reviewer)
            elif choice == "e":
                edited: dict[str, Any] = {}
                text = ask("  new text (blank = keep): ").strip()
                if text:
                    edited["text"] = text
                pres = ask("  new preconditions as JSON list (blank = keep): ").strip()
                if pres:
                    edited["preconditions"] = json.loads(pres)
                rows[i] = decide(c, "edit", reviewer, reason=ask("  why edited (optional): "), edited=edited)
            elif choice == "r":
                rows[i] = decide(c, "reject", reviewer, reason=ask("  reason: "))
        except (ValueError, json.JSONDecodeError) as exc:
            say(f"  not recorded: {exc}")
            continue
        write_candidates(candidates, rows)               # every decision is saved at once
        write_rejected(candidates.parent / "rejected.md", rows)


# ---------- freeze ----------

def write_rejected(path: Path, rows: list[dict[str, Any]]) -> None:
    lines = ["# Rejected lessons", "", "Lessons the reflection step proposed from dev runs that a person rejected, "
             "with the reason (capstone L12 evidence).", ""]
    for c in (r for r in rows if r["status"] == "rejected"):
        lesson = c.get("lesson") or {}
        lines += [f"## {c['candidate_id']} (from {c['source_run']})", "",
                  f"- **Lesson:** {lesson.get('text') or '(reflection failed)'}",
                  f"- **Preconditions:** `{json.dumps(lesson.get('preconditions') or [])}`",
                  f"- **Failure it came from:** {'; '.join(c['failures'])}",
                  f"- **Rejected by:** {c['reviewed_by']} on {c['decided_at'][:10]}",
                  f"- **Reason:** {c['reason']}", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


async def load_into(store: Any, lessons: list[Lesson], expected_hash: str) -> None:
    """Make the store's shared lesson namespace hold exactly these lessons, and check its hash."""
    mem = LongTermMemory(store)
    keep = {l.lesson_id for l in lessons}
    for old in await mem.list_lessons(include_withdrawn=True):
        if old.lesson_id not in keep and old.status == "active":
            await mem.withdraw_lesson(old.service, old.fault_family, old.lesson_id)
    await mem.load_playbook(lessons)
    _, got = await mem.export_playbook()
    if got != expected_hash:
        raise RuntimeError(f"the store's playbook hash {got[:12]} is not the frozen {expected_hash[:12]}")


async def freeze(candidates: Path = CANDIDATES, out_dir: Path = PLAYBOOK_DIR, store: Any = None,
                 dsn: str | None = None) -> tuple[list[Lesson], str]:
    rows = jsonl(candidates)
    if any(r["status"] == "pending" for r in rows):
        raise ValueError("review every candidate before freezing (pending ones remain)")
    lessons = sorted((to_lesson(r) for r in rows if r["status"] == "approved"), key=lambda l: l.lesson_id)
    if not lessons:
        raise ValueError("no approved lessons to freeze")
    digest = playbook_hash(lessons)
    now = datetime.now(timezone.utc)
    reviewers = sorted({l.reviewed_by for l in lessons})
    cost = generation_cost(rows)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "playbook.json").write_text(json.dumps({
        "playbook_hash": digest, "frozen_at": now.isoformat(), "reviewed_by": reviewers,
        "source_runs": sorted({l.source_incident for l in lessons}),
        "generation_cost": cost,
        "lessons": [l.model_dump(mode="json") for l in lessons]}, indent=2) + "\n", encoding="utf-8")
    write_rejected(out_dir / "rejected.md", rows)
    (out_dir / "README.md").write_text("\n".join([
        "# Frozen playbook", "",
        f"- **Hash:** `{digest}`", f"- **Frozen:** {now:%Y-%m-%d %H:%M} UTC",
        f"- **Reviewed by:** {', '.join(reviewers)}",
        f"- **Lessons:** {len(lessons)} approved, {sum(r['status'] == 'rejected' for r in rows)} rejected "
        "(see `rejected.md`)",
        f"- **Cost of writing the lessons:** {cost['tokens']} tokens, ${cost['usd']:.4f} over {cost['reflections']} "
        f"reflection(s) ({cost['reflections'] - cost['with_cost']} without a known cost); review time not included", "",
        "Built by `python -m copilot.reflexion` from dev runs only. Every lesson was approved by a person and has "
        "preconditions the graph checks against live telemetry before using it.", "",
        "Held-out and experiment runs read it with `memory: playbook` (read-only). To put it into another Postgres:",
        "", "```bash", "POSTGRES_DSN=... python -m copilot.reflexion load docs/playbook/playbook.json", "```", "",
        "Any change to a lesson gives a new hash, and held-out runs made with the old one must be redone.", ""]),
        encoding="utf-8")
    await _into_store(lessons, digest, store, dsn)
    return lessons, digest


def read_playbook(path: Path) -> tuple[list[Lesson], str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    lessons = [Lesson.model_validate(l) for l in data["lessons"]]
    if playbook_hash(lessons) != data["playbook_hash"]:
        raise ValueError(f"{path}: lessons do not match the recorded hash; the file was changed after freezing")
    return lessons, data["playbook_hash"]


async def _into_store(lessons: list[Lesson], digest: str, store: Any, dsn: str | None) -> None:
    if store is not None:
        await load_into(store, lessons, digest)
        return
    if not dsn:
        print("POSTGRES_DSN is not set: the playbook is in the repo only. Run `load` where Postgres runs.")
        return
    from copilot.memory import make_store

    async with make_store(dsn) as pg:
        await load_into(pg, lessons, digest)


# ---------- CLI ----------

def main(argv: list[str] | None = None) -> None:
    from dotenv import load_dotenv

    from copilot import tracing

    load_dotenv(ROOT / ".env")
    tracing.setup()                  # LangSmith, masked; off unless LANGSMITH_TRACING=true (Person 5)
    p = argparse.ArgumentParser(prog="python -m copilot.reflexion", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reflect", help="one candidate lesson per failed dev run")
    r.add_argument("run_dir", type=Path)
    r.add_argument("--cases", type=Path, default=DEV_CASES)
    r.add_argument("--candidates", type=Path, default=CANDIDATES)
    v = sub.add_parser("review", help="approve / edit / reject each pending candidate")
    v.add_argument("--reviewer", required=True)
    v.add_argument("--candidates", type=Path, default=CANDIDATES)
    f = sub.add_parser("freeze", help="write the playbook and its hash; load it into Postgres")
    f.add_argument("--candidates", type=Path, default=CANDIDATES)
    f.add_argument("--out", type=Path, default=PLAYBOOK_DIR)
    lo = sub.add_parser("load", help="load a frozen playbook.json into POSTGRES_DSN (hash checked)")
    lo.add_argument("playbook", type=Path)
    a = p.parse_args(argv)
    dsn = os.getenv("POSTGRES_DSN") or None
    if a.cmd == "reflect":
        new = asyncio.run(reflect(a.run_dir, a.cases, a.candidates))
        print(f"{len(new)} new candidate(s) in {a.candidates}; next: review --reviewer <name>")
    elif a.cmd == "review":
        review_cli(a.candidates, a.reviewer)
    elif a.cmd == "freeze":
        lessons, digest = asyncio.run(freeze(a.candidates, a.out, dsn=dsn), loop_factory=loop_factory(dsn))
        print(f"froze {len(lessons)} lesson(s); playbook hash {digest}")
    else:
        lessons, digest = read_playbook(a.playbook)
        if not dsn:
            raise SystemExit("POSTGRES_DSN is not set")
        asyncio.run(_into_store(lessons, digest, None, dsn), loop_factory=loop_factory(dsn))
        print(f"loaded {len(lessons)} lesson(s); playbook hash {digest}")


if __name__ == "__main__":
    main()
