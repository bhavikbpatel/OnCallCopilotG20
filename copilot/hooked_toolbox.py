"""Run the graph's code-driven tool calls through the hooks (Person 3).

`HookedToolbox` wraps an `McpToolbox` and has the same `call(qualified, args)`, so the Mcp* clients
(`McpTelemetry(toolbox)`, `McpOps(toolbox)`, ...) take it unchanged. Every call goes through `Hooks.acall`:
permission, approval, budget and loop checks before; injection filter, redaction and logging after.

Which role is calling is not in the call, so a node sets it with `@with_role("comms")` (or
`with use_role("comms"):` around the calls). A call made with no role is BLOCKED (`unknown_role`): it never runs
unchecked.

Wiring (copilot/runtime.py), after `toolbox = await McpToolbox.connect(...)`:

    hooks = Hooks(secret=os.environ["APPROVAL_SECRET"])
    toolbox = HookedToolbox(toolbox, hooks)        # then build McpTelemetry(toolbox), McpOps(toolbox)... as before
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
from typing import Any, Callable, Dict, Iterator, Optional

from copilot.clients import ToolError
from copilot.hooks import Hooks

_role: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("hook_role", default=None)
_agent: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("hook_agent", default=None)


@contextlib.contextmanager
def use_role(role: str, agent: Optional[str] = None) -> Iterator[None]:
    """Everything called inside this block counts as `role` (and `agent`, default the role name)."""
    r, a = _role.set(role), _agent.set(agent or role)
    try:
        yield
    finally:
        _role.reset(r)
        _agent.reset(a)


def with_role(role: str, agent: Optional[str] = None) -> Callable:
    """Decorator for an async graph node: its tool calls are made as `role`."""
    def wrap(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def inner(*args: Any, **kwargs: Any) -> Any:
            with use_role(role, agent):
                return await fn(*args, **kwargs)
        return inner
    return wrap


class HookedToolbox:
    def __init__(self, inner: Any, hooks: Hooks):
        self._inner = inner
        self.hooks = hooks

    def __getattr__(self, name: str) -> Any:      # tools, permissions, for_agent, ... stay the inner toolbox's
        return getattr(self._inner, name)

    async def call(self, qualified: str, args: Dict[str, Any]) -> Any:
        server, _, tool = qualified.partition(".")
        role = _role.get() or ""                  # "" is not a role: the hooks block it as unknown_role
        agent = _agent.get() or role or "unassigned"

        async def invoke(a: Dict[str, Any]) -> Dict[str, Any]:
            try:
                return {"ok": True, "result": await self._inner.call(qualified, a)}
            except ToolError as exc:
                return {"ok": False, "error": {"code": exc.code, "message": str(exc).split(": ", 1)[-1]}}

        envelope = await self.hooks.acall(agent, role, server, tool, args, invoke)
        if isinstance(envelope, dict) and envelope.get("ok"):
            return envelope.get("result")
        error = (envelope or {}).get("error") or {}
        reason = (envelope or {}).get("block_reason") or error.get("block_reason")
        code = ("blocked:%s" % reason) if reason else error.get("code", "tool_error")
        raise ToolError(code, error.get("message", "unknown error"))
