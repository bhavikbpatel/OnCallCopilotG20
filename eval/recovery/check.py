"""Independent recovery check: did the shop really recover after the fix?

The verdict comes from Prometheus, never from the agent's claim (capstone guide pp. 5-6:
"recovery is judged by code from metrics"). It fills the ``recovery`` part of the
result line (docs/eval-result-contract.md).

A case's ``expected.recovery`` (eval/schemas/case.py: RecoveryCheck) says how to judge:

- ``measure: since_fix`` (error_rate, error_count): count server-span errors and
  requests from ``settle_s`` after the fix until ``within_s`` after it, once that
  data has reached Prometheus, and compare with ``threshold``. Low-traffic paths
  (checkout: ~2.5 requests/min) cannot be judged from a short fixed window.
- ``measure: window`` (any other signal): read the signal's 2-minute value every
  ``poll_s`` from ``settle_s`` after the fix. Recovered once it is past the limit
  in ``SUSTAINED_POINTS`` distinct Prometheus data points in a row; not recovered if
  that never happens by ``within_s``. The limit is ``threshold``, or
  ``baseline_factor`` x the value measured just before injection. Readings are of
  "now", so this must run while the window is open; started after ``within_s`` it
  reports "cannot judge". A since-fix count can also be taken afterwards.

Rules from docs/shop-telemetry-findings.md, applied here:
- "No data" is never "healthy": no data, too few requests, no baseline or an
  unreachable Prometheus give ``recovered = None`` with a ``reason``.
- Prometheus lags 12-40 s: nothing is read before ``fixed_at + PROM_LAG_S``, and a
  since-fix count waits until the end of its range has arrived.
- One Prometheus point a minute: identical readings < 45 s apart are one point,
  so one lucky dip never counts as recovery.
- Direction comes from the case (``op``): lower is better for errors, latency,
  memory and lag; higher for throughput (enforced when the case loads).
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from ..schemas.case import RecoveryCheck
from ..schemas.result import RecoveryObservation
from ..telemetry import signals
from ..telemetry.prom import PromClient, PromError

SUSTAINED_POINTS = 2       # distinct data points past the limit, in a row
DEFAULT_POLL_S = 30.0      # about two readings per Prometheus point
DEFAULT_MIN_REQUESTS = 5   # since_fix: fewer requests than this cannot be judged
                           # (same value as Person 2's verify_min_requests)
WINDOW = "2m"              # shortest window with data (signals.MIN_WINDOW_S)


def limit_for(check: RecoveryCheck, baseline: Optional[float]) -> tuple[Optional[float], Optional[str]]:
    """The number to compare with, or (None, reason) when it cannot be known."""
    if check.threshold is not None:
        return check.threshold, None
    if baseline is None:
        return None, "no baseline: the signal had no data just before injection"
    if baseline <= 0:
        return None, f"baseline is {baseline:g}; {check.baseline_factor:g} x baseline cannot be judged"
    return check.baseline_factor * baseline, None


def passes(value: float, limit: float, op: str) -> bool:
    return value < limit if op == "lt" else value > limit


def measure_baseline(prom: PromClient, check: RecoveryCheck, at: Optional[float] = None) -> Optional[float]:
    """The signal's value just before injection; the runner calls this for baseline-form checks.

    Returns None for threshold-form checks (no baseline needed) and when there is no data.
    Raises PromError if Prometheus cannot be reached, so the runner refuses to start the case.
    """
    if check.baseline_factor is None:
        return None
    return signals.measure(prom, check.signal, check.service, WINDOW, at)


def check_recovery(
    prom: PromClient,
    check: RecoveryCheck,
    fixed_at: float,
    *,
    baseline: Optional[float] = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = DEFAULT_POLL_S,
    min_requests: int = DEFAULT_MIN_REQUESTS,
) -> RecoveryObservation:
    """Judge recovery for one run. Blocks until it can decide: at most within_s after the fix
    (window form), or within_s + PROM_LAG_S (since_fix, so the end of the range has arrived).

    ``fixed_at``: unix time the fix was executed (or, if the agent did nothing, the time
    from which the runner wants recovery judged). ``baseline``: from ``measure_baseline``
    before injection; needed only when the case uses ``baseline_factor``.
    """
    if poll_s <= 0:
        raise ValueError("poll_s must be positive")
    base = {"checked": True, "signal": check.signal, "service": check.service,
            "measure": check.measure, "op": check.op, "baseline": baseline}

    def unknown(reason: str, **kw) -> RecoveryObservation:
        return RecoveryObservation(**base, recovered=None, reason=reason, **kw)

    limit, why = limit_for(check, baseline)
    if limit is None:
        return unknown(why)
    base["limit"] = limit
    query_signal = "error_rate" if check.measure == "since_fix" else check.signal
    if not signals.supported(query_signal, check.service):
        return unknown(f"{check.service} has no data for {query_signal} in this shop "
                       f"(no span metrics or not a Kafka consumer)")

    def wait_until(t: float) -> None:
        while (now := clock()) < t:
            sleep(t - now)

    try:
        if check.measure == "since_fix":
            return _since_fix(prom, check, fixed_at, limit, base, wait_until, min_requests)
        return _window(prom, check, fixed_at, limit, base, wait_until, clock, sleep, poll_s)
    except PromError as exc:
        return unknown(f"Prometheus error: {exc}")


def _since_fix(prom, check, fixed_at, limit, base, wait_until, min_requests) -> RecoveryObservation:
    since = fixed_at + check.settle_s
    until = fixed_at + check.within_s
    wait_until(until + signals.PROM_LAG_S)          # let the end of the range arrive
    counts = signals.errors_since(prom, check.service, since, until)
    errors, requests = counts["errors"], counts["requests"]
    value = errors if check.signal == "error_count" else counts["error_rate"]
    query = (f"increase({signals.CALLS}{{service_name=\"{check.service}\",server spans}}) "
             f"from {since:.0f} to {until:.0f} (errors and requests)")
    obs = dict(base, errors=errors, requests=requests, value=value, query=query, observed_at=until)
    if requests is None or requests < min_requests or value is None:
        return RecoveryObservation(**obs, recovered=None,
                                   reason=f"too little traffic to judge: {requests} requests since the fix "
                                          f"(need at least {min_requests})")
    return RecoveryObservation(**obs, recovered=passes(value, limit, check.op))


def _window(prom, check, fixed_at, limit, base, wait_until, clock, sleep, poll_s) -> RecoveryObservation:
    deadline = fixed_at + check.within_s
    query = signals.promql(check.signal, check.service, WINDOW)
    wait_until(fixed_at + max(check.settle_s, signals.PROM_LAG_S))
    if clock() > deadline:
        # Readings are of "now", so a window that has already passed cannot be replayed.
        return RecoveryObservation(**base, recovered=None, query=query,
                                   reason="check started after the recovery window had ended; "
                                          "window checks must run while the window is open")
    points: list[tuple[float, float]] = []
    good_in_a_row = 0
    while True:
        t = clock()
        value = signals.measure(prom, check.signal, check.service, WINDOW)
        new_point = value is not None and not (
            points and points[-1][1] == value and t - points[-1][0] < signals.SAME_POINT_WINDOW_S)
        if new_point:
            points.append((t, value))
            good_in_a_row = good_in_a_row + 1 if passes(value, limit, check.op) else 0
            if good_in_a_row >= SUSTAINED_POINTS:
                return RecoveryObservation(**base, recovered=True, value=value, query=query,
                                           points=points, observed_at=t)
        if t >= deadline:
            break
        sleep(min(poll_s, deadline - t))
    if not points:
        return RecoveryObservation(**base, recovered=None, query=query,
                                   reason=f"no data for {check.signal} on {check.service} after the fix")
    last_t, last_v = points[-1]
    return RecoveryObservation(**base, recovered=False, value=last_v, query=query, points=points,
                               observed_at=last_t,
                               reason=f"not past the limit in {SUSTAINED_POINTS} distinct data points in a row "
                                      f"within {check.within_s} s")
