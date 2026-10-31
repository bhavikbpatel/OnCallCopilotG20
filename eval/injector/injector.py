"""Run a case's fault plan against the shop and always restore it.

Usage:
    with Injector(store).run(case, horizon_s=600, run_id="E1-C2-D-001-r1") as inj:
        ...  # agent works on the incident
    record = inj.record   # flags are back at rest here, whatever happened

Contamination: the flag UI's scheduler has no API, so instead of asking whether
it is on, the injector checks that nothing outside the plan moved. Any flag not in
the plan that leaves rest, or a planned flag that is not at the value we set, is
recorded as drift and the run is marked contaminated.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import threading
import time
from typing import Callable, Iterator, Optional

from ..schemas.case import Case
from ..schemas.catalogue import FLAG_VARIANTS
from .flagstore import REST_VARIANT, FlagStore, FlagStoreError, apply, variants_of
from .plan import build_plan, group_by_time, planned_flags
from .record import InjectionRecord


class PreflightError(RuntimeError):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Injector:
    def __init__(
        self,
        store: FlagStore,
        check_every_s: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], dt.datetime] = _now,
        waiter: Optional[Callable[[threading.Event, float], bool]] = None,
    ):
        self.store = store
        self.check_every_s = check_every_s
        self._clock = clock
        self._wall = wall
        # waiter(stop_event, seconds) -> True if stop was requested. Tests replace it.
        self._wait = waiter or (lambda ev, s: ev.wait(max(0.0, s)))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.record: Optional[InjectionRecord] = None
        self._expected: dict[str, str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ checks

    def status(self) -> dict[str, str]:
        return variants_of(self.store.read())

    def not_at_rest(self) -> dict[str, str]:
        return {k: v for k, v in self.status().items() if v != REST_VARIANT}

    def preflight(self, force_clear: bool = False) -> None:
        """Refuse to start unless every flag is at rest (or clear them if forced)."""
        current = self.status()
        missing = sorted(set(FLAG_VARIANTS) - set(current))
        if missing:
            raise PreflightError(f"shop is missing expected 3.1.0 flags: {missing}")
        dirty = {k: v for k, v in current.items() if v != REST_VARIANT}
        if dirty and not force_clear:
            raise PreflightError(f"flags not at rest before start: {dirty}. Run 'clear' first.")
        if dirty:
            self.clear()

    def _check_drift(self) -> None:
        current = self.status()
        with self._lock:
            expected = dict(self._expected)
        for flag, variant in sorted(current.items()):
            want = expected.get(flag, REST_VARIANT)
            if variant != want:
                self.record.add_drift(self._wall(), flag, variant, want)

    # ------------------------------------------------------------ actions

    def clear(self) -> dict[str, str]:
        """Put every flag back to rest and confirm."""
        dirty = self.not_at_rest()
        if dirty:
            apply(self.store, {k: REST_VARIANT for k in dirty})
        remaining = self.not_at_rest()
        if remaining:
            raise FlagStoreError(f"flags still not at rest after clear: {remaining}")
        return dirty

    def _execute(self, groups: list[tuple[float, dict[str, str]]], t0: float) -> None:
        try:
            pending = list(groups)
            next_check = self.check_every_s
            while True:
                elapsed = self._clock() - t0
                while pending and pending[0][0] <= elapsed:
                    at_s, changes = pending.pop(0)
                    apply(self.store, changes)
                    with self._lock:
                        self._expected.update(changes)
                    self.record.add_applied(at_s, self._wall(), changes)
                    self._check_drift()
                if elapsed >= next_check:
                    self._check_drift()
                    next_check = elapsed + self.check_every_s
                if pending:
                    next_due = min(pending[0][0], next_check)
                else:
                    next_due = next_check
                if self._wait(self._stop, next_due - (self._clock() - t0)):
                    return
        except Exception as exc:  # recorded; the caller still clears
            self.record.errors.append(f"{type(exc).__name__}: {exc}")

    def start(self, case: Case, horizon_s: float, run_id: str, background: bool = True,
              force_clear: bool = False) -> InjectionRecord:
        if self._thread is not None:
            raise RuntimeError("injector already running")
        plan = build_plan(case, horizon_s)
        self.preflight(force_clear=force_clear)
        self._stop.clear()
        with self._lock:
            self._expected = {}
        self.record = InjectionRecord.new(run_id, case, horizon_s, plan, sorted(planned_flags(case)), self._wall())
        t0 = self._clock()
        groups = group_by_time(plan)
        if background:
            self._thread = threading.Thread(target=self._execute, args=(groups, t0), daemon=True,
                                            name=f"injector-{run_id}")
            self._thread.start()
        else:
            self._execute(groups, t0)
        return self.record

    def stop(self) -> InjectionRecord:
        """Stop the plan, record final drift, clear everything. Safe to call twice."""
        if self.record is None:
            raise RuntimeError("injector was never started")
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
            self._thread = None
        if self.record.ended_at is None:
            try:
                self._check_drift()
            except Exception as exc:
                self.record.errors.append(f"final check: {type(exc).__name__}: {exc}")
            try:
                self.clear()
                self.record.cleared = True
            except Exception as exc:
                self.record.errors.append(f"clear: {type(exc).__name__}: {exc}")
            self.record.ended_at = self._wall()
        return self.record

    @contextlib.contextmanager
    def run(self, case: Case, horizon_s: float, run_id: str, force_clear: bool = False) -> Iterator["Injector"]:
        try:
            self.start(case, horizon_s, run_id, force_clear=force_clear)
            yield self
        finally:
            if self.record is not None:
                self.stop()
