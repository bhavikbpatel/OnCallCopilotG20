"""Wire everything for the incident API: env, OpenAI, MCP tools, checkpointer, store, graph.

    async with copilot_runtime() as rt:
        async for ev in rt.graph.astream({"alert": alert}, config, stream_mode=["updates", "custom"]): ...
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from dotenv import load_dotenv
from langgraph.graph.state import CompiledStateGraph

from copilot import tracing
from copilot.config import ROOT, AppSettings, load_settings
from copilot.deps import CopilotDeps
from copilot.graph import build_graph
from copilot.memory import make_checkpointer, make_store
from copilot.tools import McpOps, McpRunbooks, McpTelemetry, McpTickets, McpToolbox


@dataclass
class Runtime:
    graph: CompiledStateGraph
    deps: CopilotDeps
    toolbox: McpToolbox


@asynccontextmanager
async def copilot_runtime(settings: AppSettings | None = None) -> AsyncIterator[Runtime]:
    load_dotenv(ROOT / ".env")
    tracing.setup()                  # LangSmith, masked; off unless LANGSMITH_TRACING=true (Person 5)
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set (put it in .env, see .env.example)")
    settings = settings or load_settings()
    dsn = os.getenv("POSTGRES_DSN") or None          # unset → in-memory backends

    toolbox = await McpToolbox.connect(settings.mcp_servers_path, settings.permissions_path)
    if os.getenv("COPILOT_HOOKS") == "1":
        # Opt-in (Person 3): every code-driven tool call goes through the hooks (permission, approval, budget,
        # loop, injection filter, logging). One Hooks per process, so use it for one incident per process (CLI,
        # demo, eval case); a per-incident reset is a follow-up. Its call cap is a runaway guard (verify polls
        # make many calls), not the graph's own limits.tool_calls budget.
        from copilot.hooked_toolbox import HookedToolbox
        from copilot.hooks import Hooks

        toolbox = HookedToolbox(toolbox, Hooks(max_tool_calls=int(os.getenv("HOOKS_MAX_TOOL_CALLS", "200"))))

    embed = None
    if settings.embedding_model:
        from langchain_openai import OpenAIEmbeddings

        embed = OpenAIEmbeddings(model=settings.embedding_model,
                                 base_url=os.getenv("OPENAI_BASE_URL") or settings.llm_base_url)

    async with make_checkpointer(dsn) as saver, make_store(dsn, embed=embed, dims=settings.embedding_dims) as store:
        deps = CopilotDeps(
            settings=settings,
            telemetry=McpTelemetry(toolbox),
            runbooks=McpRunbooks(toolbox),
            ops=McpOps(toolbox),
            tickets=McpTickets(toolbox),
            store=store,
            tools_factory=toolbox.for_agent,
        )
        yield Runtime(graph=build_graph(deps, checkpointer=saver, store=store), deps=deps, toolbox=toolbox)
