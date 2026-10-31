"""SQLite-backed reviewed lesson memory with fail-closed applicability checks."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from ..schemas.catalogue import SERVICES


@dataclass(frozen=True)
class Lesson:
    lesson_id: str
    text: str
    applies_to: dict
    preconditions: tuple[dict, ...]
    source_incident: str
    reviewed_by: Optional[str]
    status: str


@dataclass(frozen=True)
class LessonDecision:
    lesson: Lesson
    applicable: bool
    reason: str


def _matches(predicate: dict, context: dict) -> tuple[bool, str]:
    """Evaluate only the small, explicit predicate language; unknown data fails closed."""
    check = predicate.get("check")
    if check == "service_is":
        expected = predicate.get("value")
        actual = context.get("service")
        return actual == expected, f"service is {actual!r}, expected {expected!r}"
    if check == "fault_family_is":
        expected = predicate.get("value")
        actual = context.get("fault_family")
        return actual == expected, f"fault_family is {actual!r}, expected {expected!r}"
    if check == "recent_change":
        flag = predicate.get("flag")
        within_minutes = predicate.get("within_minutes")
        changes = context.get("recent_changes")
        if (not isinstance(flag, str) or isinstance(within_minutes, bool)
                or not isinstance(within_minutes, (int, float)) or within_minutes < 0
                or not isinstance(changes, dict)):
            return False, "recent-change evidence is missing or malformed"
        age = changes.get(flag)
        ok = (isinstance(age, (int, float)) and not isinstance(age, bool)
              and 0 <= age <= within_minutes)
        return ok, f"recent change age for {flag} is {age!r} minutes"
    if check == "signal":
        signal = predicate.get("name")
        operator = predicate.get("operator")
        threshold = predicate.get("threshold")
        signals = context.get("signals")
        if (not isinstance(signal, str) or operator not in {"gt", "gte", "lt", "lte"}
                or isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                or not isinstance(signals, dict)):
            return False, "signal evidence is missing or malformed"
        actual = signals.get(signal)
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return False, f"signal {signal!r} is unavailable"
        ok = {"gt": actual > threshold, "gte": actual >= threshold,
              "lt": actual < threshold, "lte": actual <= threshold}[operator]
        return ok, f"signal {signal}={actual} {operator} {threshold} is {ok}"
    return False, f"unsupported precondition {check!r}"


class MemoryStore:
    """Store proposed lessons separately from human-reviewed durable lessons."""

    def __init__(self, path: str | Path = ":memory:"):
        self._db = sqlite3.connect(str(path))
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS lessons (
                lesson_id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                applies_to TEXT NOT NULL,
                preconditions TEXT NOT NULL,
                source_incident TEXT NOT NULL,
                reviewed_by TEXT,
                status TEXT NOT NULL CHECK(status IN ('pending', 'active', 'withdrawn', 'rejected'))
            )
        """)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def propose(self, lesson_id: str, text: str, applies_to: dict,
                preconditions: list[dict], source_incident: str) -> None:
        if not lesson_id.strip() or not text.strip() or not source_incident.strip():
            raise ValueError("lesson id, text, and source incident are required")
        self._validate_scope(applies_to)
        self._validate_preconditions(preconditions)
        self._db.execute(
            "INSERT INTO lessons VALUES (?, ?, ?, ?, ?, NULL, 'pending')",
            (lesson_id, text, json.dumps(applies_to, sort_keys=True),
             json.dumps(preconditions, sort_keys=True), source_incident),
        )
        self._db.commit()

    def review(self, lesson_id: str, reviewer: str, decision: str,
               text: Optional[str] = None, applies_to: Optional[dict] = None,
               preconditions: Optional[list[dict]] = None) -> None:
        if not reviewer.strip():
            raise ValueError("reviewer identity is required")
        if decision not in {"approve", "edit", "reject"}:
            raise ValueError("decision must be approve, edit, or reject")
        if decision == "edit":
            if not text or not text.strip() or not isinstance(applies_to, dict) or not preconditions:
                raise ValueError("edit decisions require revised text, scope, and preconditions")
            self._validate_scope(applies_to)
            self._validate_preconditions(preconditions)
        status = "active" if decision in {"approve", "edit"} else "rejected"
        if decision == "edit":
            cursor = self._db.execute(
                "UPDATE lessons SET status = ?, reviewed_by = ?, text = ?, applies_to = ?, preconditions = ? "
                "WHERE lesson_id = ? AND status = 'pending'",
                (status, reviewer, text, json.dumps(applies_to, sort_keys=True),
                 json.dumps(preconditions, sort_keys=True), lesson_id),
            )
        else:
            cursor = self._db.execute(
                "UPDATE lessons SET status = ?, reviewed_by = ? WHERE lesson_id = ? AND status = 'pending'",
                (status, reviewer, lesson_id),
            )
        if cursor.rowcount != 1:
            self._db.rollback()
            raise ValueError(f"lesson {lesson_id!r} is not awaiting review")
        self._db.commit()

    @staticmethod
    def _validate_scope(applies_to: dict) -> None:
        if (not isinstance(applies_to, dict)
                or applies_to.get("service") not in SERVICES
                or not isinstance(applies_to.get("fault_family"), str)
                or not applies_to["fault_family"].strip()):
            raise ValueError("lesson scope requires a known service and fault_family")

    @staticmethod
    def _validate_preconditions(preconditions: list[dict]) -> None:
        allowed = {"service_is", "fault_family_is", "recent_change", "signal"}
        if not preconditions:
            raise ValueError("lessons require at least one applicability precondition")
        for item in preconditions:
            if not isinstance(item, dict) or item.get("check") not in allowed:
                raise ValueError("lesson contains an unsupported or malformed precondition")
            check = item["check"]
            if check in {"service_is", "fault_family_is"}:
                valid = isinstance(item.get("value"), str) and bool(item["value"].strip())
            elif check == "recent_change":
                valid = (isinstance(item.get("flag"), str) and bool(item["flag"])
                         and isinstance(item.get("within_minutes"), (int, float))
                         and not isinstance(item.get("within_minutes"), bool)
                         and item["within_minutes"] >= 0)
            else:
                valid = (isinstance(item.get("name"), str) and bool(item["name"])
                         and item.get("operator") in {"gt", "gte", "lt", "lte"}
                         and isinstance(item.get("threshold"), (int, float))
                         and not isinstance(item.get("threshold"), bool))
            if not valid:
                raise ValueError(f"lesson contains a malformed {check} precondition")

    def load_proposals(self, path: str | Path) -> int:
        """Load seed lesson proposals as pending; never silently activate them."""
        entries = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("lesson proposal file must contain a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("lesson proposal must be a mapping")
            self._validate_preconditions(entry.get("preconditions", []))
            self.propose(
                lesson_id=entry["lesson_id"],
                text=entry["text"],
                applies_to=entry["applies_to"],
                preconditions=entry["preconditions"],
                source_incident=entry["source_incident"],
            )
        return len(entries)

    def withdraw(self, lesson_id: str, reviewer: str) -> None:
        if not reviewer.strip():
            raise ValueError("reviewer identity is required")
        cursor = self._db.execute(
            "UPDATE lessons SET status = 'withdrawn', reviewed_by = ? WHERE lesson_id = ? AND status = 'active'",
            (reviewer, lesson_id),
        )
        if cursor.rowcount != 1:
            self._db.rollback()
            raise ValueError(f"active lesson {lesson_id!r} not found")
        self._db.commit()

    def search(self, service: str, fault_family: str, context: dict,
               enabled: bool = True) -> list[LessonDecision]:
        """Return applicable reviewed lessons; preserve rejection reasons for mismatches."""
        if not enabled:
            return []
        rows = self._db.execute(
            "SELECT lesson_id, text, applies_to, preconditions, source_incident, reviewed_by, status "
            "FROM lessons WHERE status = 'active' ORDER BY lesson_id"
        ).fetchall()
        decisions = []
        context = {**context, "service": service, "fault_family": fault_family}
        for row in rows:
            applies = json.loads(row[2])
            if applies.get("service") not in {None, service} or applies.get("fault_family") not in {None, fault_family}:
                continue
            lesson = Lesson(row[0], row[1], applies, tuple(json.loads(row[3])),
                            row[4], row[5], row[6])
            checks = [_matches(item, context) for item in lesson.preconditions]
            failures = [reason for ok, reason in checks if not ok]
            decisions.append(LessonDecision(
                lesson=lesson,
                applicable=not failures,
                reason="all preconditions matched" if not failures else "; ".join(failures),
            ))
        return decisions
