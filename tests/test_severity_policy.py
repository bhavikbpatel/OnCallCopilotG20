"""Triage severity follows Person 1's policy (eval/knowledge/severity.py) and reproduces every dev case's expected
level and rule from the telemetry triage would see. Readings are representative of the live shop
(docs/shop-telemetry-findings.md); the mapping in copilot.policy.IMPACT_SCOPE is a proposal for Person 1."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from copilot.clients import RecoveryCount, ServiceHealth
from copilot.policy import IMPACT_SCOPE, classify_severity
from copilot.state import Alert, Severity
from eval.runner.alerts import signal_for
from eval.schemas.catalogue import SERVICES

DEV = Path(__file__).resolve().parents[1] / "eval" / "cases" / "dev"
FLAG = [{"kind": "flag", "flag": "someFault", "variant": "on"}]


def alert(service: str, signal: str, threshold: float, value: float) -> Alert:
    return Alert(alert_name="A", service=service, signal=signal, summary="", query="q", threshold=threshold,
                 value=value, fired_at=datetime.now(timezone.utc), fingerprint="f")


def sample(requests: float, rate: float) -> RecoveryCount:
    return RecoveryCount(errors=requests * rate, requests=requests, error_rate=rate)


# What triage would measure for each dev case's alert: (current, threshold, error rate 5m, p95 ms, flag on,
# 10-minute sample, noise). Checkout traffic is low (~2 requests a minute), so its 10-minute sample is ~20-30.
READINGS = {
    "CheckoutErrorRateHigh": (0.17, 0.05, 0.17, 300.0, True, sample(25, 0.12), False),
    "CartErrorRateHigh": (4.0, 1.0, 0.015, 5.0, True, sample(400, 0.015), False),
    "ProductCatalogErrorRateHigh": (0.06, 0.01, 0.06, 4.0, True, sample(900, 0.05), False),
    "FrontendLatencyHigh": (5200.0, 1000.0, 0.0, 5200.0, True, None, False),
    "ProductCatalogLatencyHigh": (900.0, 100.0, 0.0, 900.0, True, sample(900, 0.0), False),
    "KafkaConsumerLagHigh": (4000.0, 100.0, 0.0, 3.0, True, None, False),
    "AdErrorRateHigh": (0.5, 0.05, 0.5, 4.0, True, sample(300, 0.45), False),
    "EmailMemoryHigh": (620.0, 300.0, 0.0, 6.0, True, sample(40, 0.0), False),
    "FrontendTrafficHigh": (20.0, 24.0, 0.0, 40.0, False, sample(9000, 0.0), True),
}


def dev_cases() -> list:
    cases = [yaml.safe_load(p.read_text()) for p in sorted(DEV.glob("D-*.yaml"))]
    return [pytest.param(c, id=c["case_id"]) for c in cases]


@pytest.mark.parametrize("case", dev_cases())
def test_triage_severity_matches_every_dev_case(case):
    name, service = case["alert"]["name"], case["alert"]["service"]
    current, thr, err5, p95, flag_on, smp, noise = READINGS[name]
    health = [ServiceHealth(service=service, error_rate=err5, latency_p95_ms=p95),
              ServiceHealth(service="ad", error_rate=0.0, latency_p95_ms=3.0)]
    level, rule, reason = classify_severity(alert(service, signal_for(name), thr, current), current, health,
                                            FLAG if flag_on else [], smp, noise)
    want = case["expected"]["severity"]
    got_level = "NONE" if case["expected"]["verdict"] == "noise" else level.value     # noise is reported as NONE
    assert (got_level, rule) == (want["level"], want["rule"]), reason


def test_broad_critical_error_rate_needs_twenty_requests_in_ten_minutes():
    a = alert("checkout", "error_rate", 0.05, 0.17)
    health = [ServiceHealth(service="checkout", error_rate=0.17, latency_p95_ms=300.0)]
    level, rule, _ = classify_severity(a, 0.17, health, FLAG, sample(12, 0.15), noise=False)
    assert (level, rule) == (Severity.SEV3, "S3.0")                  # too few requests to call it SEV1
    level, rule, _ = classify_severity(a, 0.17, health, FLAG, None, noise=False)
    assert (level, rule) == (Severity.SEV3, "S3.0")                  # no sample at all: unknown, not SEV1


def test_one_evidence_item_is_not_enough():
    a = alert("ad", "error_rate", 0.05, 0.5)
    level, rule, _ = classify_severity(a, 0.5, [], [], None, noise=False)   # breaching only
    assert (level, rule) == (Severity.SEV3, "S3.0")


def test_missing_or_bad_readings_are_unknown_never_zero():
    a = alert("checkout", "error_rate", 0.05, 0.17)
    nan = float("nan")
    health = [ServiceHealth(service="checkout", error_rate=nan, latency_p95_ms=None)]
    level, rule, _ = classify_severity(a, nan, health, FLAG, RecoveryCount(errors=None, requests=None,
                                                                            error_rate=None), noise=False)
    assert (level, rule) == (Severity.SEV3, "S3.0")
    level, rule, _ = classify_severity(a, None, [], [], None, noise=True)    # noise without healthy readings
    assert rule == "S3.0"


def test_flag_turned_off_is_not_an_active_fault():
    a = alert("email", "memory_mb", 300.0, 620.0)
    health = [ServiceHealth(service="email", error_rate=0.0, latency_p95_ms=6.0)]
    off = [{"kind": "flag", "flag": "emailMemoryLeak", "variant": "off"}]
    level, rule, _ = classify_severity(a, 620.0, health, off, None, noise=False)
    assert (level, rule) == (Severity.SEV3, "S3.0")                  # breaching only: one evidence item


def test_unknown_service_is_unknown_impact():
    a = alert("not-a-service", "error_rate", 0.05, 0.5)
    level, rule, _ = classify_severity(a, 0.5, [], FLAG, None, noise=False)
    assert (level, rule) == (Severity.SEV3, "S3.0")


def test_every_shop_service_has_a_scope():
    assert set(IMPACT_SCOPE) == set(SERVICES)
    assert {scope for scope, _ in IMPACT_SCOPE.values()} <= {"broad", "partial", "localized"}
