"""LangSmith tracing for the copilot (Person 5): every LLM call and tool call, with secrets and PII masked.

Off unless `.env` sets LANGSMITH_TRACING=true and LANGSMITH_API_KEY (and optionally LANGSMITH_PROJECT).
LangChain and LangGraph trace themselves; this module only makes every trace go through `mask` first.
Call `setup()` once per process, after `.env` is loaded and before the graph runs:

    from copilot import tracing
    tracing.setup()          # True when tracing is on

`setup()` replaces LangSmith's process-wide client (`langsmith.configure`), so threads and event loops
started later (the eval runner drives each incident on its own thread) trace through the same masking client.
"""
from __future__ import annotations

import os
import re
from typing import Any, Sequence

from mcp_servers.common import redact

DEFAULT_PROJECT = "oncall-copilot"

# Values under these keys are dropped whole, wherever they sit in a trace: the approval token in a gated
# tool call's arguments, and anything that is a credential by name. Every other string goes through `redact`
# (keys, bearer tokens, emails, card numbers, random-looking runs), the same filter the MCP servers use.
MASKED_KEYS = frozenset({"approval_token", "api_key", "apikey", "authorization", "password", "secret", "token"})

# Identifiers, kept as they are: random-looking (so `redact` would hide them) but not secrets, and needed to
# find an incident's trace (run_id, incident_id, thread_id) or follow a tool call to its result.
ID_KEYS = frozenset({"id", "run_id", "incident_id", "thread_id", "tool_call_id", "checkpoint_id", "trace_id",
                     "span_id", "payload_hash"})

# A decimal number (0.4123456789012345, 739.12, 1e-05 after a digit): `redact` reads a run of exactly 16
# digits as a card number, and Prometheus values often have 16 decimal places, so the trace would lose the
# error rates and latencies it exists to show. Decimals are set aside before `redact` and put back after.
_DECIMAL = re.compile(r"(?<![\w.])\d+\.\d+(?:[eE][-+]?\d+)?(?![\w.])")
_HELD = re.compile("\x00(\\d+)\x00")

_installed = False


def _redact_keeping_decimals(text: str) -> str:
    held: list[str] = []

    def hold(match: re.Match) -> str:
        held.append(match.group(0))
        return f"\x00{len(held) - 1}\x00"

    out = redact(_DECIMAL.sub(hold, text))
    return _HELD.sub(lambda m: held[int(m.group(1))], out) if held else out


def mask(value: str, path: Sequence[Any] = ()) -> str:
    """Mask one string from a trace; `path` is where it sits (dict keys and list indexes)."""
    key = path[-1] if path else None
    if isinstance(key, str):
        if key.lower() in MASKED_KEYS:
            return "[REDACTED]"
        if key.lower() in ID_KEYS:
            return value
    return _redact_keeping_decimals(value)


def enabled() -> bool:
    return os.getenv("LANGSMITH_TRACING", "").strip().lower() == "true" and bool(os.getenv("LANGSMITH_API_KEY"))


def setup() -> bool:
    """Install the masking client as LangSmith's default and switch tracing on. No-op when off or done."""
    global _installed
    if not enabled():
        return False
    if _installed:
        return True
    import langsmith
    from langsmith.anonymizer import create_anonymizer

    client = langsmith.Client(anonymizer=create_anonymizer(mask))
    langsmith.configure(client=client, enabled=True,
                        project_name=os.getenv("LANGSMITH_PROJECT") or DEFAULT_PROJECT)
    _installed = True
    return True
