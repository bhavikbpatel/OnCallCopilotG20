"""The agreed signal -> PromQL map for Astronomy Shop 3.1.0.

Chosen from the discovery probe (31 Oct 2026), see docs/shop-telemetry-findings.md:
- Error rate and latency use the service's own SERVER spans: 0% errors at rest
  for every service except flagd. All-span rates include chronic flag-lookup
  noise and would never look "recovered".
- Memory and CPU come from Docker container stats, so they work even for
  services that send no telemetry of their own (recommendation in 3.1.0).
- CPU is reported in PERCENT (e.g. 91.6), not 0-1.
- Kafka lag is per consumer group on the "orders" topic.

All functions return None when there is no data (no traffic, unknown service),
never 0, so "no data" cannot be mistaken for "healthy".
"""
from __future__ import annotations

import math
from typing import Optional

from ..schemas.catalogue import SERVICES
from .prom import PromClient

CALLS = "traces_span_metrics_calls_total"
DURATION_BUCKET = "traces_span_metrics_duration_milliseconds_bucket"
SERVER = 'span_kind="SPAN_KIND_SERVER"'
ERROR = 'status_code="STATUS_CODE_ERROR"'
KAFKA_TOPIC = "orders"

# Services that consume Kafka (lag is measured per consumer group).
KAFKA_CONSUMERS = frozenset({"fraud-detection", "accounting"})
# Instrumented services whose server spans reach Prometheus in 3.1.0.
# (recommendation sends nothing; databases and kafka have no spans.)
NO_SPAN_METRICS = frozenset({"recommendation", "astronomy-db", "valkey-cart", "kafka"})

# Span metrics reach Prometheus about once a minute; rate()/increase() need two
# samples inside the window, so shorter windows return "no data" (found 31 Oct).
MIN_WINDOW_S = 120
# Prometheus gets a new span-metric point about once a minute. Two identical readings
# closer together than this are the same point read twice, not two observations.
SAME_POINT_WINDOW_S = 45.0
# Data reaches Prometheus 12-40 s behind real time (docs/shop-telemetry-findings.md §5).
PROM_LAG_S = 40

SIGNALS = ("error_rate", "error_count", "latency_p95_ms", "latency_p50_ms", "slow_requests", "throughput_rps",
           "memory_mb", "cpu_percent", "consumer_lag")
# slow_requests: server requests slower than this in the window. 1000 ms is an edge of the span-metrics
# connector's default histogram (2, 4, 6, 8, 10, 50, 100, 200, 400, 800, 1000, 1400, 2000, 5000, 10000,
# 15000 ms), which the shop's collector uses (src/otel-collector/otelcol-config.yml: span_metrics with no
# buckets set). Prometheus 3 may store the edge as "1000" or "1000.0", so both are matched.
SLOW_REQUEST_MS = 1000
_SLOW_LE = 'le=~"1000(.0)?"'

LESSON_SIGNALS = frozenset({"error_rate", "latency_p95_ms"})


def lesson_signal_name(service: str, signal: str) -> str:
    """Validate a service-qualified signal predicate allowed in durable lessons."""
    if service not in SERVICES:
        raise ValueError(f"unsupported or unverified service {service!r}")
    if signal not in LESSON_SIGNALS or not supported(signal, service):
        raise ValueError(f"unsupported or unverified lesson signal {signal!r} for {service!r}")
    promql(signal, service)
    return f"{service}.{signal}"


def window_seconds(window: str) -> int:
    if not window or window[-1] not in "smh" or not window[:-1].isdigit() or int(window[:-1]) <= 0:
        raise ValueError(f"window must look like '2m', '300s' or '1h', got {window!r}")
    return int(window[:-1]) * {"s": 1, "m": 60, "h": 3600}[window[-1]]


def _check_window(window: str) -> str:
    if window_seconds(window) < MIN_WINDOW_S:
        raise ValueError(f"window {window!r} is shorter than {MIN_WINDOW_S}s: metrics arrive about once a "
                         f"minute, so rate() would return no data")
    return window


def _svc(service: str) -> str:
    if not service or '"' in service or "\\" in service:
        raise ValueError(f"bad service name {service!r}")
    return service


