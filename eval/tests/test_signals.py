"""Signal map: query construction (pure) and measurement (fake Prometheus)."""
from __future__ import annotations

import pytest

from eval.schemas.catalogue import SERVICES
from eval.telemetry import PromClient
from eval.telemetry import signals as sig
from eval.tests.fakes import FakePrometheus


@pytest.mark.parametrize("signal", sig.SIGNALS)
def test_every_signal_builds_balanced_query(signal):
    q = sig.promql(signal, "payment", "5m")
    assert q.count("(") == q.count(")") and q.count("{") == q.count("}")
    assert "payment" in q


def test_error_rate_uses_server_spans_and_zero_default():
    q = sig.promql("error_rate", "cart")
    assert q.startswith("(sum(rate(") and "or vector(0))" in q
    assert q.count('span_kind="SPAN_KIND_SERVER"') == 2


def test_error_count_counts_server_span_errors_with_zero_default():
    q = sig.promql("error_count", "cart", "2m")
    assert q.startswith("sum(increase(") and q.endswith("or vector(0)") and "STATUS_CODE_ERROR" in q
    assert sig.supported("error_count", "cart") and not sig.supported("error_count", "recommendation")


def test_latency_p50_is_median_of_server_spans():
    q = sig.promql("latency_p50_ms", "ad")
    assert q.startswith("histogram_quantile(0.5,") and 'span_kind="SPAN_KIND_SERVER"' in q


def test_slow_requests_counts_server_spans_above_one_second():
    q = sig.promql("slow_requests", "shipping", "5m")
    assert q.count('service_name="shipping",span_kind="SPAN_KIND_SERVER"') == 2
    assert 'le="+Inf"' in q and 'le=~"1000(.0)?"' in q and q.count("[5m]") == 2 and " - " in q
    assert sig.SLOW_REQUEST_MS == 1000
    assert sig.supported("slow_requests", "shipping") and not sig.supported("slow_requests", "recommendation")


def test_container_signals_use_container_name_and_units():
    assert 'container_name="recommendation"' in sig.promql("memory_mb", "recommendation")
    assert sig.promql("memory_mb", "email").endswith("/ 1e6")
    assert "container_cpu_utilization_ratio" in sig.promql("cpu_percent", "ad")


def test_consumer_lag_uses_group_and_orders_topic():
    assert sig.promql("consumer_lag", "fraud-detection") == \
        'sum(kafka_consumer_group_lag_sum_ratio{group="fraud-detection",topic="orders"})'


@pytest.mark.parametrize("bad", ["", "2", "2x", "0m", "-1m", "m", "1m", "90s", "119s"])
def test_bad_windows_rejected(bad):
    with pytest.raises(ValueError):
        sig.promql("error_rate", "cart", bad)


def test_minimum_window_is_two_minutes():
    # Metrics arrive about once a minute: rate() over 1m returned no data on the real shop.
    assert sig.MIN_WINDOW_S == 120
    assert "[120s]" in sig.promql("error_rate", "cart", "120s")
    assert "[2m]" in sig.promql("error_rate", "cart", "2m")


def test_bad_service_and_unknown_signal_rejected():
    with pytest.raises(ValueError):
        sig.promql("error_rate", 'cart"} or vector(1) #')
    with pytest.raises(ValueError):
        sig.promql("cpu_ratio", "ad")


def test_supported_matrix():
    assert not sig.supported("error_rate", "recommendation")  # no telemetry in 3.1.0
    assert sig.supported("memory_mb", "recommendation")       # container stats still work
    assert sig.supported("consumer_lag", "fraud-detection")
    assert not sig.supported("consumer_lag", "payment")
    for svc in SERVICES:
        assert sig.supported("memory_mb", svc)


def test_measure_returns_value_none_on_nan_and_skips_unsupported():
    def responder(expr, at):
        if "payment" in expr:
            return [({}, 0.25)]
        if "cart" in expr:
            return [({}, "NaN")]
        return []

    with FakePrometheus(responder) as fp:
        prom = PromClient(fp.url)
        assert sig.measure(prom, "error_rate", "payment") == 0.25
        assert sig.measure(prom, "error_rate", "cart") is None       # NaN = no traffic
        assert sig.measure(prom, "error_rate", "shipping") is None   # no series
        n = len(fp.queries)
        assert sig.measure(prom, "error_rate", "recommendation") is None
        assert len(fp.queries) == n  # unsupported: no query sent


def test_errors_since_counts_and_rate():
    def responder(expr, at):
        assert at == 2000.0 and "[300s]" in expr
        return [({}, 3.0)] if "STATUS_CODE_ERROR" in expr else [({}, 12.0)]

    with FakePrometheus(responder) as fp:
        out = sig.errors_since(PromClient(fp.url), "checkout", since=1700.0, at=2000.0)
    assert out == {"errors": 3.0, "requests": 12.0, "error_rate": 0.25}


def test_errors_since_no_requests_gives_none_rate():
    with FakePrometheus(lambda e, at: [({}, 0.0)]) as fp:
        out = sig.errors_since(PromClient(fp.url), "checkout", since=1000.0, at=1150.5)
    assert out["requests"] == 0.0 and out["error_rate"] is None
    assert "[151s]" in fp.queries[0][0]  # rounded up


def test_errors_since_rejects_ranges_under_two_minutes():
    with pytest.raises(ValueError, match="at least 120s"):
        sig.errors_since(PromClient("http://127.0.0.1:9"), "checkout", since=1000.0, at=1100.0)


def test_errors_since_rejects_reversed_times():
    with pytest.raises(ValueError):
        sig.errors_since(PromClient("http://127.0.0.1:9"), "checkout", since=2000.0, at=1000.0)
