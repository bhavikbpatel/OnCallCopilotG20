"""Deterministic severity classification; thresholds are intentionally explicit."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Severity:
    level: str
    rule: str
    rationale: str


def classify(*, user_impact: str, error_rate: Optional[float],
             duration_minutes: Optional[float], evidence_count: int,
             critical_path: bool, latency_p95_ms: Optional[float] = None,
             consumer_lag: Optional[float] = None,
             sample_count: Optional[int] = None,
             fault_confirmed: bool = False) -> Severity:
    """Classify from observed scope and telemetry; unknowns never imply recovery."""
    if user_impact not in {"broad", "partial", "localized", "none", "unknown"}:
        raise ValueError(f"unsupported user impact {user_impact!r}")
    if evidence_count < 0:
        raise ValueError("evidence_count cannot be negative")
    if sample_count is not None and (
        isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 0
    ):
        raise ValueError("sample_count must be a non-negative integer")
    if user_impact == "unknown":
        return Severity("SEV-3", "S3.0", "user impact is unknown; gather more evidence")
    if (error_rate is not None and error_rate < 0) or (
        duration_minutes is not None and duration_minutes < 0
    ) or (latency_p95_ms is not None and latency_p95_ms < 0) or (
        consumer_lag is not None and consumer_lag < 0
    ):
        raise ValueError("telemetry measurements cannot be negative")
    duration = duration_minutes if duration_minutes is not None else 0
    if user_impact == "none" and evidence_count >= 2 and not fault_confirmed:
        return Severity("NONE", "S0.1", "no user impact supported by telemetry")
    if evidence_count < 2:
        return Severity("SEV-3", "S3.0", "insufficient independent evidence; investigate, do not remediate")
    high_error_rate = (
        critical_path and user_impact == "broad"
        and error_rate is not None and error_rate >= 0.10
    )
    sample_insufficient_for_high_rate = high_error_rate and (
        sample_count is None or sample_count < 20
    )
    if ((user_impact == "broad" and duration >= 5 and error_rate is not None and error_rate >= 0.05
         and not sample_insufficient_for_high_rate)
            or (high_error_rate and sample_count is not None and sample_count >= 20)
            or (critical_path and consumer_lag is not None and consumer_lag >= 1000 and duration >= 10)):
        return Severity("SEV-1", "S1.1", "broad critical-path impact with sustained high error or backlog")
    if (user_impact in {"broad", "partial"} and duration >= 5
            and ((not high_error_rate and error_rate is not None and error_rate >= 0.01)
                 or (critical_path and latency_p95_ms is not None and latency_p95_ms >= 500))):
        return Severity("SEV-2", "S2.1", "sustained material user impact or critical-path latency")
    if sample_insufficient_for_high_rate:
        return Severity("SEV-3", "S3.0", "fewer than 20 requests support the 10% error-rate threshold")
    if error_rate is None and latency_p95_ms is None and consumer_lag is None:
        return Severity("SEV-3", "S3.0", "impact telemetry is missing; magnitude is unknown")
    if user_impact == "none" and fault_confirmed:
        return Severity("SEV-3", "S3.1", "a fault is confirmed despite no measured user impact")
    return Severity("SEV-3", "S3.1", "localized, short-lived, or below SEV-2 impact threshold")
