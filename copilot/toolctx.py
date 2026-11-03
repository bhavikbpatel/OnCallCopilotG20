"""Who is making a tool call, for the hooks: one Hooks per incident plus the caller's agent, role, driver, purpose.

Context variables, so nothing is threaded through node signatures. LangGraph runs each node in its own
asyncio task, which copies the context: `run_incident` sets `hooks` once, the node wrapper in graph.py sets
`agent`/`role`, and code that polls by itself (verify) runs under `driver="code"`.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Iterator

CODE, MODEL = "code", "model"


@dataclass(frozen=True)
class CallContext:
    hooks: Any = None               # copilot.hooks.Hooks; None → calls go straight to the tool (no hooks log)
    agent: str = "unknown"          # instance name, e.g. "diagnosis-payment"
    role: str = "unknown"           # eval.schemas.catalogue.AGENT_ROLES
    driver: str = MODEL             # "code": the graph made the call by itself (not budgeted)
    purpose: str | None = None      # fix | follow_up | rollback, on dry runs and shop writes


_CTX: ContextVar[CallContext] = ContextVar("copilot_call_context", default=CallContext())


def current() -> CallContext:
    return _CTX.get()


@contextmanager
def scope(**changes: Any) -> Iterator[CallContext]:
    """Override some fields for the calls made inside the block."""
    token = _CTX.set(replace(_CTX.get(), **changes))
    try:
        yield _CTX.get()
    finally:
        _CTX.reset(token)
