"""Long-term memory over a LangGraph store. See agent_graph_design.md §4.2–4.3.

Namespaces (optionally prefixed by run_id for eval isolation):
    ("incidents",)                         key incident_id
    ("lessons", service, fault_family)     key lesson_id
    ("engineer_prefs", engineer_id)        key pref_id
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from langgraph.store.base import BaseStore

from copilot.memory.preconditions import TelemetryClient, evaluate_lesson
from copilot.memory.schemas import (
    EngineerPref,
    IncidentRecord,
    Lesson,
    MemoryContext,
    RejectedLesson,
)

SIMILAR_INCIDENTS_K = 3
_SEARCH_LIMIT = 100


class MemoryReadOnlyError(RuntimeError):
    pass


class LongTermMemory:
    """store=None → memory disabled (E4 'memory off'): reads return empty, writes are no-ops."""

    def __init__(self, store: BaseStore | None, run_id: str | None = None, read_only: bool = False):
        self.store = store
        self.run_id = run_id
        self.read_only = read_only

    # ---------- namespaces ----------

    def _ns(self, *parts: str) -> tuple[str, ...]:
        return ((self.run_id,) if self.run_id else ()) + parts

    def _writable(self) -> bool:
        if self.read_only:
            raise MemoryReadOnlyError("long-term memory is frozen for this run")
        return self.store is not None

    # ---------- incidents ----------

    async def search_incidents(
        self, symptom: str, services: list[str] | None = None, fault_family: str | None = None,
        k: int = SIMILAR_INCIDENTS_K,
    ) -> list[IncidentRecord]:
        if self.store is None:
            return []
        filt = {"fault_family": fault_family} if fault_family else None
        items = await self.store.asearch(self._ns("incidents"), query=symptom or None, filter=filt, limit=_SEARCH_LIMIT)
        records = [IncidentRecord.model_validate(i.value) for i in items]
        if services:
            wanted = set(services)
            records = [r for r in records if wanted & set(r.services)]
        return records[:k]

    async def get_incident(self, incident_id: str) -> IncidentRecord | None:
        if self.store is None:
            return None
        item = await self.store.aget(self._ns("incidents"), incident_id)
        return IncidentRecord.model_validate(item.value) if item else None

    async def save_incident(self, record: IncidentRecord) -> None:
        if not self._writable():
            return
        await self.store.aput(self._ns("incidents"), record.incident_id, record.model_dump(mode="json"))

    async def delete_incident(self, incident_id: str) -> None:
        if not self._writable():
            return
        await self.store.adelete(self._ns("incidents"), incident_id)

    # ---------- lessons ----------

    async def list_lessons(self, service: str | None = None, fault_family: str | None = None,
                           include_withdrawn: bool = False) -> list[Lesson]:
        if self.store is None:
            return []
        prefix = self._ns("lessons", *(p for p in (service, fault_family) if p)) if service else self._ns("lessons")
        filt = None if include_withdrawn else {"status": "active"}
        items = await self.store.asearch(prefix, filter=filt, limit=_SEARCH_LIMIT)
        lessons = [Lesson.model_validate(i.value) for i in items]
        if fault_family and not service:
            lessons = [l for l in lessons if l.fault_family == fault_family]
        return lessons

    async def save_lesson(self, lesson: Lesson) -> None:
        if not self._writable():
            return
        await self.store.aput(
            self._ns("lessons", lesson.service, lesson.fault_family), lesson.lesson_id,
            lesson.model_dump(mode="json"), index=False,
        )

    async def withdraw_lesson(self, service: str, fault_family: str, lesson_id: str) -> bool:
        if not self._writable():
            return False
        ns = self._ns("lessons", service, fault_family)
        item = await self.store.aget(ns, lesson_id)
        if item is None:
            return False
        await self.store.aput(ns, lesson_id, {**item.value, "status": "withdrawn"}, index=False)
        return True

    # ---------- engineer prefs ----------

    async def list_prefs(self, engineer_id: str | None) -> list[EngineerPref]:
        if self.store is None or not engineer_id:
            return []
        items = await self.store.asearch(self._ns("engineer_prefs", engineer_id), limit=_SEARCH_LIMIT)
        return sorted((EngineerPref.model_validate(i.value) for i in items), key=lambda p: p.created_at)

    async def add_pref(self, engineer_id: str, text: str) -> EngineerPref | None:
        """Called only from an explicit UI/API action — never from model output."""
        if not self._writable():
            return None
        pref = EngineerPref(pref_id=uuid.uuid4().hex[:12], text=text.strip(), created_at=datetime.now(timezone.utc))
        await self.store.aput(self._ns("engineer_prefs", engineer_id), pref.pref_id, pref.model_dump(mode="json"), index=False)
        return pref

    async def delete_pref(self, engineer_id: str, pref_id: str) -> None:
        if not self._writable():
            return
        await self.store.adelete(self._ns("engineer_prefs", engineer_id), pref_id)

    # ---------- graph read point (node 3.7 Runbook) ----------

    async def build_memory_context(
        self, *, symptom: str, suspects: list[str], fault_families: list[str] | None,
        telemetry: TelemetryClient, engineer_id: str | None = None, now: datetime | None = None,
    ) -> MemoryContext:
        """Similar incidents + lessons split by code-checked preconditions + prefs."""
        ctx = MemoryContext(engineer_prefs=await self.list_prefs(engineer_id))
        if self.store is None:
            return ctx
        ctx.similar_incidents = await self.search_incidents(symptom, services=suspects)

        seen: set[str] = set()
        for service in suspects:
            lessons = await self.list_lessons(service=service)
            for lesson in lessons:
                if lesson.lesson_id in seen or (fault_families and lesson.fault_family not in fault_families):
                    continue
                seen.add(lesson.lesson_id)
                ok, results = await evaluate_lesson(lesson, telemetry, now)
                if ok:
                    ctx.applicable_lessons.append(lesson)
                else:
                    failed = [r.detail for r in results if not r.passed] or ["lesson has no preconditions"]
                    ctx.rejected_lessons.append(RejectedLesson(lesson=lesson, results=results, reason="; ".join(failed)))
        return ctx

    # ---------- frozen playbook (E3) ----------

    async def export_playbook(self) -> tuple[list[Lesson], str]:
        """All active lessons + a stable hash; store the hash in the run record before held-out."""
        lessons = sorted(await self.list_lessons(), key=lambda l: l.lesson_id)
        return lessons, playbook_hash(lessons)

    async def load_playbook(self, lessons: list[Lesson]) -> None:
        for lesson in lessons:
            await self.save_lesson(lesson)


def playbook_hash(lessons: list[Lesson]) -> str:
    payload = json.dumps([l.model_dump(mode="json") for l in sorted(lessons, key=lambda l: l.lesson_id)],
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
