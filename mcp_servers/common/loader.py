"""What the MCP loader needs that has no model or network in it: extra servers and the pin check.

Extra servers exist so the red team can plant a server we have not seen (system design section 10,
"third-party server test") without sharing its content: a YAML file named by MCP_EXTRA_SERVERS,

    servers:
      weather_helper:
        transport: streamable_http
        url: http://localhost:8199/mcp
        agents: [diagnosis]          # optional: agents whose tool list gets this server's tools

An extra server is never pinned, so with the pinning defence on (the default) every one of its tools is
refused (`server_not_pinned`); with it off (E2 "defences off") they load like any other tool. Our four
servers are pinned in config/mcp_allowlist.yaml.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

from mcp_servers.common.pinning import OUR_SERVERS, check_server

EXTRA_ENV = "MCP_EXTRA_SERVERS"
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_KEYS = frozenset({"transport", "url", "agents"})


def extra_servers_path(explicit: Optional[str] = None) -> Optional[str]:
    """The extra-servers file: the argument, else MCP_EXTRA_SERVERS, else none (the normal case)."""
    return explicit or os.environ.get(EXTRA_ENV) or None


def load_extra_servers(path: str, reserved: Iterable[str] = OUR_SERVERS) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[str]]]:
    """Read the file. Returns (connections for MultiServerMCPClient, {server: [agents given its tools]}).

    A file that was asked for but is wrong stops the run: silently dropping a planted server would change
    what an experiment measures. Every problem is a ValueError that names the file and the server."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("%s: cannot read the extra MCP servers file: %s" % (path, exc)) from exc
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or not servers:
        raise ValueError("%s: expected a non-empty `servers:` mapping" % path)
    connections: Dict[str, Dict[str, Any]] = {}
    grants: Dict[str, List[str]] = {}
    for name, spec in servers.items():
        where = "%s: server %r" % (path, name)
        if not isinstance(name, str) or not _NAME.match(name):
            raise ValueError("%s: the name must be lower-case letters, digits and _ (max 32)" % where)
        if name in set(reserved):
            raise ValueError("%s: that name belongs to one of our own servers" % where)
        if not isinstance(spec, dict) or set(spec) - _KEYS:
            raise ValueError("%s: allowed keys are %s" % (where, ", ".join(sorted(_KEYS))))
        if not isinstance(spec.get("transport"), str) or not isinstance(spec.get("url"), str):
            raise ValueError("%s: `transport` and `url` are required text" % where)
        agents = spec.get("agents", [])
        if not isinstance(agents, list) or not all(isinstance(a, str) and a for a in agents):
            raise ValueError("%s: `agents` must be a list of agent names" % where)
        connections[name] = {"transport": spec["transport"], "url": spec["url"]}
        grants[name] = list(agents)
    return connections, grants


def pin_problems(server: str, tools: Iterable[Any], allowlist: Dict[str, Any]) -> Tuple[Dict[str, str], List[str]]:
    """({tool name: reason it is refused}, [pinned tools the server no longer has]) for one server.

    `tools` are whatever the adapter returns: anything with `.name` and `.description`."""
    result = check_server(server, [{"name": t.name, "description": getattr(t, "description", "") or ""} for t in tools],
                          allowlist)
    return {r["tool"]: r["reason"] for r in result["refused"]}, list(result["missing"])
