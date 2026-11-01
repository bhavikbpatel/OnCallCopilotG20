"""Checkpointer (short-term) and store (long-term) factories.

Usage:
    async with make_checkpointer(dsn) as saver, make_store(dsn, embed=...) as store:
        graph = builder.compile(checkpointer=saver, store=store)

dsn=None → in-memory backends (dev / unit tests).
"""
from __future__ import annotations

import enum
import importlib
import inspect
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from pydantic import BaseModel

INCIDENT_INDEX_FIELDS = ["symptom_text"]
_STATE_MODULES = ("copilot.state", "copilot.memory.schemas")


def make_serde() -> JsonPlusSerializer:
    """Allow-list our own state types for checkpoint deserialization (nothing else is revived)."""
    allowed = []
    for name in _STATE_MODULES:
        module = importlib.import_module(name)
        for cls_name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ == name and issubclass(cls, (BaseModel, enum.Enum)):
                allowed.append((name, cls_name))
    return JsonPlusSerializer(allowed_msgpack_modules=allowed)


@asynccontextmanager
async def make_checkpointer(dsn: str | None = None) -> AsyncIterator[BaseCheckpointSaver]:
    if dsn is None:
        yield InMemorySaver(serde=make_serde())
        return
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(dsn, serde=make_serde()) as saver:
        await saver.setup()
        yield saver


@asynccontextmanager
async def make_store(
    dsn: str | None = None,
    embed: Callable[[list[str]], list[list[float]]] | Any | None = None,
    dims: int | None = None,
) -> AsyncIterator[BaseStore]:
    """`embed` + `dims` enable semantic search over incidents (`symptom_text`).
    Without them, incident search falls back to metadata filters only."""
    index = {"dims": dims, "embed": embed, "fields": INCIDENT_INDEX_FIELDS} if embed and dims else None
    if dsn is None:
        yield InMemoryStore(index=index) if index else InMemoryStore()
        return
    from langgraph.store.postgres.aio import AsyncPostgresStore

    kwargs = {"index": index} if index else {}
    async with AsyncPostgresStore.from_conn_string(dsn, **kwargs) as store:
        await store.setup()
        yield store
