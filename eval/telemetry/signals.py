"""Supported, service-scoped Prometheus signal names for lesson preconditions.

These queries use the span-metric names and labels discovered by
``eval.probes.discover``. Database lock wait is intentionally not exposed:
the pinned shop does not currently export a confirmed astronomy-db wait metric.
"""
from __future__ import annotations

from ..schemas.catalogue import SERVICES

CALLS = "traces_span_metrics_calls_total"
DURATION_BUCKET = "traces_span_metrics_duration_milliseconds_bucket"
ERROR = 'status_code="STATUS_CODE_ERROR"'
SERVER = 'span_kind="SPAN_KIND_SERVER"'
WINDOW = "2m"
SUPPORTED_SIGNALS = frozenset({"error_rate", "latency_p95_ms"})


def signal_query(service: str, signal: str) -> str:
    """Return a known service-scoped PromQL expression or reject unknown signals."""
    if service not in SERVICES:
        raise ValueError(f"unknown service {service!r}")
    if signal not in SUPPORTED_SIGNALS:
        raise ValueError(f"unsupported or unverified telemetry signal {signal!r}")
    if signal == "error_rate":
        return (
            f'(sum(rate({CALLS}{{service_name="{service}",{SERVER},{ERROR}}}[{WINDOW}])) or vector(0))'
            f' / sum(rate({CALLS}{{service_name="{service}",{SERVER}}}[{WINDOW}]))'
        )
    return (
        f'histogram_quantile(0.95, sum by (le) (rate({DURATION_BUCKET}'
        f'{{service_name="{service}",{SERVER}}}[{WINDOW}])))'
    )


def lesson_signal_name(service: str, signal: str) -> str:
    """Canonical key for a result sampled from the supported query."""
    signal_query(service, signal)
    return f"{service}.{signal}"
