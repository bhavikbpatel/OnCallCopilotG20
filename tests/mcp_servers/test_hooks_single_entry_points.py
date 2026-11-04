"""Hooks has exactly one call() and one acall(), both taking `driver` and logging the call.

Merging #43 into #45/#46 left a second copy of call/acall (no `driver`, a `_finish` that returned nothing) after
the working ones, so it silently replaced them: every hooked call from run_incident raised TypeError."""
from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

from copilot.hooks import Hooks
from mcp_servers.common import ok

SRC = Path(inspect.getsourcefile(Hooks)).read_text()


def _method_names() -> list[str]:
    cls = next(n for n in ast.parse(SRC).body if isinstance(n, ast.ClassDef) and n.name == "Hooks")
    return [n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def test_each_method_is_defined_once():
    names = _method_names()
    assert sorted({n for n in names if names.count(n) > 1}) == []


def test_call_and_acall_take_driver_and_log_the_call():
    hooks = Hooks(max_tool_calls=5)
    r = hooks.call("triage", "triage", "telemetry", "query_promql", {"query": "up"}, lambda a: ok({"v": 1}),
                   driver="code")
    assert r["ok"] and r["result"] == {"v": 1}

    async def invoke(a):
        return ok({"v": 2})

    r = asyncio.run(hooks.acall("triage", "triage", "telemetry", "query_promql", {"query": "up2"}, invoke,
                                driver="model"))
    assert r["ok"] and r["result"] == {"v": 2}
    assert [e["driver"] for e in hooks.tool_calls()] == ["code", "model"]
