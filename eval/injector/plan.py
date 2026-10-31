"""Turn a case into a timed list of flag changes (pure; no I/O)."""
from __future__ import annotations

from dataclasses import dataclass

from ..schemas.case import Case
from ..schemas.catalogue import LOAD_FLAG
from .flagstore import REST_VARIANT


@dataclass(frozen=True, order=True)
class FlagEvent:
    at_s: float
    flag: str
    variant: str


def build_plan(case: Case, horizon_s: float) -> list[FlagEvent]:
    """All flag changes for ``case`` within ``horizon_s`` seconds of the start.

    Steady faults switch on once. Intermittent faults alternate on/off. A flood
    switches the load flag on, and off again if it has a duration. Clearing at the
    end is not part of the plan; the injector always does it.
    """
    if horizon_s <= 0:
        raise ValueError("horizon_s must be positive")
    events: list[FlagEvent] = []

    for f in case.faults:
        if f.start_after_s >= horizon_s:
            raise ValueError(f"{f.flag} starts at {f.start_after_s}s, after the {horizon_s}s horizon")
        if f.mode == "steady":
            events.append(FlagEvent(f.start_after_s, f.flag, f.variant))
            continue
        t, on = float(f.start_after_s), True
        while t < horizon_s:
            events.append(FlagEvent(t, f.flag, f.variant if on else REST_VARIANT))
            t += f.on_s if on else f.off_s
            on = not on

    load = case.load
    if load.profile == "flood":
        events.append(FlagEvent(load.flood_start_after_s, LOAD_FLAG, "on"))
        if load.flood_duration_s is not None:
            end = load.flood_start_after_s + load.flood_duration_s
            if end < horizon_s:
                events.append(FlagEvent(end, LOAD_FLAG, REST_VARIANT))

    return sorted(events)


def planned_flags(case: Case) -> set[str]:
    flags = {f.flag for f in case.faults}
    if case.load.profile == "flood":
        flags.add(LOAD_FLAG)
    return flags


def group_by_time(events: list[FlagEvent]) -> list[tuple[float, dict[str, str]]]:
    """Events at the same moment become one write."""
    groups: dict[float, dict[str, str]] = {}
    for e in events:
        groups.setdefault(e.at_s, {})[e.flag] = e.variant
    return sorted(groups.items())
