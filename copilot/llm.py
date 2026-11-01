"""Chat model for OpenRouter: logs every 400 with agent, model and the upstream error body.

OpenRouter wraps an upstream provider's 400 as "Provider returned error"; the real reason is only in
body.metadata.raw, so it is logged. Those are retried once (a provider can fail where another succeeds);
other 400s are raised at once. (The 1-in-4 failures in the judge previews were a deterministic request
bug in structured_call's retry, found through this log and fixed there.)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import openai
from langchain_openai import ChatOpenAI

log = logging.getLogger("copilot.llm")

PROVIDER_ERROR = "Provider returned error"


class RetryingChatOpenAI(ChatOpenAI):
    agent: str = ""
    provider_retries: int = 1
    retry_backoff_s: float = 1.0

    def _retry_after(self, exc: openai.BadRequestError, attempt: int) -> float | None:
        """Log the 400; seconds to wait before retrying, or None to raise."""
        body = getattr(exc, "body", None)
        retry = PROVIDER_ERROR in f"{exc} {body}" and attempt < self.provider_retries
        log.log(logging.WARNING if retry else logging.ERROR, "LLM 400 agent=%s model=%s attempt=%d/%d retry=%s body=%s",
                self.agent, self.model_name, attempt + 1, self.provider_retries + 1, retry, body or exc)
        return self.retry_backoff_s * (attempt + 1) if retry else None

    async def _agenerate(self, *args: Any, **kwargs: Any):
        for attempt in range(self.provider_retries + 1):
            try:
                return await super()._agenerate(*args, **kwargs)
            except openai.BadRequestError as exc:
                wait = self._retry_after(exc, attempt)
                if wait is None:
                    raise
                await asyncio.sleep(wait)

    def _generate(self, *args: Any, **kwargs: Any):
        for attempt in range(self.provider_retries + 1):
            try:
                return super()._generate(*args, **kwargs)
            except openai.BadRequestError as exc:
                wait = self._retry_after(exc, attempt)
                if wait is None:
                    raise
                time.sleep(wait)
