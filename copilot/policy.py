"""Decisions made by code, not the model: severity, noise, allow-list, budget.

Severity follows Person 1's policy (docs/severity-policy.md), computed by its classifier
(eval/knowledge/severity.py ``classify``) from what triage measures; see ``classify_severity``.
"""
from __future__ import annotations

import math
from typing import Any

from copilot.clients import RecoveryCount, ServiceHealth
from copilot.state import (
    Action, Alert, BudgetLedger, BudgetLimits, FlagName, RestartService, Rollback, ScaleService, ServiceName,
    SetFlag, Severity,
)
from eval.knowledge.severity import classify
from eval.schemas import catalogue

# The policy's scope of user impact and critical path, per alerting service. PROPOSED (Person 4), for Person 1 to
# confirm: "broad" = core browse/checkout path; "partial" = a material route; "localized" = a non-critical feature.
IMPACT_SCOPE: dict[str, tuple[str, bool]] = {
    "frontend": ("broad", True), "frontend-proxy": ("broad", True),
    "checkout": ("broad", True), "payment": ("broad", True),
    "cart": ("partial", True), "valkey-cart": ("partial", True),
    "product-catalog": ("partial", True), "astronomy-db": ("partial", True),
    "currency": ("partial", True), "shipping": ("partial", True), "quote": ("partial", True),
    "image-provider": ("partial", False),
    "recommendation": ("localized", False), "ad": ("localized", False), "email": ("localized", False),
    "fraud-detection": ("localized", False), "accounting": ("localized", False), "kafka": ("localized", False),
    "flagd": ("localized", False), "load-generator": ("localized", False),
}
IMPACT_ERROR_RATE = 0.01      # the policy's lowest impact thresholds (SEV2): error rate 1%, p95 500 ms
IMPACT_LATENCY_MS = 500.0
SEVERITY_WINDOW_MIN = 10      # the policy's rolling sample window for a broad critical-path error rate

# Only fault flags may be toggled; load / red-team / agentic flags are off limits.
FLAG_ALLOW_LIST = {FlagName(f) for f in catalogue.FAULT_TYPES}
RESTART_ALLOW_LIST = {ServiceName.RECOMMENDATION, ServiceName.EMAIL, ServiceName.AD, ServiceName.CART}
SCALE_ALLOW_LIST = {ServiceName.AD, ServiceName.RECOMMENDATION, ServiceName.FRONTEND}
ROLLBACK_ALLOW_LIST = set(ServiceName)


def _num(v: Any) -> float | None:
    """A usable, non-negative reading, or None (missing, NaN or negative is unknown, never zero)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f >= 0 else None


def classify_severity(alert: Alert, current: float | None, health: list[ServiceHealth],
                      changes: list[dict[str, Any]], sample: RecoveryCount | None,
                      noise: bool) -> tuple[Severity, str, str]:
    """Severity, Person 1's rule ID and the reason, from what triage measured.

    Inputs to ``classify`` (independent evidence counted at triage, before diagnosis):
    - user impact / critical path: IMPACT_SCOPE for the alerting service; "none" when triage found noise.
    - evidence items: the alert still breaching, the service's own error rate or p95 past the policy's impact
      thresholds, an active flag change (for noise: the alert back below its threshold, the service healthy).
    - error rate and sample: errors over requests in the last 10 minutes (``sample``), else the 5-minute rate.
    - fault active: a flag is on; impact sustained: the alert is still breaching (it fired on 2 readings).
    NONE (S0.1) is stored as SEV4: the state has no NONE level, and noise is reported as NONE anyway.
    """
    mine = next((h for h in health if h.service == alert.service), None)
    err5 = _num(mine.error_rate) if mine else None
    p95 = _num(mine.latency_p95_ms) if mine else None
    cur = _num(current)
    breaching = cur is not None and cur >= alert.threshold
    impact = (err5 or 0.0) >= IMPACT_ERROR_RATE or (p95 or 0.0) >= IMPACT_LATENCY_MS
    measured = mine is not None and (err5 is not None or p95 is not None)
    active = [c for c in changes if c.get("kind", "flag") == "flag" and str(c.get("variant", "")).lower() not in ("", "off")]
    scope, critical = IMPACT_SCOPE.get(alert.service, ("unknown", False))
    if noise:
        scope = "none"
        evidence = int(cur is not None and not breaching) + int(measured and not impact)
    else:
        evidence = int(breaching) + int(impact) + int(bool(active))
    requests = _num(sample.requests) if sample else None
    rate = _num(sample.error_rate) if sample and requests else None
    error_rate = rate if rate is not None else err5
    try:
        s = classify(user_impact=scope, error_rate=error_rate, duration_minutes=None, evidence_count=evidence,
                     critical_path=critical, latency_p95_ms=p95,
                     consumer_lag=cur if alert.signal == "consumer_lag" else None,
                     sample_count=int(round(requests)) if rate is not None else None,
                     sample_window_minutes=SEVERITY_WINDOW_MIN if rate is not None else None,
                     fault_confirmed=False, fault_active=bool(active), impact_sustained=breaching)
    except ValueError as exc:                                   # bad input: investigate, never guess high or low
        return Severity.SEV3, "S3.0", f"severity inputs rejected: {exc}"
    level = Severity.SEV4 if s.level == "NONE" else Severity(s.level)
    return level, s.rule, s.rationale


def is_noise(current_value: float | None, threshold: float, recent_change_count: int) -> bool:
    """Alert no longer breaching and nothing changed recently → noise; close with no action."""
    return current_value is not None and current_value < threshold and recent_change_count == 0


def is_allowed(action: Action) -> tuple[bool, str]:
    if isinstance(action, SetFlag):
        return action.flag in FLAG_ALLOW_LIST, f"flag {action.flag} not allow-listed"
    if isinstance(action, RestartService):
        return action.service in RESTART_ALLOW_LIST, f"restart of {action.service} not allow-listed"
    if isinstance(action, ScaleService):
        return action.service in SCALE_ALLOW_LIST, f"scaling {action.service} not allow-listed"
    if isinstance(action, Rollback):
        return action.service in ROLLBACK_ALLOW_LIST, f"rollback of {action.service} not allow-listed"
    return False, f"unknown action {type(action).__name__}"


def budget_exhausted(spent: BudgetLedger | None, limits: BudgetLimits) -> bool:
    total = (spent or BudgetLedger()).total
    return total.tool_calls >= limits.tool_calls or total.tokens >= limits.tokens or total.usd >= limits.usd
