"""Typed outputs for every tool (system design section 6).

The shape of a successful `result` is a Pydantic model per tool. The servers wrap each tool with
`typed_output`, so a tool that returns the wrong shape becomes {ok: false, error: output_schema_error}
instead of silently handing the graph something it cannot parse. Models allow extra keys (a new
field is not a break), but the listed fields are required and typed. Errors are always
{ok: false, error: {code, message}} (ErrorInfo / Envelope).
"""
from __future__ import annotations

import functools
from typing import Any, Callable, Dict, List, Optional, Type

from pydantic import BaseModel, ConfigDict, ValidationError


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class ErrorInfo(BaseModel):
    code: str
    message: str = ""


class Envelope(_Open):
    ok: bool
    result: Optional[Any] = None
    error: Optional[ErrorInfo] = None


# ---- telemetry
class Series(_Open):
    labels: Dict[str, Any]
    values: List[List[Any]]       # [[unix_ts, value], ...]; value None when NaN/inf


class PromResult(_Open):
    series: List[Series]
    truncated: bool
    evidence_id: str


class LogLine(_Open):
    evidence_id: str
    time: Optional[Any] = None
    text: str


class LogsResult(_Open):
    service: str
    count: int
    lines: List[LogLine]
    notice: str


class TraceSummary(_Open):
    evidence_id: str
    trace_id: Optional[str] = None
    span_count: int
    services: List[str]
    failed_spans: List[Dict[str, Any]]


class TracesResult(_Open):
    service: str
    count: int
    traces: List[TraceSummary]
    notice: str


class FlagChange(_Open):
    flag: str
    variant: str
    at: str                      # ISO-8601
    first_seen_age_s: int
    evidence_id: str


class ChangesResult(_Open):
    since_s: int
    changes: List[FlagChange]
    deploys: List[Dict[str, Any]]


# ---- runbooks_kb
class Passage(_Open):
    score: Optional[float] = None    # rank-derived (1/(1+rank)) on search; not a model score
    passage_id: str
    doc_id: str
    section: str
    title: str
    text: str
    sha256: str


class SearchResult(_Open):
    count: int
    passages: List[Passage]
    notice: str


class RunbookResult(_Open):
    runbook_id: str
    title: str
    passages: List[Passage]
    notice: str


# ---- tickets
class IncidentRef(_Open):
    incident_id: str


class IncidentRecord(_Open):
    incident_id: str
    alert_name: str
    status: str
    timeline: List[Dict[str, Any]]


class StatusDraft(_Open):
    text: str
    payload: Dict[str, Any]
    payload_hash: str


class StatusPublished(_Open):
    status_id: str
    payload_hash: str
    published: bool


# ---- ops_actions
class DryRunResult(_Open):
    payload_hash: str
    action: Dict[str, Any]
    would_do: str
    current_state: Dict[str, Any]
    expected_effect: str


class ActionResult(_Open):
    action_id: str
    payload_hash: str


OUTPUT_MODELS: Dict[str, Type[BaseModel]] = {
    "telemetry.query_promql": PromResult, "telemetry.search_logs": LogsResult,
    "telemetry.get_traces": TracesResult, "telemetry.recent_changes": ChangesResult,
    "runbooks_kb.search": SearchResult, "runbooks_kb.get": RunbookResult,
    "tickets.create_incident": IncidentRef, "tickets.append_timeline": IncidentRef,
    "tickets.save_postmortem": IncidentRef, "tickets.get_incident": IncidentRecord,
    "tickets.draft_status": StatusDraft, "tickets.publish_status": StatusPublished,
    "ops_actions.dry_run": DryRunResult, "ops_actions.set_flag": ActionResult,
    "ops_actions.restart_service": ActionResult, "ops_actions.scale_service": ActionResult,
    "ops_actions.rollback": ActionResult,
}


def check_output(server: str, tool: str, envelope: Any) -> Any:
    """Return the envelope unchanged if it is valid for this tool, else an output_schema_error."""
    from mcp_servers.common import err
    try:
        env = Envelope.model_validate(envelope)
    except ValidationError as exc:
        return err("output_schema_error", "not a {ok, result|error} envelope: %s" % str(exc)[:200])
    if not env.ok:
        if env.error is None:
            return err("output_schema_error", "ok is false but there is no error object")
        return envelope
    model = OUTPUT_MODELS.get("%s.%s" % (server, tool))
    if model is None:
        return envelope
    try:
        model.model_validate(env.result)
    except ValidationError as exc:
        return err("output_schema_error", "%s.%s returned the wrong shape: %s" % (server, tool, str(exc)[:200]))
    return envelope


def typed_output(server: str, tool: str) -> Callable:
    """Decorator for a FastMCP tool function: validate its result before it leaves the server."""
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return check_output(server, tool, fn(*args, **kwargs))
        return wrapper
    return deco
