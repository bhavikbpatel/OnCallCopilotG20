"""Recovery checker: judged by code from metrics; no data is never healthy.

Uses an in-process fake Prometheus and a fake clock, so waits take no real time.
One contract test goes through the real HTTP client against a local fake server.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from eval.recovery import SUSTAINED_POINTS, check_recovery, limit_for, measure_baseline
from eval.schemas.case import RecoveryCheck, load_cases
from eval.telemetry import signals
from eval.telemetry.prom import PromClient, PromError
from eval.tests.fakes import FakePrometheus

FIX = 1_000_000.0  # unix time the fix was executed


class Clock:
    def __init__(self, t: float = FIX):
        self.t = t
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        assert seconds > 0, "never sleep zero or negative (would spin)"
        self.sleeps.append(seconds)
        self.t += seconds


class Prom:
    """query_value(expr, at) answered by ``answer(expr, now)``; records every query."""

    def __init__(self, clock: Clock, answer):
        self.clock, self.answer, self.queries = clock, answer, []

    def query_value(self, expr, at=None):
        self.queries.append((expr, at, self.clock()))
        return self.answer(expr, self.clock())


def since_fix(signal="error_rate", threshold=0.05, **kw):
    return RecoveryCheck(signal=signal, service="checkout", measure="since_fix",
                         threshold=threshold, within_s=300, settle_s=90, **kw)


def counts(errors, requests):
    """Answer since-fix queries: the error query has STATUS_CODE_ERROR, the request query has not."""
    def answer(expr, now):
        return errors if signals.ERROR in expr else requests
    return answer


def run(check, answer, **kw):
    clock = Clock()
    prom = Prom(clock, answer)
    obs = check_recovery(prom, check, FIX, clock=clock, sleep=clock.sleep, **kw)
    return obs, prom, clock


# ---------------------------------------------------------------- since_fix

def test_since_fix_recovered_counts_from_settle_to_within_after_lag():
    obs, prom, clock = run(since_fix(), counts(0.0, 20.0))
    assert obs.recovered is True and obs.value == 0.0 and obs.requests == 20.0
    assert clock.t == FIX + 300 + signals.PROM_LAG_S          # waited for the range to arrive
    for expr, at, _ in prom.queries:
        assert "[210s]" in expr and at == FIX + 300            # 300 - 90 settle, evaluated at the end
    assert obs.limit == 0.05 and obs.measure == "since_fix" and obs.reason is None


def test_since_fix_not_recovered():
    obs, *_ = run(since_fix(), counts(5.0, 20.0))
    assert obs.recovered is False and obs.value == 0.25


def test_since_fix_too_little_traffic_is_not_judged():
    obs, *_ = run(since_fix(), counts(0.0, 3.0))
    assert obs.recovered is None and "too little traffic" in obs.reason


def test_since_fix_no_requests_or_nan_is_not_healthy():
    for requests in (0.0, math.nan):
        obs, *_ = run(since_fix(), counts(0.0, requests))
        assert obs.recovered is None, requests


def test_min_requests_is_configurable():
    obs, *_ = run(since_fix(), counts(0.0, 3.0), min_requests=2)
    assert obs.recovered is True


def test_error_count_compares_the_count_not_the_rate():
    obs, *_ = run(since_fix("error_count", threshold=1), counts(0.0, 30.0))
    assert obs.recovered is True and obs.value == 0.0
    obs, *_ = run(since_fix("error_count", threshold=1), counts(2.0, 30.0))
    assert obs.recovered is False and obs.value == 2.0


# ---------------------------------------------------------------- window

def window(signal="latency_p95_ms", service="frontend-proxy", **kw):
    kw.setdefault("threshold", 200.0)
    return RecoveryCheck(signal=signal, service=service, within_s=300, settle_s=90, **kw)


def series(values):
    """Answer the n-th reading with values[n] (None = no data); repeats the last one after."""
    it = {"n": 0}

    def answer(expr, now):
        v = values[min(it["n"], len(values) - 1)]
        it["n"] += 1
        return math.nan if v is None else v
    return answer


def test_window_recovers_after_two_distinct_points_and_stops_early():
    obs, prom, clock = run(window(), series([900.0, 150.0, 120.0]))
    assert obs.recovered is True and obs.value == 120.0
    assert [v for _, v in obs.points] == [900.0, 150.0, 120.0]
    assert clock.t < FIX + 300                                   # did not wait for the deadline
    assert prom.queries[0][2] == FIX + 90                        # first reading after settle_s


def test_identical_readings_within_45s_are_one_point():
    # 30 s polls: the same value at +0 and +30 s is one point; at +60 s it is a new point.
    obs, prom, clock = run(window(), series([150.0, 150.0, 150.0]))
    assert obs.recovered is True and len(prom.queries) == 3 and len(obs.points) == SUSTAINED_POINTS
    assert obs.observed_at - obs.points[0][0] >= signals.SAME_POINT_WINDOW_S


def test_one_dip_is_not_recovery():
    obs, *_ = run(window(), series([900.0, 150.0, 900.0, 800.0, 950.0, 870.0, 910.0, 880.0, 940.0]))
    assert obs.recovered is False and "2 distinct data points" in obs.reason
    assert obs.value == 880.0                                    # 8th reading: +90 s ... +300 s every 30 s


def test_window_reads_until_the_deadline_then_stops():
    obs, prom, clock = run(window(), series([900.0, 910.0]))
    assert obs.recovered is False and clock.t == FIX + 300
    assert prom.queries[-1][2] == FIX + 300                       # a final reading at the deadline


def test_window_started_after_its_deadline_is_not_judged():
    clock = Clock(FIX + 400)                                       # runner got here late
    prom = Prom(clock, series([100.0]))
    obs = check_recovery(prom, window(), FIX, clock=clock, sleep=clock.sleep)
    assert obs.recovered is None and "after the recovery window" in obs.reason and prom.queries == []


def test_since_fix_can_be_judged_afterwards():
    clock = Clock(FIX + 1000)
    obs = check_recovery(Prom(clock, counts(0.0, 30.0)), since_fix(), FIX, clock=clock, sleep=clock.sleep)
    assert obs.recovered is True and clock.sleeps == []


def test_no_data_after_fix_is_not_healthy():
    obs, *_ = run(window(), series([None]))
    assert obs.recovered is None and "no data" in obs.reason and obs.points == []


def test_gaps_in_data_do_not_break_the_run():
    obs, *_ = run(window(), series([None, 150.0, None, 140.0]))
    assert obs.recovered is True


def test_first_reading_waits_for_prometheus_lag_even_with_no_settle():
    check = RecoveryCheck(signal="memory_mb", service="email", threshold=80.0, within_s=300, settle_s=0)
    _, prom, _ = run(check, series([50.0, 49.0]))
    assert prom.queries[0][2] == FIX + signals.PROM_LAG_S


def test_baseline_form_uses_factor_times_baseline():
    check = window(threshold=None, baseline_factor=3)
    obs, *_ = run(check, series([100.0, 110.0]), baseline=50.0)
    assert obs.limit == 150.0 and obs.recovered is True and obs.baseline == 50.0
    obs, *_ = run(check, series([200.0]), baseline=50.0)
    assert obs.recovered is False


@pytest.mark.parametrize("baseline,word", [(None, "no baseline"), (0.0, "cannot be judged"), (-1.0, "cannot be judged")])
def test_missing_or_zero_baseline_is_not_judged(baseline, word):
    obs, prom, clock = run(window(threshold=None, baseline_factor=3), series([1.0]), baseline=baseline)
    assert obs.recovered is None and word in obs.reason
    assert prom.queries == [] and clock.t == FIX                   # gave up at once, no waiting


def test_higher_is_better_signal_uses_gt():
    check = RecoveryCheck(signal="throughput_rps", service="frontend", op="gt", baseline_factor=0.5,
                          within_s=300, settle_s=90)
    obs, *_ = run(check, series([4.0, 7.0, 8.0]), baseline=12.0)
    assert obs.limit == 6.0 and obs.recovered is True


def test_case_refuses_wrong_direction():
    with pytest.raises(ValidationError, match="op must be 'gt'"):
        RecoveryCheck(signal="throughput_rps", service="frontend", baseline_factor=0.5)
    with pytest.raises(ValidationError, match="op must be 'lt'"):
        RecoveryCheck(signal="latency_p95_ms", service="frontend", op="gt", threshold=100)


# ---------------------------------------------------------------- fail closed

def test_service_without_data_for_the_signal_is_not_judged():
    check = RecoveryCheck(signal="error_rate", service="recommendation", threshold=0.05)   # no span metrics
    obs, prom, _ = run(check, series([0.0]))
    assert obs.recovered is None and "no data" in obs.reason and prom.queries == []


def test_prometheus_error_is_reported_not_raised():
    def broken(expr, now):
        raise PromError("Prometheus not reachable at http://localhost:9090")
    obs, *_ = run(since_fix(), broken)
    assert obs.recovered is None and "not reachable" in obs.reason


def test_poll_must_be_positive():
    with pytest.raises(ValueError):
        run(window(), series([1.0]), poll_s=0)


def test_limit_for():
    assert limit_for(window(), None) == (200.0, None)
    assert limit_for(window(threshold=None, baseline_factor=1.3), 100.0)[0] == pytest.approx(130.0)


def test_measure_baseline_only_queries_for_baseline_form():
    clock = Clock()
    prom = Prom(clock, series([42.0]))
    assert measure_baseline(prom, window()) is None and prom.queries == []
    assert measure_baseline(prom, window(threshold=None, baseline_factor=3)) == 42.0
    assert "histogram_quantile(0.95" in prom.queries[0][0]


def test_every_dev_case_recovery_check_can_be_measured():
    dev = Path(__file__).resolve().parents[1] / "cases" / "dev"
    for case in load_cases(dev):
        rc = case.expected.recovery
        if rc is None:
            continue
        sig = "error_rate" if rc.measure == "since_fix" else rc.signal
        assert signals.supported(sig, rc.service), (case.case_id, rc.signal, rc.service)


# ---------------------------------------------------------------- through the real HTTP client

def test_since_fix_through_http_client():
    def responder(expr, at):
        m = re.search(r"\[(\d+)s\]", expr)
        assert m and int(m.group(1)) == 210 and at == FIX + 300
        return [({}, 1.0)] if signals.ERROR in expr else [({}, 40.0)]

    with FakePrometheus(responder) as fake:
        clock = Clock()
        obs = check_recovery(PromClient(fake.url), since_fix(), FIX, clock=clock, sleep=clock.sleep)
    assert obs.recovered is True and obs.value == pytest.approx(0.025) and obs.errors == 1.0
