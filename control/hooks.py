"""Pre-tool and post-tool hooks: every tool call goes through Hooks.call().

Pre-tool, in order: role known -> permission -> approval (gated writes) -> budget -> loop.
A blocked call is logged (never dropped) and returns {"ok": false, "error": {"code": "blocked"}}
to the agent. Post-tool: redact secrets, neutralise instruction-like text, log.

The log follows eval.schemas.result.ToolCall, so the evaluation can score it as it is.
Only the hash of an approval token is ever relevant; the token itself is never logged.
"""
from __future__ import annotations

import fnmatch
import json
import os
from typing import Any, Callable, Dict, List, Optional

import yaml
from eval.schemas import payload_hash
from eval.schemas.catalogue import AGENT_ROLES, EXTERNAL_WRITE_TOOLS, SHOP_WRITE_TOOLS

from control.guardrails import GuardConfig, sanitize_obj
from tools.approval import check_token
from tools.common import TOOL_KINDS, WRITE, err

DEFAULT_PERMISSIONS = os.path.join(os.path.dirname(__file__), "permissions.yaml")


def load_permissions(path: Optional[str] = None) -> dict:
    with open(path or DEFAULT_PERMISSIONS) as fh:
        return yaml.safe_load(fh)


def is_gated(server: str, tool: str) -> bool:
    """Writes that need an approval token: shop actions and external status updates."""
    return (server == "ops-actions" and tool in SHOP_WRITE_TOOLS) or (server == "tickets" and tool in EXTERNAL_WRITE_TOOLS)


def _split_flat(args: Dict[str, Any]):
    tool = args.get("tool")
    return tool, {k: v for k, v in args.items() if k != "tool"}


class Hooks:
    def __init__(self, permissions: Optional[dict] = None, max_tool_calls: Optional[int] = None,
                 per_agent_max: Optional[int] = None, config: Optional[GuardConfig] = None,
                 secret: Optional[str] = None, clock: Optional[Callable[[], float]] = None):
        self.permissions = permissions if permissions is not None else load_permissions()
        default = (self.permissions.get("default_budget") or {}).get("max_tool_calls", 10)
        self.max_tool_calls = max_tool_calls if max_tool_calls is not None else default
        self.per_agent_max = per_agent_max
        self.config = config or GuardConfig.load()
        self._secret = secret
        self._clock = clock
        self.log: List[Dict[str, Any]] = []

    # ------------------------------------------------------------- pre-tool

    def allowed(self, role: str, key: str) -> bool:
        patterns = ((self.permissions.get("roles") or {}).get(role) or {}).get("allow", [])
        return any(fnmatch.fnmatchcase(key, pattern) for pattern in patterns)

    @staticmethod
    def _signature(key: str, args: Dict[str, Any]) -> str:
        clean = {k: v for k, v in args.items() if k != "approval_token"}
        return key + json.dumps(clean, sort_keys=True, default=str)

    def _hash_for(self, server: str, tool: str, args: Dict[str, Any]) -> Optional[str]:
        if server == "ops-actions" and tool == "dry_run":
            inner, rest = _split_flat(args)
            return payload_hash(str(inner), rest) if inner else None
        if is_gated(server, tool):
            return payload_hash(tool, args)
        return None

    def _pre(self, agent: str, role: str, server: str, tool: str, args: Dict[str, Any]) -> Optional[str]:
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
        executed = [e for e in self.log if not e["blocked"]]
        if len(executed) >= self.max_tool_calls:
            return "budget_exceeded"
        if self.per_agent_max is not None and len([e for e in executed if e["agent"] == agent]) >= self.per_agent_max:
            return "agent_budget_exceeded"
        mine = [e for e in executed if e["agent"] == agent][-2:]
        sig = self._signature(key, args)
        if len(mine) == 2 and all(self._signature("%s.%s" % (e["server"], e["tool"]), e["args"]) == sig for e in mine):
            return "loop_detected"
        return None

    # ------------------------------------------------------------- the call

    def call(self, agent: str, role: str, server: str, tool: str, args: Optional[Dict[str, Any]],
             invoke: Callable[[Dict[str, Any]], Dict[str, Any]], purpose: Optional[str] = None) -> Dict[str, Any]:
        """Run one tool call through the hooks. `invoke(args)` performs the real call."""
        args = dict(args or {})
        reason = self._pre(agent, role, server, tool, args)
        if reason:
            self._append(agent, role, server, tool, args, ok=False, blocked=True, reason=reason,
                         result=None, purpose=purpose, flags=[])
            return err("blocked", "call blocked by hook: %s" % reason, block_reason=reason)
        try:
            result = invoke(args)
        except Exception as exc:  # a tool must never crash the run
            result = err("tool_exception", "%s: %s" % (type(exc).__name__, exc))
        flags: List[str] = []
        if isinstance(result, dict):
            result, flags = sanitize_obj(result, self.config)
        self._append(agent, role, server, tool, args, ok=bool(isinstance(result, dict) and result.get("ok")),
                     blocked=False, reason=None, result=result, purpose=purpose, flags=flags)
        if flags and isinstance(result, dict):
            result = dict(result, injection_flags=sorted(set(flags)))
        return result

    def _append(self, agent: str, role: str, server: str, tool: str, args: Dict[str, Any], ok: bool,
                blocked: bool, reason: Optional[str], result: Any, purpose: Optional[str], flags: List[str]) -> None:
        logged_args = {k: v for k, v in args.items() if k != "approval_token"}
        entry = {
            "seq": len(self.log), "agent": agent, "agent_role": role, "server": server, "tool": tool,
            "kind": TOOL_KINDS.get("%s.%s" % (server, tool), WRITE),   # unknown tools count as writes
            "args": logged_args, "payload_hash": self._hash_for(server, tool, args),
            "ok": ok, "blocked": blocked, "block_reason": reason,
            "action_id": result.get("action_id") if isinstance(result, dict) else None,
            "purpose": purpose,
        }
        if flags:
            entry["injection_flags"] = sorted(set(flags))
        self.log.append(entry)

    # ------------------------------------------------------------- export

    def tool_calls(self) -> List[Dict[str, Any]]:
        """The log as the result line's `tool_calls`."""
        return [dict(e) for e in self.log]

    def budget(self) -> Dict[str, int]:
        return {"max_tool_calls": self.max_tool_calls}

    def write_jsonl(self, path: str) -> None:
        with open(path, "w") as fh:
            for entry in self.log:
                fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")

    def reset(self) -> None:
        self.log = []
