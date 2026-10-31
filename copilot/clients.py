"""Interfaces the graph needs from the MCP servers (implemented by role 4 over langchain-mcp-adapters).

Clients raise ToolError when a tool returns {ok: false, error: {code, message}}.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel

from copilot.memory.preconditions import TelemetryClient as _PreconditionTelemetry
from copilot.state import Action, Alert, DryRunResult, Postmortem, Severity


class ToolError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class ServiceHealth(BaseModel):
    service: str
    error_rate: float
    latency_p95_ms: float | None = None


class Passage(BaseModel):
    passage_id: str             # doc_id#section
    doc_id: str
    text: str
    score: float


class TelemetryClient(_PreconditionTelemetry, Protocol):
    async def service_health(self, window: str = "5m") -> list[ServiceHealth]: ...


class RunbooksClient(Protocol):
    async def search(self, query: str, service: str | None = None, symptom: str | None = None,
                     k: int = 5) -> list[Passage]: ...


class OpsClient(Protocol):
    async def dry_run(self, action: Action) -> DryRunResult: ...

    async def execute(self, action: Action, approval_token: str) -> dict[str, Any]: ...


class TicketsClient(Protocol):
    async def create_incident(self, incident_id: str, alert: Alert, severity: Severity) -> None: ...

    async def append_timeline(self, incident_id: str, at: datetime, text: str) -> None: ...

    async def save_postmortem(self, incident_id: str, postmortem: Postmortem) -> None: ...
