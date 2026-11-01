"""Synthetic alerts: open the incident when the case's alert would fire.

Until Person 5's Prometheus alert rules exist, the runner plays the alerting
system. A case names its alert (e.g. ``CheckoutErrorRateHigh`` on ``checkout``);
this module turns the name into a signal, measures that signal's healthy level
before injection, and "fires" once the signal is above the alert threshold in
two distinct Prometheus data points in a row.

The threshold is not invented here: it is the movement rule the fault probe
calibrated on control runs (eval/probes/faults.py: MOVE_THRESHOLDS and the
latency traffic tiers), so an alert fires on exactly what the probe counted as
a real effect and not on normal noise.

The alert handed to the agent has the fields of Person 2's ``Alert`` model
(copilot/state.py) and nothing else: no flag names, no case id, no expected answer.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import statistics
from typing import Callable, Optional

from ..probes.faults import (
    ERROR_COUNT_MAX_BASE, LATENCY_LOW_TRAFFIC_FACTOR, LATENCY_LOW_TRAFFIC_RPS, LATENCY_MIN_RPS, MOVE_THRESHOLDS,
)
from ..schemas.case import AlertSpec
from ..telemetry import signals
from ..telemetry.prom import PromClient

WINDOW = "2m"
SUSTAINED_POINTS = 2

# Alert name -> signal. Provisional until Person 5's alert rules exist.
SIGNAL_BY_SUFFIX = (
    ("ConsumerLagHigh", "consumer_lag"),
    ("ErrorRateHigh", "error_rate"),
    ("LatencyHigh", "latency_p95_ms"),
    ("MemoryHigh", "memory_mb"),
    ("TrafficHigh", "throughput_rps"),
)
# cartFailure breaks only EmptyCart: cart's error RATE stays ~1% but its error COUNT
# goes 0 -> 2-4 per window (docs/shop-telemetry-findings.md §6).
SIGNAL_OVERRIDES = {"CartErrorRateHigh": "error_count"}


def signal_for(alert_name: str) -> str:
    """The signal an alert watches. Unknown names are refused (fail closed)."""
    if alert_name in SIGNAL_OVERRIDES:
        return SIGNAL_OVERRIDES[alert_name]
    for suffix, signal in SIGNAL_BY_SUFFIX:
        if alert_name.endswith(suffix):
            return signal
    raise ValueError(f"no signal known for alert {alert_name!r}; expected a name ending in "
                     f"{[s for s, _ in SIGNAL_BY_SUFFIX]}")


def threshold_for(signal: str, base: Optional[float], base_rps: Optional[float]) -> tuple[Optional[float], Optional[str]]:
    """Value at or above which the alert fires, or (None, reason) if it cannot fire fairly.

    Same rule as the fault probe's ``_moved``: at least ``base + absolute`` and
    ``base + relative x |base|``; quiet services need 10x on p95; latency is not
    judged below 0.1 req/s; error counts only on services quiet at rest.
    """
    if base is None:
        return None, f"no data for {signal} before injection"
    if signal.startswith("latency_"):
        if base_rps is None or base_rps < LATENCY_MIN_RPS:
            return None, f"too little traffic ({base_rps} req/s) for a latency alert"
    if signal == "error_count" and base > ERROR_COUNT_MAX_BASE:
        return None, f"error count is already {base:g} at rest"
    absolute, relative = MOVE_THRESHOLDS[signal]
    limit = base + max(absolute, abs(base) * relative)
    if signal == "latency_p95_ms" and base_rps < LATENCY_LOW_TRAFFIC_RPS:
        limit = max(limit, base * LATENCY_LOW_TRAFFIC_FACTOR)
    return limit, None


def measure_alert_baseline(prom: PromClient, spec: AlertSpec, *, readings: int = 3, every_s: float = 30.0,
                           clock: Callable[[], float], sleep: Callable[[float], None]) -> dict:
    """Median of a few readings of the alert's signal (and its service's traffic) before injection."""
    signal = signal_for(spec.name)
    values, rps = [], []
    for i in range(readings):
        if i:
            sleep(every_s)
        v = signals.measure(prom, signal, spec.service, WINDOW)
        r = signals.measure(prom, "throughput_rps", spec.service, WINDOW)
        if v is not None:
            values.append(v)
        if r is not None:
            rps.append(r)
    return {"signal": signal, "value": statistics.median(values) if values else None,
            "rps": statistics.median(rps) if rps else None, "at": clock()}


def wait_for_alert(prom: PromClient, spec: AlertSpec, baseline: dict, *, run_id: str, deadline: float,
                   clock: Callable[[], float], sleep: Callable[[float], None],
                   poll_s: float = 30.0) -> tuple[Optional[dict], Optional[str]]:
    """Poll until the alert fires; returns (alert, None) or (None, why it did not fire)."""
    signal = baseline["signal"]
    limit, why = threshold_for(signal, baseline["value"], baseline["rps"])
    if limit is None:
        return None, f"alert {spec.name} cannot fire: {why}"
    query = signals.promql(signal, spec.service, WINDOW)
    last: Optional[tuple[float, float]] = None
    above = 0
    while True:
        t = clock()
        value = signals.measure(prom, signal, spec.service, WINDOW)
        if value is not None and not (last and last[1] == value and t - last[0] < signals.SAME_POINT_WINDOW_S):
            last = (t, value)
            above = above + 1 if value >= limit else 0
            if above >= SUSTAINED_POINTS:
                return _alert(spec, signal, query, limit, value, t, run_id), None
        if t >= deadline:
            return None, (f"alert {spec.name} did not fire: {signal} on {spec.service} never reached "
                          f"{limit:g} in {SUSTAINED_POINTS} data points in a row")
        sleep(min(poll_s, deadline - t))


def _alert(spec: AlertSpec, signal: str, query: str, limit: float, value: float, t: float, run_id: str) -> dict:
    fired_at = dt.datetime.fromtimestamp(t, dt.timezone.utc)
    return {
        "alert_name": spec.name,
        "service": spec.service,
        "signal": signal,
        "summary": f"{spec.name}: {spec.service} {signal} is {value:g} (threshold {limit:g})",
        "query": query,
        "threshold": limit,
        "value": value,
        "fired_at": fired_at.isoformat(),
        "fingerprint": hashlib.sha256(f"{run_id}|{spec.name}|{spec.service}".encode()).hexdigest()[:16],
    }
