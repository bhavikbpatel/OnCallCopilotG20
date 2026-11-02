"""Read and write Astronomy Shop feature flags.

The flag UI (flagd-ui) owns the flag file: it loads it once at start-up and writes
its whole in-memory copy back on every change. So all changes go through its API,
never through the file. ``/api/write`` replaces the whole configuration and is
asynchronous, so writes are confirmed by reading back.

The flag UI sometimes answers 502/503/504 or drops a connection for a moment (seen
right after a shop restart). Every request is retried a few times with a growing wait
before it counts as a failure: a read is harmless to repeat, and a write sends the
whole configuration and is confirmed by reading back, so repeating it is safe too.
"""
from __future__ import annotations

import copy
import json
import os
import time
import urllib.error
import urllib.request
from typing import Callable, Protocol

FlagMap = dict[str, dict]

DEFAULT_BASE_URL = "http://localhost:8080/feature"
SCHEMA_URL = "https://flagd.dev/schema/v0/flags.json"
REST_VARIANT = "off"  # every flag in 3.1.0 rests at "off"


class FlagStoreError(RuntimeError):
    pass


TRANSIENT_HTTP = frozenset({502, 503, 504})   # proxy or flag UI briefly unavailable


# ---------------------------------------------------------------- variant helpers

def _targeting_if(flag: dict):
    branch = flag.get("targeting", {}).get("if")
    return branch if isinstance(branch, list) and len(branch) == 3 else None


def get_variant(flag: dict) -> str:
    """The variant that is effectively 'on' for this flag.

    Flags with a targeting rule (productCatalogFailure) switch through the rule's
    'then' branch, exactly as the flag UI does.
    """
    branch = _targeting_if(flag)
    return branch[1] if branch is not None else flag["defaultVariant"]


def set_variant(flag: dict, variant: str) -> dict:
    """Return a copy of ``flag`` switched to ``variant``."""
    if variant not in flag.get("variants", {}):
        raise ValueError(f"variant {variant!r} not offered; choose from {sorted(flag.get('variants', {}))}")
    new = copy.deepcopy(flag)
    branch = _targeting_if(new)
    if branch is not None:
        branch[1] = variant
    else:
        new["defaultVariant"] = variant
    return new


def variants_of(flags: FlagMap) -> dict[str, str]:
    return {name: get_variant(data) for name, data in flags.items()}


# ---------------------------------------------------------------- stores

class FlagStore(Protocol):
    def read(self) -> FlagMap: ...
    def write(self, flags: FlagMap) -> None: ...


def apply(store: FlagStore, changes: dict[str, str]) -> dict[str, str]:
    """Set several flags in one write and confirm. Returns the confirmed variants."""
    flags = store.read()
    unknown = sorted(set(changes) - set(flags))
    if unknown:
        raise FlagStoreError(f"unknown flags: {unknown}")
    for name, variant in changes.items():
        flags[name] = set_variant(flags[name], variant)
    store.write(flags)
    return variants_of(store.read())


class InMemoryFlagStore:
    """Fake store for tests. Optionally records every write."""

    def __init__(self, flags: FlagMap):
        self._flags = copy.deepcopy(flags)
        self.writes: list[dict[str, str]] = []

    def read(self) -> FlagMap:
        return copy.deepcopy(self._flags)

    def write(self, flags: FlagMap) -> None:
        self._flags = copy.deepcopy(flags)
        self.writes.append(variants_of(flags))

    def poke(self, name: str, variant: str) -> None:
        """Simulate someone else (scheduler, UI) changing a flag."""
        self._flags[name] = set_variant(self._flags[name], variant)


class HttpFlagStore:
    """Client for the flag UI API, reached through the shop's proxy."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout_s: float = 5.0,
        confirm_timeout_s: float = 10.0,
        poll_s: float = 0.2,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        retries: int = 3,
        backoff_s: float = 1.0,
    ):
        self.base_url = (base_url or os.environ.get("FLAG_UI_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout_s = timeout_s
        self.confirm_timeout_s = confirm_timeout_s
        self.poll_s = poll_s
        self._sleep = sleep
        self._clock = clock
        self.retries = max(0, retries)        # extra attempts after the first; waits backoff_s, 2x, 4x...
        self.backoff_s = backoff_s

    def _request(self, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method="GET" if body is None else "POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    raw = resp.read()
                break
            except urllib.error.HTTPError as exc:        # an answer: retry only "briefly unavailable"
                if exc.code not in TRANSIENT_HTTP or attempt >= self.retries:
                    raise FlagStoreError(f"flag UI not reachable at {self.base_url}: {exc}"
                                         + (f" (after {attempt + 1} attempts)" if attempt else "")) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:   # no answer at all
                if attempt >= self.retries:
                    raise FlagStoreError(f"flag UI not reachable at {self.base_url}: {exc}"
                                         + (f" (after {attempt + 1} attempts)" if attempt else "")) from exc
            self._sleep(self.backoff_s * (2 ** attempt))
            attempt += 1
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise FlagStoreError(f"flag UI returned non-JSON from {path}") from exc

    def read(self) -> FlagMap:
        payload = self._request("/api/read")
        flags = payload.get("flags")
        if not isinstance(flags, dict) or not flags:
            raise FlagStoreError("flag UI returned no flags")
        return flags

    def write(self, flags: FlagMap) -> None:
        self._request("/api/write", {"data": {"$schema": SCHEMA_URL, "flags": flags}})
        wanted = variants_of(flags)
        deadline = self._clock() + self.confirm_timeout_s
        while True:
            current = variants_of(self.read())
            if all(current.get(k) == v for k, v in wanted.items()):
                return
            if self._clock() >= deadline:
                diff = {k: (current.get(k), v) for k, v in wanted.items() if current.get(k) != v}
                raise FlagStoreError(f"write not confirmed within {self.confirm_timeout_s}s: {diff}")
            self._sleep(self.poll_s)
