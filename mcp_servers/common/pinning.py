"""Pin MCP servers and their tool descriptions (threat T3, system design sections 6 and 10).

A poisoned tool description is an instruction channel the model reads as trusted. The loader
checks every tool a server advertises against config/mcp_allowlist.yaml:

    servers:
      telemetry:
        tools: {query_promql: <sha256 of the normalised description>, ...}

* a server that is not listed             -> every tool refused  (server_not_pinned)
* a tool that is not listed               -> refused             (tool_not_pinned)
* a description whose hash changed        -> refused             (description_changed)
* a pinned tool the server no longer has  -> reported            (pinned_tool_missing)

Use check_server() with the tool list that langchain-mcp-adapters returns, before binding tools to an
agent; an unknown or changed description is refused at startup. `python -m mcp_servers.common.pinning --write`
regenerates the file for our four servers after a reviewed change.
"""
from __future__ import annotations

import hashlib
import inspect
import os
import re
import sys
import types
from typing import Any, Dict, Iterable, List, Optional

import yaml

DEFAULT_ALLOWLIST = os.path.join(os.path.dirname(__file__), "..", "..", "config", "mcp_allowlist.yaml")
OUR_SERVERS = ("telemetry", "runbooks_kb", "tickets", "ops_actions")


def hash_description(description: Optional[str]) -> str:
    """sha256 of the description with whitespace collapsed, so re-wrapping text does not matter."""
    return hashlib.sha256(re.sub(r"\s+", " ", description or "").strip().encode()).hexdigest()


def load_allowlist(path: Optional[str] = None) -> Dict[str, Any]:
    with open(path or os.environ.get("MCP_ALLOWLIST") or DEFAULT_ALLOWLIST) as fh:
        return yaml.safe_load(fh) or {}


def check_server(server: str, tools: Iterable[Dict[str, Any]], allowlist: Dict[str, Any]) -> Dict[str, Any]:
    """tools: [{"name", "description"}]. Returns {server_ok, accepted, refused:[{tool, reason}], missing}."""
    pinned = (((allowlist.get("servers") or {}).get(server) or {}).get("tools")) or {}
    listed = (allowlist.get("servers") or {}).get(server) is not None
    accepted: List[str] = []
    refused: List[Dict[str, str]] = []
    seen = set()
    for tool in tools:
        name = str(tool.get("name"))
        seen.add(name)
        if not listed:
            refused.append({"tool": name, "reason": "server_not_pinned"})
        elif name not in pinned:
            refused.append({"tool": name, "reason": "tool_not_pinned"})
        elif pinned[name] != hash_description(tool.get("description")):
            refused.append({"tool": name, "reason": "description_changed"})
        else:
            accepted.append(name)
    missing = sorted(set(pinned) - seen) if listed else []
    return {"server_ok": listed and not refused and not missing, "accepted": accepted,
            "refused": refused, "missing": missing}


def _record_tools(server: str) -> List[Dict[str, str]]:
    """Build one of our servers with a recording stand-in for FastMCP and list its tools.
    The description is the tool function's docstring, which is what FastMCP advertises."""
    recorded: List[Dict[str, str]] = []

    class _Recorder:
        def __init__(self, *a: Any, **k: Any):
            pass

        def tool(self, *a: Any, **k: Any):
            def deco(fn):
                target = inspect.unwrap(fn)
                recorded.append({"name": getattr(target, "__name__", ""), "description": inspect.getdoc(target) or ""})
                return fn
            return deco

        def run(self, *a: Any, **k: Any) -> None:
            pass

    fake = types.ModuleType("fastmcp")
    fake.FastMCP = _Recorder  # type: ignore[attr-defined]
    saved = sys.modules.get("fastmcp")
    sys.modules["fastmcp"] = fake
    env_before = {k: os.environ.get(k) for k in ("INCIDENT_DB", "APPROVAL_STATE_DB")}
    os.environ["INCIDENT_DB"] = os.environ["APPROVAL_STATE_DB"] = ":memory:"      # leave no files behind
    try:
        import importlib
        importlib.import_module("mcp_servers.%s.server" % server).build_server()
    finally:
        for key, value in env_before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if saved is not None:
            sys.modules["fastmcp"] = saved
        else:
            sys.modules.pop("fastmcp", None)
    return recorded


def build_allowlist(servers: Iterable[str] = OUR_SERVERS) -> Dict[str, Any]:
    return {"servers": {s: {"tools": {t["name"]: hash_description(t["description"]) for t in _record_tools(s)}}
                        for s in servers}}


def main(argv: List[str]) -> int:
    data = build_allowlist()
    if "--write" in argv:
        path = os.path.abspath(DEFAULT_ALLOWLIST)
        with open(path, "w") as fh:
            fh.write("# Pinned MCP servers and tool-description hashes (threat T3). Regenerate with:\n"
                     "#   python -m mcp_servers.common.pinning --write\n"
                     "# after a REVIEWED change to a tool's docstring. A server or tool not listed here is refused.\n")
            yaml.safe_dump(data, fh, sort_keys=True)
        print("wrote", path)
    else:
        print(yaml.safe_dump(data, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
