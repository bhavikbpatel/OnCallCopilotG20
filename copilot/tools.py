"""MCP tools: load the four servers, filter per agent, and back the client interfaces with real tool calls.

Two ways a step calls a tool:
- model-driven (Diagnosis ReAct): `toolbox.for_agent("diagnosis")` → only the tools permissions.yaml grants.
- code-driven (Triage, Runbook, Remediation, Comms): the Mcp* clients below call tools by qualified name.

Tool results follow the server contract: `{ok: false, error: {code, message}}` on failure → ToolError.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from copilot.clients import Passage, ServiceHealth, ToolError
from copilot.state import Action, Alert, DryRunResult, Postmortem, Severity


def load_server_connections(path: Path) -> dict[str, dict[str, Any]]:
    """config/mcp_servers.yaml; each server's URL can be overridden by its `url_env` variable."""
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))["servers"]
    connections = {}
    for name, server in cfg.items():
        conn = {k: v for k, v in server.items() if k != "url_env"}
        if server.get("url_env") and os.getenv(server["url_env"]):
            conn["url"] = os.environ[server["url_env"]]
        connections[name] = conn
    return connections


def load_permissions(path: Path) -> dict[str, set[str]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {agent: set(tools or []) for agent, tools in data.items()}


def _parse(raw: Any) -> Any:
    """MCP adapter output (str | content blocks | (content, artifact)) → JSON value."""
    if isinstance(raw, tuple):
        raw = raw[0]
    if isinstance(raw, list):
        raw = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in raw)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return raw
    if isinstance(raw, dict) and raw.get("ok") is False:
        err = raw.get("error") or {}
        raise ToolError(err.get("code", "tool_error"), err.get("message", "unknown error"))
    if isinstance(raw, dict) and "result" in raw:
        return raw["result"]
    return raw


class McpToolbox:
    def __init__(self, tools: dict[str, BaseTool], permissions: dict[str, set[str]]):
        self.tools = tools                    # qualified name "server.tool" → tool
        self.permissions = permissions

    @classmethod
    async def connect(cls, servers_path: Path, permissions_path: Path) -> "McpToolbox":
        connections = load_server_connections(servers_path)
        client = MultiServerMCPClient(connections)
        tools: dict[str, BaseTool] = {}
        for server in connections:
            for tool in await client.get_tools(server_name=server):
                tools[f"{server}.{tool.name}"] = tool
        return cls(tools, load_permissions(permissions_path))

    def for_agent(self, agent: str) -> list[BaseTool]:
        allowed = self.permissions.get(agent.split(":")[0], set())
        return [t for name, t in self.tools.items() if name in allowed]

    async def call(self, qualified: str, args: dict[str, Any]) -> Any:
        tool = self.tools.get(qualified)
        if tool is None:
            raise ToolError("tool_not_found", qualified)
        return _parse(await tool.ainvoke(args))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _series(result: Any) -> list[dict[str, Any]]:
    return result.get("series", []) if isinstance(result, dict) else (result or [])


def _last_value(series: dict[str, Any]) -> float | None:
    values = series.get("values") or []
    if not values:
        return None
    last = values[-1]
    return float(last[1] if isinstance(last, (list, tuple)) else last)


class McpTelemetry:
    def __init__(self, toolbox: McpToolbox, promql: dict[str, str]):
        self.toolbox, self.promql = toolbox, promql

    async def _query(self, query: str, lookback: timedelta = timedelta(minutes=5)) -> list[dict[str, Any]]:
        end = datetime.now(timezone.utc)
        result = await self.toolbox.call("telemetry.query_promql", {
            "query": query, "start": _iso(end - lookback), "end": _iso(end), "step": "30s"})
        return _series(result)

    async def query_scalar(self, query: str) -> float | None:
        series = await self._query(query)
        return _last_value(series[0]) if series else None

    async def recent_changes(self, since: datetime) -> list[dict[str, Any]]:
        changes = await self.toolbox.call("telemetry.recent_changes", {"since": _iso(since)})
        out = []
        for c in changes.get("changes", []) if isinstance(changes, dict) else changes or []:
            at = c.get("at")
            out.append({**c, "at": datetime.fromisoformat(at) if isinstance(at, str) else at})
        return out

    async def service_health(self, window: str = "5m") -> list[ServiceHealth]:
        errors = await self._query(self.promql["error_rate_by_service"].replace("$window", window))
        latency = await self._query(self.promql["latency_p95_by_service"].replace("$window", window))
        p95 = {s.get("labels", {}).get("service"): _last_value(s) for s in latency}
        return [ServiceHealth(service=s.get("labels", {}).get("service", "unknown"),
                              error_rate=_last_value(s) or 0.0,
                              latency_p95_ms=p95.get(s.get("labels", {}).get("service")))
                for s in errors]


class McpRunbooks:
    def __init__(self, toolbox: McpToolbox):
        self.toolbox = toolbox

    async def search(self, query: str, service: str | None = None, symptom: str | None = None,
                     k: int = 5) -> list[Passage]:
        args = {"query": query, "k": k} | {key: v for key, v in (("service", service), ("symptom", symptom)) if v}
        result = await self.toolbox.call("runbooks_kb.search", args)
        passages = result.get("passages", []) if isinstance(result, dict) else result or []
        return [Passage.model_validate(p) for p in passages]


class McpOps:
    def __init__(self, toolbox: McpToolbox):
        self.toolbox = toolbox

    async def dry_run(self, action: Action) -> DryRunResult:
        return DryRunResult.model_validate(await self.toolbox.call("ops_actions.dry_run", action.model_dump(mode="json")))

    async def execute(self, action: Action, approval_token: str) -> dict[str, Any]:
        args = action.model_dump(mode="json", exclude={"tool"}) | {"approval_token": approval_token}
        result = await self.toolbox.call(f"ops_actions.{action.tool}", args)
        return result if isinstance(result, dict) else {"result": result}


class McpTickets:
    def __init__(self, toolbox: McpToolbox):
        self.toolbox = toolbox

    async def create_incident(self, incident_id: str, alert: Alert, severity: Severity) -> None:
        await self.toolbox.call("tickets.create_incident", {
            "incident_id": incident_id, "alert": alert.model_dump(mode="json"), "severity": str(severity)})

    async def append_timeline(self, incident_id: str, at: datetime, text: str) -> None:
        await self.toolbox.call("tickets.append_timeline", {"incident_id": incident_id, "at": _iso(at), "text": text})

    async def save_postmortem(self, incident_id: str, postmortem: Postmortem) -> None:
        await self.toolbox.call("tickets.save_postmortem", {
            "incident_id": incident_id, "postmortem": postmortem.model_dump(mode="json")})
