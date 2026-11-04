"""One incident, end to end, for the evaluation runner (Person 4): `run_incident` and the runner's `CopilotAgent`.

    result = await run_incident(alert, approve, run_id=run_id, limits=BudgetLimits(tool_calls=20),
                                options={"architecture": "C2", "memory": "off", "defences": "on"})
    result.final_state, result.tool_calls (the hooks log), result.fixed_at, result.budget_exhausted

Every tool call, code-driven or model-driven, goes through one `Hooks` per incident (copilot.toolctx), so the
permission, approval, budget and loop checks run and the log is complete. Arm options:
- architecture: C0 | C1 | C2 (copilot/graph.py)
- memory: off | on | playbook (off → no long-term memory; the playbook needs POSTGRES_DSN to persist)
- defences: on | off (copilot.guardrails.GuardConfig: injection filter, redaction, permissions in the hooks)
- review_enabled: bool (False in experiments: nothing is written to memory)
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from dotenv import load_dotenv
from langgraph.types import Command

from copilot import toolctx
from copilot.config import ROOT, AppSettings, load_settings
from copilot.deps import CopilotDeps
from copilot.graph import ARCHITECTURES, build_graph
from copilot.guardrails import GuardConfig
from copilot.hooks import Hooks, load_permissions
from copilot.memory import make_checkpointer, make_store
from copilot.policy import budget_exhausted
from copilot.state import Alert, BudgetLimits
from copilot.tools import McpOps, McpRunbooks, McpTelemetry, McpTickets, McpToolbox

ARM_OPTIONS = frozenset({"architecture", "memory", "defences", "review_enabled"})
BUDGET_BLOCKS = {"budget_exceeded", "token_budget_exceeded", "cost_budget_exceeded", "agent_budget_exceeded"}

Approve = Callable[[dict[str, Any]], "dict[str, Any] | Awaitable[dict[str, Any]]"]


@dataclass
class IncidentResult:
    final_state: dict[str, Any]
    tool_calls: list[dict[str, Any]]            # the hooks log (eval.schemas.result.ToolCall), blocked calls included
    budget: dict[str, Any] = field(default_factory=dict)
    fixed_at: float | None = None               # unix time of the last executed fix (recovery is judged from here)
    budget_exhausted: bool = False


def _switch(value: Any, name: str, on: tuple[str, ...]) -> bool:
    text = str(value).strip().lower()
    if text in ("off", "false", "0", "no"):
        return False
    if text in ("on", "true", "1", "yes", *on):
        return True
    raise ValueError(f"unknown {name} option {value!r}")


def apply_options(settings: AppSettings, limits: BudgetLimits, options: Mapping[str, Any]) -> AppSettings:
    unknown = sorted(set(options) - ARM_OPTIONS)
    if unknown:
        raise ValueError(f"unsupported arm options {unknown}; supported: {sorted(ARM_OPTIONS)}")
    update: dict[str, Any] = {"limits": limits}
    if "architecture" in options:
        if options["architecture"] not in ARCHITECTURES:
            raise ValueError(f"unknown architecture {options['architecture']!r}")
        update["architecture"] = options["architecture"]
    if "review_enabled" in options:
        update["review_enabled"] = _switch(options["review_enabled"], "review_enabled", ())
    if memory_mode(options.get("memory")) == "playbook":
        update["memory_read_only"] = True        # E3/E4: the frozen playbook is read, never written to
    return settings.model_copy(update=update)


def guard_config(defences: Any) -> GuardConfig:
    if defences is None:
        return GuardConfig.load()
    return GuardConfig() if _switch(defences, "defences", ()) else GuardConfig.all_off()


def memory_mode(memory: Any) -> str:
    """"off" | "on" | "playbook" (the frozen playbook: read-only, and it must persist in Postgres)."""
    if memory is None:
        return "on"
    if str(memory).strip().lower() == "playbook":
        return "playbook"
    return "on" if _switch(memory, "memory", ()) else "off"


def memory_on(memory: Any) -> bool:
    return memory_mode(memory) != "off"


def options_problem(options: Mapping[str, Any] | None, env: Mapping[str, str] | None = None) -> str | None:
    """Why these arm options cannot run here, or None. The runner asks before the first case (CopilotAgent)."""
    env = os.environ if env is None else env
    if memory_mode((options or {}).get("memory")) == "playbook" and not env.get("POSTGRES_DSN"):
        return ("memory: playbook needs POSTGRES_DSN (the frozen playbook); without it the store would be a new, "
                "empty in-memory one and the run would be labelled 'playbook' while using no memory")
    return None


def fixed_at(state: Mapping[str, Any]) -> float | None:
    """When the last fix (or follow-up step) was executed; rollbacks are not fixes."""
    times = [e.at.timestamp() for e in state.get("timeline") or [] if e.node == "execute" and e.kind == "action"]
    return max(times) if times else None


async def drive(graph: Any, config: dict[str, Any], alert: Alert, approve: Approve,
                review: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
    """Stream the graph to the end, answering each pause: approval → approve(flat action); review → review(payload)
    or "not accepted" (nothing is stored). Returns the final state values."""
    inp: Any = {"alert": alert}
    while True:
        async for _ in graph.astream(inp, config, stream_mode="updates"):
            pass
        snap = await graph.aget_state(config)
        pending = [i for t in snap.tasks for i in (t.interrupts or ())]
        if not pending:
            return dict(snap.values)
        payload = pending[0].value
        if payload.get("kind") == "approval":
            answer = await _answer(approve, payload["action"])
        elif review is not None:
            answer = await _answer(review, payload)
        else:
            answer = {"accept_postmortem": False, "reviewed_by": "run_incident"}
        inp = Command(resume=answer)


async def _answer(fn: Callable[[Any], Any], arg: Any) -> Any:
    if inspect.iscoroutinefunction(fn):
        return await fn(arg)
    out = await asyncio.to_thread(fn, arg)          # a blocking approver (the runner's) never stalls the graph
    return await out if inspect.isawaitable(out) else out


async def run_incident(alert: Alert | Mapping[str, Any], approve: Approve, *, run_id: str,
                       limits: BudgetLimits | Mapping[str, Any] | None = None, settings: AppSettings | None = None,
                       options: Mapping[str, Any] | None = None, toolbox: McpToolbox | None = None,
                       review: Callable[[dict[str, Any]], Any] | None = None,
                       llm_factory: Callable[[str], Any] | None = None) -> IncidentResult:
    load_dotenv(ROOT / ".env")
    options = dict(options or {})
    settings = settings or load_settings()
    limits = BudgetLimits.model_validate(limits) if isinstance(limits, Mapping) else (limits or settings.limits)
    settings = apply_options(settings, limits, options)
    problem = options_problem(options)
    if problem:
        raise ValueError(problem)
    alert = alert if isinstance(alert, Alert) else Alert.model_validate(dict(alert))
    hooks = Hooks(permissions=load_permissions(str(settings.permissions_path)), max_tool_calls=limits.tool_calls,
                  config=guard_config(options.get("defences")))
    toolbox = toolbox or await McpToolbox.connect(settings.mcp_servers_path, settings.permissions_path)
    dsn = os.getenv("POSTGRES_DSN") or None

    async with make_checkpointer(dsn) as saver, make_store(dsn) as store:
        deps = CopilotDeps(settings=settings, telemetry=McpTelemetry(toolbox), runbooks=McpRunbooks(toolbox),
                           ops=McpOps(toolbox), tickets=McpTickets(toolbox),
                           store=store if memory_on(options.get("memory")) else None, tools_factory=toolbox.for_agent,
                           llm_factory=llm_factory)
        graph = build_graph(deps, checkpointer=saver, store=store)
        config = {"configurable": {"thread_id": run_id, "run_id": run_id, "engineer_id": None}, "recursion_limit": 200}
        with toolctx.scope(hooks=hooks):
            final = await drive(graph, config, alert, approve, review)

    log = hooks.tool_calls()
    counted = [e for e in log if not e["blocked"] and e.get("driver") != toolctx.CODE]
    spent = final["budget"].total if final.get("budget") else None
    return IncidentResult(
        final_state=final, tool_calls=log,
        budget={"max_tool_calls": limits.tool_calls, "tool_calls": len(counted), "code_calls": len(log) - len(counted)
                - sum(e["blocked"] for e in log), "blocked": sum(e["blocked"] for e in log),
                "tokens": spent.tokens if spent else 0, "usd": spent.usd if spent else 0.0},
        fixed_at=fixed_at(final),
        budget_exhausted=budget_exhausted(final.get("budget"), limits)
        or any(e.get("block_reason") in BUDGET_BLOCKS for e in log),
    )


class CopilotAgent:
    """eval/runner's Agent protocol over run_incident; register it in eval.runner.agents.AGENTS as "copilot"."""

    name = "copilot"
    options_supported = ARM_OPTIONS
    needs_approval_secret = True          # the approval gate mints tokens with APPROVAL_SECRET

    def __init__(self, settings: AppSettings | None = None):
        self.settings = settings or load_settings()
        self.version = self.settings.config_hash or "dev"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def _event_loop(self) -> asyncio.AbstractEventLoop:
        """One event loop for the agent's whole life, on its own thread, shared by every case.

        A new loop per case (asyncio.run) breaks the second case of a batch: langchain-openai keeps one shared
        async HTTP client, bound to the loop of the first call, and once that loop is closed every later model
        call fails with "RuntimeError: Event loop is closed"."""
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever, daemon=True, name="copilot-loop").start()
            return self._loop

    def close(self) -> None:
        """Stop the agent's event loop (optional; it is a daemon thread)."""
        with self._lock:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop = None

    def options_problem(self, options: Mapping[str, Any] | None) -> str | None:
        """For the runner's pre-flight (eval.runner.agents.not_ready): checked once, before the first case."""
        from dotenv import dotenv_values

        env = {k: v for k, v in dotenv_values(ROOT / ".env").items() if v} | dict(os.environ)
        return options_problem(options, env)

    def run(self, ctx: Any) -> Any:
        from eval.runner.agents import AgentResult

        limits = self.settings.limits.model_copy(update={"tool_calls": ctx.max_tool_calls})
        future = asyncio.run_coroutine_threadsafe(
            run_incident(ctx.alert, ctx.approve, run_id=ctx.run_id, limits=limits, settings=self.settings,
                         options=ctx.options),
            self._event_loop())
        left = ctx.deadline - ctx.clock() if getattr(ctx, "deadline", None) else None
        try:
            result = future.result(timeout=None if left is None else max(1.0, left))
        except concurrent.futures.TimeoutError:
            future.cancel()                       # a case past its deadline must not keep running into the next one
            raise TimeoutError(f"copilot did not finish {ctx.run_id} before its deadline") from None
        return AgentResult(state=result.final_state, tool_calls=result.tool_calls, fixed_at=result.fixed_at,
                           budget_exhausted=result.budget_exhausted)
