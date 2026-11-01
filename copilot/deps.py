"""Everything nodes need at runtime. Not checkpointed — rebuilt on process start."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.store.base import BaseStore

from copilot.clients import OpsClient, RunbooksClient, TelemetryClient, TicketsClient
from copilot.config import AppSettings
from copilot.memory import LongTermMemory


@dataclass
class CopilotDeps:
    settings: AppSettings
    telemetry: TelemetryClient
    runbooks: RunbooksClient
    ops: OpsClient
    tickets: TicketsClient
    store: BaseStore | None                                   # None → memory off (E4)
    tools_factory: Callable[[str], list[BaseTool]]            # per-agent filtered MCP tools, hooks applied
    llm_factory: Callable[[str], BaseChatModel] | None = None

    def llm(self, agent: str) -> BaseChatModel:
        if self.llm_factory:
            return self.llm_factory(agent)
        from copilot.llm import RetryingChatOpenAI

        # OpenRouter via its OpenAI-compatible API; key in OPENAI_API_KEY.
        cfg = self.settings.models[agent]
        return RetryingChatOpenAI(model=cfg.model, temperature=cfg.temperature, base_url=self.base_url, agent=agent)

    @property
    def base_url(self) -> str:
        return os.getenv("OPENAI_BASE_URL") or self.settings.llm_base_url

    def tools(self, agent: str) -> list[BaseTool]:
        return self.tools_factory(agent)

    def memory(self, run_id: str | None) -> LongTermMemory:
        return LongTermMemory(self.store, run_id=run_id, read_only=self.settings.memory_read_only)
