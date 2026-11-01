"""Spent approval tokens: a token works once, even across a server restart.

Backed by SQLite. `claim()` is one atomic INSERT on a primary key, so two calls (or two
server processes) racing with the same token cannot both win. Only a hash of the token is
stored. Path: APPROVAL_STATE_DB (default approval_state.db in the working directory;
use ":memory:" in tests). Give every server and the hooks the same path so a token spent
in one is spent in all.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from typing import Callable, Optional


class UsedTokens:
    def __init__(self, path: Optional[str] = None, clock: Callable[[], float] = time.time):
        self.path = path or os.environ.get("APPROVAL_STATE_DB") or "approval_state.db"
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._db.execute("CREATE TABLE IF NOT EXISTS used_tokens "
                         "(token_hash TEXT PRIMARY KEY, claimed_at REAL NOT NULL)")
        self._db.commit()

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def claim(self, token: str) -> bool:
        """True if this is the first use of the token (and it is now spent); False if already spent."""
        with self._lock:
            try:
                self._db.execute("INSERT INTO used_tokens(token_hash, claimed_at) VALUES (?, ?)",
                                 (self._hash(token), self._clock()))
                self._db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def release(self, token: str) -> None:
        """Give the token back after an execution that failed before changing anything."""
        with self._lock:
            self._db.execute("DELETE FROM used_tokens WHERE token_hash = ?", (self._hash(token),))
            self._db.commit()

    def is_spent(self, token: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT 1 FROM used_tokens WHERE token_hash = ?", (self._hash(token),)).fetchone()
        return row is not None
