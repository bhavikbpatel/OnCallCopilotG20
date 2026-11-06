"""Check POSTGRES_DSN: connect, create the LangGraph checkpointer and store tables, write / read / delete one item.

    python scripts/check_postgres.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from copilot.config import ROOT  # noqa: E402
from copilot.memory import make_checkpointer, make_store  # noqa: E402
from copilot.memory.backends import loop_factory  # noqa: E402


async def check(dsn: str) -> None:
    async with make_checkpointer(dsn), make_store(dsn) as store:       # both run their table setup
        ns, key = ("_healthcheck",), "ping"
        await store.aput(ns, key, {"ok": True}, index=False)
        item = await store.aget(ns, key)
        await store.adelete(ns, key)
        assert item is not None and item.value == {"ok": True}, "store write/read failed"
        lessons = await store.asearch(("lessons",), limit=1000)
    print(f"ok: checkpointer and store ready; shared playbook namespace holds {len(lessons)} lesson(s)")


def main() -> None:
    load_dotenv(ROOT / ".env")
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise SystemExit("POSTGRES_DSN is not set (put it in .env)")
    u = urlparse(dsn)
    print(f"connecting to {u.hostname}:{u.port or 5432}{u.path} as {u.username}")
    asyncio.run(check(dsn), loop_factory=loop_factory(dsn))


if __name__ == "__main__":
    main()
