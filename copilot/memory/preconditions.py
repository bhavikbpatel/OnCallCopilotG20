"""Code-side lesson precondition checks against live telemetry. The model never decides these.

Any check that cannot be evaluated (tool error, unknown check, bad args) counts as FAILED.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from copilot.memory.schemas import Lesson, Precondition, PreconditionResult

_DURATION = re.compile(r"^(\d+)([smh])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours"}


class TelemetryClient(Protocol):
    """Thin wrapper over the telemetry MCP server (role 4)."""

    async def recent_changes(self, since: datetime) -> list[dict[str, Any]]:
        """Each item has at least {"kind": "flag"|"deploy", "at": datetime} plus "flag" or "service"."""
        ...

    async def query_scalar(self, query: str) -> float | None:
        """Instant PromQL query reduced to one number; None if no data."""
        ...


def parse_duration(text: str) -> timedelta:
    m = _DURATION.match(text.strip())
    if not m:
        raise ValueError(f"bad duration {text!r}")
    return timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})


async def _check(p: Precondition, telemetry: TelemetryClient, now: datetime) -> PreconditionResult:
    def result(passed: bool, detail: str) -> PreconditionResult:
        return PreconditionResult(check=p.check, args=p.args, passed=passed, detail=detail)

    try:
        if p.check == "recent_change":
            since = now - parse_duration(p.args["within"])
            target_key = "flag" if "flag" in p.args else "service"
            target = p.args[target_key]
            changes = await telemetry.recent_changes(since)
            hit = [c for c in changes if c.get(target_key) == target]
            if hit:
                return result(True, f"{target_key} {target} changed at {hit[-1].get('at')}")
            return result(False, f"no change to {target_key} {target} within {p.args['within']}")

        if p.check in ("promql_above", "promql_below"):
            value = await telemetry.query_scalar(p.args["query"])
            threshold = float(p.args["threshold"])
            if value is None:
                return result(False, "query returned no data")
            passed = value > threshold if p.check == "promql_above" else value < threshold
            op = ">" if p.check == "promql_above" else "<"
            return result(passed, f"value {value:g} {op} {threshold:g} is {passed}")

        return result(False, f"unknown check {p.check}")
    except Exception as exc:  # noqa: BLE001 — any failure means "not applicable"
        return result(False, f"check error: {type(exc).__name__}: {exc}")


async def evaluate_lesson(
    lesson: Lesson, telemetry: TelemetryClient, now: datetime | None = None
) -> tuple[bool, list[PreconditionResult]]:
    """Applicable only if it has preconditions and all pass."""
    now = now or datetime.now(timezone.utc)
    if not lesson.preconditions:
        return False, []
    results = [await _check(p, telemetry, now) for p in lesson.preconditions]
    return all(r.passed for r in results), results