def promql(signal: str, service: str, window: str = "2m") -> str:
    """The PromQL expression for one signal of one service."""
    s, w = _svc(service), _check_window(window)
    if signal == "error_rate":
        return (f'(sum(rate({CALLS}{{service_name="{s}",{SERVER},{ERROR}}}[{w}])) or vector(0))'
                f' / sum(rate({CALLS}{{service_name="{s}",{SERVER}}}[{w}]))')
    if signal == "error_count":
        # Errors in the window. For low-volume operations (cartFailure breaks only
        # EmptyCart, ~2-3 calls/min) a rate stays tiny while the count is clear.
        return f'sum(increase({CALLS}{{service_name="{s}",{SERVER},{ERROR}}}[{w}])) or vector(0)'
    if signal == "latency_p95_ms":
        return (f'histogram_quantile(0.95, sum by (le) (rate({DURATION_BUCKET}'
                f'{{service_name="{s}",{SERVER}}}[{w}])))')
    if signal == "latency_p50_ms":
        # Median: CPU saturation slows every request, while random noise mostly
        # hits the slowest few (ad p95 jumped 10x in a control run, 31 Oct).
        return (f'histogram_quantile(0.5, sum by (le) (rate({DURATION_BUCKET}'
                f'{{service_name="{s}",{SERVER}}}[{w}])))')
    if signal == "slow_requests":
        # Faults that slow only SOME requests (intlShippingSlowdown: international orders only, ~0.2 req/s)
        # vanish in p95 once they are under 5% of requests; a count shows every slow request, and a wider
        # window collects more of them instead of diluting them. All requests minus those <= 1000 ms.
        sel = f'service_name="{s}",{SERVER}'
        return (f'sum(increase({DURATION_BUCKET}{{{sel},le="+Inf"}}[{w}]))'
                f' - sum(increase({DURATION_BUCKET}{{{sel},{_SLOW_LE}}}[{w}]))')
    if signal == "throughput_rps":
        return f'sum(rate({CALLS}{{service_name="{s}",{SERVER}}}[{w}]))'
    if signal == "memory_mb":
        return f'sum(container_memory_usage_total_bytes{{container_name="{s}"}}) / 1e6'
    if signal == "cpu_percent":
        return f'sum(container_cpu_utilization_ratio{{container_name="{s}"}})'
    if signal == "consumer_lag":
        return f'sum(kafka_consumer_group_lag_sum_ratio{{group="{s}",topic="{KAFKA_TOPIC}"}})'
    raise ValueError(f"unknown signal {signal!r}; choose from {SIGNALS}")


def supported(signal: str, service: str) -> bool:
    """Whether this signal can have data for this service in 3.1.0."""
    if signal in ("error_rate", "error_count", "latency_p95_ms", "latency_p50_ms", "slow_requests", "throughput_rps"):
        return service not in NO_SPAN_METRICS
    if signal == "consumer_lag":
        return service in KAFKA_CONSUMERS
    return signal in SIGNALS


def clean(x: float) -> Optional[float]:
    """A Prometheus value as a number, or None for NaN / Inf (0/0 with no traffic). No data is never 0."""
    return None if (x is None or math.isnan(x) or math.isinf(x)) else x


_clean = clean  # kept for callers that used the private name (Person 2's copilot/tools.py)


def measure(prom: PromClient, signal: str, service: str, window: str = "2m",
            at: Optional[float] = None) -> Optional[float]:
    """Current value of a signal, or None if there is no data."""
    if not supported(signal, service):
        return None
    return _clean(prom.query_value(promql(signal, service, window), at))


def _range(since: float, at: float) -> str:
    seconds = int(math.ceil(at - since))
    if seconds <= 0:
        raise ValueError("'since' must be before 'at'")
    if seconds < MIN_WINDOW_S:
        raise ValueError(f"need at least {MIN_WINDOW_S}s between 'since' and 'at' (metrics arrive about "
                         f"once a minute); got {seconds}s")
    return f"{seconds}s"


def errors_since(prom: PromClient, service: str, since: float, at: float) -> dict:
    """Server-span errors and requests between ``since`` and ``at`` (unix seconds).

    Used for recovery: low-traffic services (checkout: ~2.5 calls/min) give too
    few requests in a fixed 2-minute window, so we count everything since the
    fix was applied instead. Returns {"errors", "requests", "error_rate"}; the
    rate is None when there were no requests.
    """
    s, r = _svc(service), _range(since, at)
    if not supported("error_rate", s):
        return {"errors": None, "requests": None, "error_rate": None}
    err = _clean(prom.query_value(
        f'sum(increase({CALLS}{{service_name="{s}",{SERVER},{ERROR}}}[{r}])) or vector(0)', at))
    req = _clean(prom.query_value(f'sum(increase({CALLS}{{service_name="{s}",{SERVER}}}[{r}]))', at))
    rate = (err / req) if (err is not None and req) else None
    return {"errors": err, "requests": req, "error_rate": rate}
