"""Try the four running MCP servers the way the graph does: McpToolbox over streamable HTTP.

Start the servers first (docs/run-mcp-servers-locally.md), then, from the repo root:

    py scripts/try_servers.py

Each check prints OK or FAIL with the reason. A server that is not running is reported and skipped.
telemetry and the flag-based dry run need the live shop; runbooks_kb and tickets do not.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: E402

from copilot.tools import McpToolbox, load_permissions, load_server_connections  # noqa: E402

CHECKS = [
    ("runbooks_kb", "search", {"query": "payment errors checkout failing", "service": "payment", "k": 2}),
    ("tickets", "create_incident", {"incident_id": "TRY-1", "alert": {"alert_name": "TryAlert", "service": "checkout"},
                                    "severity": "SEV-3"}),
    ("tickets", "append_timeline", {"incident_id": "TRY-1", "text": "tried from scripts/try_servers.py"}),
    ("tickets", "get_incident", {"incident_id": "TRY-1"}),
    ("ops_actions", "dry_run", {"tool": "restart_service", "service": "cart"}),
    ("telemetry", "recent_changes", {"since": "15m"}),
    ("telemetry", "search_logs", {"service": "payment", "limit": 3}),
    ("telemetry", "get_traces", {"service": "payment", "limit": 2}),
]


def reachable(url: str) -> bool:
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=2):
            return True
    except OSError:
        return False


def short(value, limit: int = 160) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


async def main() -> int:
    connections = load_server_connections(ROOT / "config" / "mcp_servers.yaml")
    up = {name: conn for name, conn in connections.items() if reachable(conn["url"])}
    for name in connections:
        print("%-12s %s" % (name, "up" if name in up else "NOT RUNNING (%s)" % connections[name]["url"]))
    if not up:
        print("\nNo server is running. Start them first: docs/run-mcp-servers-locally.md")
        return 1
    client = MultiServerMCPClient(up)
    tools = {}
    for server in up:
        for tool in await client.get_tools(server_name=server):
            tools["%s.%s" % (server, tool.name)] = tool
    toolbox = McpToolbox(tools, load_permissions(ROOT / "config" / "permissions.yaml"))
    print("\n%d tools found: %s\n" % (len(tools), ", ".join(sorted(tools))))
    failures = 0
    for server, tool, args in CHECKS:
        label = "%s.%s" % (server, tool)
        if server not in up:
            print("SKIP %-28s server not running" % label)
            continue
        try:
            print("OK   %-28s %s" % (label, short(await toolbox.call(label, args))))
        except Exception as exc:                      # noqa: BLE001 - report every failure, keep going
            failures += 1
            print("FAIL %-28s %s" % (label, str(exc)[:200]))
    print("\n%d check(s) failed." % failures if failures else "\nAll checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
