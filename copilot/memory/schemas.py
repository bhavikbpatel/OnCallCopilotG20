"""Long-term memory records. See agent_graph_design.md §4.2.

Plain str for service / fault_family (not the enums in copilot.state) so the
store stays readable by role 2's tooling and survives enum changes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class TimelineEntry(BaseModel):
    at: datetime
    node: str
    kind: str
    text: str


class IncidentRecord(BaseModel):
    incident_id: str
    alert_name: str
    services: list[str]
    fault_family: str
    symptom_text: str              # embedded for similarity search
    root_cause: str
    action: dict[str, Any] | None  # Action.model_dump(); kept untyped to avoid a state import cycle
    outcome: Literal["recovered", "rolled_back", "escalated", "noise"]
    timeline: list[TimelineEntry] = Field(default_factory=list)
    reviewed_by: str
    stored_at: datetime


class Precondition(BaseModel):
    check: Literal["recent_change", "promql_above", "promql_below"]
    args: dict[str, Any]
    # recent_change: {"flag": "paymentFailure" | "service": "payment", "within": "15m"}
    # promql_above / promql_below: {"query": "...", "threshold": 0.05}


class Lesson(BaseModel):
    lesson_id: str
    text: str
    preconditions: list[Precondition]
    service: str
    fault_family: str
    source_incident: str
    reviewed_by: str
    status: Literal["active", "withdrawn"] = "active"


class EngineerPref(BaseModel):
    pref_id: str
    text: str
    created_at: datetime


class PreconditionResult(BaseModel):
    check: str
    args: dict[str, Any]
    passed: bool
    detail: str


class RejectedLesson(BaseModel):
    lesson: Lesson
    results: list[PreconditionResult]
    reason: str


class MemoryContext(BaseModel):
    similar_incidents: list[IncidentRecord] = Field(default_factory=list)
    applicable_lessons: list[Lesson] = Field(default_factory=list)
    rejected_lessons: list[RejectedLesson] = Field(default_factory=list)
    engineer_prefs: list[EngineerPref] = Field(default_factory=list)
