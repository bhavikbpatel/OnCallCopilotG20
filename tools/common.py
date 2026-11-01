"""Shared helpers for the tool servers: result shape, read/write kinds, secret redaction.

Every tool returns {"ok": True, "data": ...} or {"ok": False, "error": {"code", "message"}}
and never raises, so one bad call cannot crash an agent run.
"""
from __future__ import annotations

import inspect
import os
import re
from typing import Any, Dict, Optional

READ = "read"
WRITE = "write"

# Single source of truth for read/write marking (system design section 6).
# Keys are "<server>.<tool>". Hooks use this to fill ToolCall.kind and to enforce permissions.
TOOL_KINDS: Dict[str, str] = {
    "telemetry.query_promql": READ,
    "telemetry.search_logs": READ,
    "telemetry.get_traces": READ,
    "telemetry.recent_changes": READ,
    "runbooks-kb.search": READ,
    "runbooks-kb.get": READ,
    "tickets.get_incident": READ,
    "tickets.draft_status": READ,
    "tickets.create_incident": WRITE,      # internal: writes only our incident DB
    "tickets.append_timeline": WRITE,      # internal
    "tickets.save_postmortem": WRITE,      # internal
    "tickets.publish_status": WRITE,       # external: needs approval
    "ops-actions.dry_run": READ,           # no side effects
    "ops-actions.set_flag": WRITE,
    "ops-actions.restart_service": WRITE,
    "ops-actions.scale_service": WRITE,
    "ops-actions.rollback": WRITE,
}


def kind_of(server: str, tool: str) -> Optional[str]:
    return TOOL_KINDS.get("%s.%s" % (server, tool))


def marks(server: str, tool: str) -> Dict[str, Any]:
    """kwargs for @mcp.tool(): mark the tool read or write in the MCP metadata (tags + annotations),
    from the one TOOL_KINDS table. Unknown tools are marked write (fail closed). Only arguments the
    installed FastMCP accepts are passed, so an older or newer FastMCP cannot break server start-up."""
    kind = kind_of(server, tool) or WRITE
    wanted = {"tags": {kind}, "annotations": {"readOnlyHint": kind == READ, "destructiveHint": kind != READ}}
    try:
        from fastmcp import FastMCP
        accepted = set(inspect.signature(FastMCP.tool).parameters)
    except Exception:
        return {}
    return {k: v for k, v in wanted.items() if k in accepted}


DEFAULT_PORTS = {"telemetry": 8101, "runbooks_kb": 8102, "tickets": 8103, "ops_actions": 8104}


def run_server(mcp: Any, name: str) -> None:
    """stdio by default. MCP_TRANSPORT=http serves streamable HTTP for Docker/Compose, on
    MCP_HOST (default 127.0.0.1; use 0.0.0.0 inside a container) and MCP_PORT (default per server,
    matching the ports in Person 2's mcp_servers.yaml)."""
    if os.environ.get("MCP_TRANSPORT", "stdio").lower() in ("http", "streamable-http", "streamable_http"):
        mcp.run(transport="streamable-http", host=os.environ.get("MCP_HOST", "127.0.0.1"),
                port=int(os.environ.get("MCP_PORT", DEFAULT_PORTS[name])))
    else:
        mcp.run()


def ok(data: Any = None, **extra: Any) -> dict:
    out = {"ok": True, "data": data}
    out.update(extra)
    return out


def err(code: str, message: str = "", **extra: Any) -> dict:
    out = {"ok": False, "error": {"code": code, "message": message}}
    out.update(extra)
    return out


# ---------------------------------------------------------------- redaction

_SECRET_PATTERNS = [
    (re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token|authorization)(\s*[=:]\s*)(\"?)[^\s\",;&]+"),
     r"\1\2\3[REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}"), "[REDACTED-KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED-KEY]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[REDACTED-EMAIL]"),
    (re.compile(r"\b(?:\d{4}[ -]){3}\d{4}\b"), "[REDACTED-NUMBER]"),
    (re.compile(r"\b\d{16}\b"), "[REDACTED-NUMBER]"),
]


def redact(text: str) -> str:
    """Remove secrets and obvious PII (cards, emails, keys) from one string."""
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_obj(obj: Any) -> Any:
    """Redact every string inside a nested dict/list."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    return obj
