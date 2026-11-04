"""Pre-tool and post-tool hooks: every tool call goes through Hooks.call().

Pre-tool, in order: role known -> permission -> approval (gated writes) -> budget -> loop.
A blocked call is logged (never dropped) and returns {"ok": false, "error": {"code": "blocked"}}
to the agent. Post-tool: redact secrets, neutralise instruction-like text, log.

The log follows eval.schemas.result.ToolCall, so the evaluation can score it as it is.
Only the hash of an approval token is ever relevant; the token itself is never logged.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import yaml
from eval.schemas import payload_hash
from eval.schemas.catalogue import AGENT_ROLES, EXTERNAL_WRITE_TOOLS, SHOP_WRITE_TOOLS

from copilot.guardrails import GuardConfig, sanitize_obj
from mcp_servers.common.approval import check_token
from mcp_servers.common import TOOL_KINDS, WRITE, err

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_PERMISSIONS = os.path.join(ROOT, "config", "permissions.yaml")
DEFAULT_APP_CONFIG = os.path.join(ROOT, "config", "app.yaml")
FALLBACK_MAX_TOOL_CALLS = 30


def _limits(path: Optional[str] = None) -> dict:
    try:
        with open(path or os.environ.get("COPILOT_CONFIG") or DEFAULT_APP_CONFIG) as fh:
            return (yaml.safe_load(fh) or {}).get("limits") or {}
    except (OSError, yaml.YAMLError, AttributeError):
        return {}


def load_budget(path: Optional[str] = None) -> int:
    """The per-incident tool-call budget lives in one place: limits.tool_calls in config/app.yaml
    (E4 overrides it there). 30 only if that file or key is missing."""
    try:
        value = _limits(path).get("tool_calls")
        return int(value) if value else FALLBACK_MAX_TOOL_CALLS
    except (ValueError, TypeError):
        return FALLBACK_MAX_TOOL_CALLS


def load_cost_limits(path: Optional[str] = None) -> tuple:
    """(max_tokens, max_usd) from limits.tokens / limits.usd in config/app.yaml; None = not capped."""
    limits = _limits(path)
    try:
        tokens = int(limits["tokens"]) if limits.get("tokens") else None
        usd = float(limits["usd"]) if limits.get("usd") else None
    except (ValueError, TypeError):
        return None, None
    return tokens, usd


def normalize_permissions(data: Optional[dict]) -> dict:
    """Both file shapes become {"roles": {role: {"allow": [...]}}}.

    config/permissions.yaml is Person 2's flat shape (`agent: [server.tool, ...]`, read by
    McpToolbox.for_agent). A `_code_driven:` section adds tools a role calls from code. The older nested
    shape (`roles: {role: {allow: [...]}}`) is still accepted."""
    data = dict(data or {})
    if isinstance(data.get("roles"), dict):
        return data
    code = data.pop("_code_driven", None) or {}
    roles: Dict[str, Dict[str, List[str]]] = {}
    for role, tools in list(data.items()) + [(r, t) for r, t in code.items()]:
        roles.setdefault(role, {"allow": []})["allow"].extend(list(tools or []))
    return {"roles": roles}


def load_permissions(path: Optional[str] = None) -> dict:
    with open(path or DEFAULT_PERMISSIONS) as fh:
        return normalize_permissions(yaml.safe_load(fh))


def is_gated(server: str, tool: str) -> bool:
    """Writes that need an approval token: shop actions and external status updates."""
    return (server == "ops_actions" and tool in SHOP_WRITE_TOOLS) or (server == "tickets" and tool in EXTERNAL_WRITE_TOOLS)


def _split_flat(args: Dict[str, Any]):
    tool = args.get("tool")
    return tool, {k: v for k, v in args.items() if k != "tool"}


class Hooks:
    def __init__(self, permissions: Optional[dict] = None, max_tool_calls: Optional[int] = None,
                 per_agent_max: Optional[int] = None, config: Optional[GuardConfig] = None,
                 secret: Optional[str] = None, clock: Optional[Callable[[], float]] = None,
                 max_tokens: Optional[int] = None, max_usd: Optional[float] = None,
                 sink: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.permissions = normalize_permissions(permissions) if permissions is not None else load_permissions()
        self.max_tool_calls = max_tool_calls if max_tool_calls is not None else load_budget()
        self.per_agent_max = per_agent_max
        self.config = config or GuardConfig.load()
        self._secret = secret
        self._clock = clock
        cfg_tokens, cfg_usd = load_cost_limits()
        self.max_tokens = max_tokens if max_tokens is not None else cfg_tokens
        self.max_usd = max_usd if max_usd is not None else cfg_usd
        self.tokens_used = 0
        self.usd_used = 0.0
        self.sink = sink                 # called with every log entry: Langfuse span, incident timeline, JSONL...
        self.sink_errors = 0             # log entries the sink failed to take (a broken sink never stops the incident)
        self.log: List[Dict[str, Any]] = []

    def add_cost(self, tokens: int = 0, usd: float = 0.0) -> None:
        """The graph reports each LLM call's cost here so tokens and dollars share the tool-call budget."""
        self.tokens_used += int(tokens)
        self.usd_used += float(usd)

    # ------------------------------------------------------------- pre-tool

    def allowed(self, role: str, key: str) -> bool:
        patterns = ((self.permissions.get("roles") or {}).get(role) or {}).get("allow", [])
        return any(fnmatch.fnmatchcase(key, pattern) for pattern in patterns)

    @staticmethod
    def _signature(key: str, args: Dict[str, Any]) -> str:
        clean = {k: v for k, v in args.items() if k != "approval_token"}
        return key + json.dumps(clean, sort_keys=True, default=str)

    def _hash_for(self, server: str, tool: str, args: Dict[str, Any]) -> Optional[str]:
        if server == "ops_actions" and tool == "dry_run":
            inner, rest = _split_flat(args)
            return payload_hash(str(inner), rest) if inner else None
        if is_gated(server, tool):
            return payload_hash(tool, args)
        return None

    def _pre(self, agent: str, role: str, server: str, tool: str, args: Dict[str, Any],
             driver: Optional[str] = None) -> Optional[str]:
        key = "%s.%s" % (server, tool)
        if role not in AGENT_ROLES:
            return "unknown_role"
        if self.config.permissions and not self.allowed(role, key):
            return "permission_denied"
        if is_gated(server, tool):
            code = check_token(payload_hash(tool, args), args.get("approval_token"), self._secret,
                               self._clock() if self._clock else None)
            if code:
                return code
        if driver == "code":     # the graph's own calls (verify polling): neither budgeted nor loop-checked
            return None
        executed = [e for e in self.log if not e["blocked"] and e.get("driver") != "code"]
        if len(executed) >= self.max_tool_calls:
            return "budget_exceeded"
        if self.max_tokens is not None and self.tokens_used >= self.max_tokens:
            return "token_budget_exceeded"
        if self.max_usd is not None and self.usd_used >= self.max_usd:
            return "cost_budget_exceeded"
        if self.per_agent_max is not None and len([e for e in executed if e["agent"] == agent]) >= self.per_agent_max:
            return "agent_budget_exceeded"
        mine = [e for e in executed if e["agent"] == agent][-2:]
        sig = self._signature(key, args)
        if len(mine) == 2 and all(self._signature("%s.%s" % (e["server"], e["tool"]), e["args"]) == sig for e in mine):
            return "loop_detected"
        return None

    # ------------------------------------------------------------- the call

    def _begin(self, agent: str, role: str, server: str, tool: str, args: Dict[str, Any],
               purpose: Optional[str]) -> Optional[Dict[str, Any]]:
        """Pre-tool hooks. Returns the 'blocked' envelope (already logged) or None to go ahead."""
        reason = self._pre(agent, role, server, tool, args)
        if not reason:
            return None
        self._append(agent, role, server, tool, args, ok=False, blocked=True, reason=reason,
                     result=None, purpose=purpose, flags=[])
        return err("blocked", "call blocked by hook: %s" % reason, block_reason=reason)

    def _finish(self, agent: str, role: str, server: str, tool: str, args: Dict[str, Any], result: Any,
                started: float, purpose: Optional[str]) -> Any:
        """Post-tool hooks: sanitize, redact, log."""
        self._latency_ms = int((time.monotonic() - started) * 1000)
    def call(self, agent: str, role: str, server: str, tool: str, args: Optional[Dict[str, Any]],
             invoke: Callable[[Dict[str, Any]], Dict[str, Any]], purpose: Optional[str] = None,
             driver: Optional[str] = None) -> Dict[str, Any]:
        """Run one tool call through the hooks. `invoke(args)` performs the real call."""
        args, blocked = self._before(agent, role, server, tool, args, purpose, driver)
        if blocked is not None:
            return blocked
        started = time.monotonic()
        try:
            result = invoke(args)
        except Exception as exc:  # a tool must never crash the run
            result = err("tool_exception", "%s: %s" % (type(exc).__name__, exc))
        return self._after(agent, role, server, tool, args, result, started, purpose, driver)

    async def acall(self, agent: str, role: str, server: str, tool: str, args: Optional[Dict[str, Any]],
                    invoke: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]], purpose: Optional[str] = None,
                    driver: Optional[str] = None) -> Dict[str, Any]:
        """call() for an async tool (the graph's MCP clients): `await invoke(args)` performs the real call.
        `driver` is "model" (the agent chose the call) or "code" (the graph made it, e.g. verify polling)."""
        args, blocked = self._before(agent, role, server, tool, args, purpose, driver)
        if blocked is not None:
            return blocked
        started = time.monotonic()
        try:
            result = await invoke(args)
        except Exception as exc:  # a tool must never crash the run
            result = err("tool_exception", "%s: %s" % (type(exc).__name__, exc))
        return self._after(agent, role, server, tool, args, result, started, purpose, driver)

    def _before(self, agent, role, server, tool, args, purpose, driver):
        args = dict(args or {})
        reason = self._pre(agent, role, server, tool, args, driver)
        if not reason:
            return args, None
        self._append(agent, role, server, tool, args, ok=False, blocked=True, reason=reason,
                     result=None, purpose=purpose, flags=[], driver=driver)
        return args, err("blocked", "call blocked by hook: %s" % reason, block_reason=reason)

    def _after(self, agent, role, server, tool, args, result, started, purpose, driver):
        latency_ms = int((time.monotonic() - started) * 1000)
        flags: List[str] = []
        if isinstance(result, dict):
            result, flags = sanitize_obj(result, self.config)
        size = len(json.dumps(result, default=str)) if result is not None else 0
        self._append(agent, role, server, tool, args, ok=bool(isinstance(result, dict) and result.get("ok")),
                     blocked=False, reason=None, result=result, purpose=purpose, flags=flags, driver=driver,
                     latency_ms=latency_ms, size=size)
        if flags and isinstance(result, dict):
            result = dict(result, injection_flags=sorted(set(flags)))
        return result

    def call(self, agent: str, role: str, server: str, tool: str, args: Optional[Dict[str, Any]],
             invoke: Callable[[Dict[str, Any]], Dict[str, Any]], purpose: Optional[str] = None) -> Dict[str, Any]:
        """Run one tool call through the hooks. `invoke(args)` performs the real call."""
        args = dict(args or {})
        blocked = self._begin(agent, role, server, tool, args, purpose)
        if blocked:
            return blocked
        started = time.monotonic()
        try:
            result = invoke(args)
        except Exception as exc:  # a tool must never crash the run
            result = err("tool_exception", "%s: %s" % (type(exc).__name__, exc))
        return self._finish(agent, role, server, tool, args, result, started, purpose)

    async def acall(self, agent: str, role: str, server: str, tool: str, args: Optional[Dict[str, Any]],
                    invoke: Callable[[Dict[str, Any]], Any], purpose: Optional[str] = None) -> Dict[str, Any]:
        """Same as call(), for an async `invoke` (the graph's tool calls are async). The hooks themselves do no I/O."""
        args = dict(args or {})
        blocked = self._begin(agent, role, server, tool, args, purpose)
        if blocked:
            return blocked
        started = time.monotonic()
        try:
            result = await invoke(args)
        except Exception as exc:
            result = err("tool_exception", "%s: %s" % (type(exc).__name__, exc))
        return self._finish(agent, role, server, tool, args, result, started, purpose)

    def _append(self, agent: str, role: str, server: str, tool: str, args: Dict[str, Any], ok: bool,
                blocked: bool, reason: Optional[str], result: Any, purpose: Optional[str], flags: List[str],
                driver: Optional[str] = None, latency_ms: int = 0, size: int = 0) -> None:
        logged_args = {k: v for k, v in args.items() if k != "approval_token"}
        entry = {
            "seq": len(self.log), "agent": agent, "agent_role": role, "server": server, "tool": tool,
            "kind": TOOL_KINDS.get("%s.%s" % (server, tool), WRITE),   # unknown tools count as writes
            "args": logged_args, "payload_hash": self._hash_for(server, tool, args),
            "ok": ok, "blocked": blocked, "block_reason": reason,
            "action_id": result.get("action_id") if isinstance(result, dict) else None,
            "purpose": purpose, "driver": driver,
        }
        entry["args_hash"] = hashlib.sha256(json.dumps(logged_args, sort_keys=True, default=str).encode()).hexdigest()[:16]
        entry["latency_ms"] = 0 if blocked else latency_ms
        entry["result_size"] = 0 if blocked else size
        if flags:
            entry["injection_flags"] = sorted(set(flags))
        self.log.append(entry)
        if self.sink is not None:
            try:
                self.sink(dict(entry))
            except Exception:       # a broken log sink must never stop the incident, but it is counted
                self.sink_errors += 1

    # ------------------------------------------------------------- export

    def tool_calls(self) -> List[Dict[str, Any]]:
        """The log as the result line's `tool_calls`."""
        return [dict(e) for e in self.log]

    def budget(self) -> Dict[str, int]:
        return {"max_tool_calls": self.max_tool_calls}

    def costs(self) -> Dict[str, Any]:
        """Token and dollar totals against their caps (None = not capped)."""
        return {"max_tokens": self.max_tokens, "max_usd": self.max_usd,
                "tokens_used": self.tokens_used, "usd_used": round(self.usd_used, 6),
                "sink_errors": self.sink_errors}

    def write_jsonl(self, path: str) -> None:
        with open(path, "w") as fh:
            for entry in self.log:
                fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")

    def reset(self) -> None:
        self.log = []


def jsonl_sink(path: str) -> Callable[[Dict[str, Any]], None]:
    """Append every log entry to a file as it happens (so the log survives a crash and Grafana/Loki can tail it)."""
    def write(entry: Dict[str, Any]) -> None:
        with open(path, "a") as fh:
            fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
    return write


def timeline_sink(append_timeline: Callable[..., Any], incident_id: str) -> Callable[[Dict[str, Any]], None]:
    """Put each tool call on the incident timeline (system design section 9). `append_timeline` is the
    tickets tool: (incident_id, event, source)."""
    def write(entry: Dict[str, Any]) -> None:
        state = "BLOCKED (%s)" % entry["block_reason"] if entry["blocked"] else ("ok" if entry["ok"] else "error")
        append_timeline(incident_id, "%s called %s.%s: %s" % (entry["agent"], entry["server"], entry["tool"], state),
                        "hooks")
    return write
