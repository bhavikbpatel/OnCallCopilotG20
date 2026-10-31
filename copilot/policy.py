"""Decisions made by code, not the model: severity, noise, allow-list, budget.

Severity table is a placeholder until role 1 publishes the written policy.
"""
from __future__ import annotations

from copilot.state import (
    Action, BudgetLedger, BudgetLimits, FaultType, RestartService, Rollback, ScaleService, ServiceName,
    SetFlag, Severity,
)

CUSTOMER_FACING = {ServiceName.FRONTEND, ServiceName.CHECKOUT, ServiceName.PAYMENT, ServiceName.CART}

# (customer_facing, min value/threshold ratio) → severity; first match wins
SEVERITY_TABLE: list[tuple[bool, float, Severity]] = [
    (True, 5.0, Severity.SEV1),
    (True, 2.0, Severity.SEV2),
    (True, 1.0, Severity.SEV3),
    (False, 2.0, Severity.SEV3),
    (False, 0.0, Severity.SEV4),
]

FLAG_ALLOW_LIST = {f for f in FaultType if f not in (FaultType.BAD_DEPLOY, FaultType.DEPENDENCY_LATENCY, FaultType.NOISE)}
RESTART_ALLOW_LIST = {ServiceName.RECOMMENDATION, ServiceName.EMAIL, ServiceName.AD, ServiceName.CART}
SCALE_ALLOW_LIST = {ServiceName.AD, ServiceName.RECOMMENDATION, ServiceName.FRONTEND}
ROLLBACK_ALLOW_LIST = set(ServiceName)


def severity_for(service: str, value: float, threshold: float, affected: list[str]) -> Severity:
    ratio = value / threshold if threshold else 0.0
    customer_facing = any(s in CUSTOMER_FACING for s in [service, *affected])
    for needs_cf, min_ratio, sev in SEVERITY_TABLE:
        if (customer_facing or not needs_cf) and ratio >= min_ratio:
            return sev
    return Severity.SEV4


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
