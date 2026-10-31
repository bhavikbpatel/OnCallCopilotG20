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
             consumer_lag: Optional[float] = None) -> Severity:
    """Classify from observed scope and telemetry; unknowns never imply recovery."""
    if user_impact not in {"broad", "partial", "localized", "none", "unknown"}:
        raise ValueError(f"unsupported user impact {user_impact!r}")
    if evidence_count < 0:
        raise ValueError("evidence_count cannot be negative")
    duration = duration_minutes or 0
    errors = error_rate or 0
    if user_impact == "none" and evidence_count >= 2:
        return Severity("NONE", "S0.1", "no user impact supported by telemetry")
    if evidence_count < 2:
        return Severity("SEV3", "S3.0", "insufficient independent evidence; investigate, do not remediate")
    if ((user_impact == "broad" and duration >= 5 and errors >= 0.05)
            or (critical_path and user_impact == "broad" and errors >= 0.10)
            or (critical_path and consumer_lag is not None and consumer_lag >= 1000 and duration >= 10)):
        return Severity("SEV1", "S1.1", "broad critical-path impact with sustained high error or backlog")
    if (user_impact in {"broad", "partial"} and duration >= 5
            and (errors >= 0.01 or (latency_p95_ms is not None and latency_p95_ms >= 500))):
        return Severity("SEV2", "S2.1", "sustained material user impact or critical-path latency")
    return Severity("SEV3", "S3.1", "localized, short-lived, or below SEV2 impact threshold")
