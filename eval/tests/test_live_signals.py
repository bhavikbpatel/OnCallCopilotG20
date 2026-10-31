"""Live check of the signal map against the running shop. Skipped unless EVAL_LIVE=1.

Read-only. Confirms every agreed query returns real data on the pinned 3.1.0 shop.
"""
from __future__ import annotations

import os
import time

import pytest

from eval.telemetry import PromClient
from eval.telemetry import signals as sig

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("EVAL_LIVE") != "1", reason="set EVAL_LIVE=1 to run against the real shop"),
]


@pytest.fixture(scope="module")
def prom():
    return PromClient()


@pytest.mark.parametrize("service", ["frontend", "frontend-proxy", "product-catalog", "cart"])
def test_live_busy_services_have_error_rate_latency_and_traffic(prom, service):
    assert sig.measure(prom, "error_rate", service, "5m") is not None
    assert sig.measure(prom, "latency_p95_ms", service, "5m") is not None
    assert sig.measure(prom, "throughput_rps", service, "5m") > 0


def test_live_server_error_rate_is_near_zero_at_rest(prom):
    # Valid only with every flag at rest (the injector preflight checks this elsewhere).
    for service in ("frontend", "cart", "product-catalog", "currency"):
        assert sig.measure(prom, "error_rate", service, "5m") < 0.02, service


@pytest.mark.parametrize("service", ["recommendation", "email", "ad", "payment"])
def test_live_container_memory_and_cpu(prom, service):
    assert sig.measure(prom, "memory_mb", service) > 1
    assert sig.measure(prom, "cpu_percent", service) >= 0


def test_live_kafka_lag_exists_for_fraud_detection(prom):
    assert sig.measure(prom, "consumer_lag", "fraud-detection") is not None


def test_live_errors_since_last_ten_minutes_for_checkout(prom):
    now = time.time()
    out = sig.errors_since(prom, "checkout", since=now - 600, at=now)
    assert out["requests"] is not None and out["requests"] > 0, "no checkouts in 10 min: is load running?"
