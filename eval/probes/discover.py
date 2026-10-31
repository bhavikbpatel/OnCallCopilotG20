"""Discovery probe: learn what the running shop's telemetry actually looks like.

Read-only except for the optional noise check, which rewrites the flag
configuration WITHOUT changing any value. No fault is introduced, so any error
spike afterwards is caused by the act of writing flags itself (a possible false
clue for the agent).

Output: one JSON report under eval/results/probes/ for Person 4 to turn into the
signal -> PromQL map, recovery thresholds and case updates.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import time
from pathlib import Path
from typing import Callable, Optional

from ..injector.flagstore import REST_VARIANT, FlagStore, variants_of
from ..schemas.catalogue import SERVICES
from ..telemetry.prom import PromClient, PromError

REPORTS_DIR = Path(__file__).resolve().parents[1] / "results" / "probes"

CALLS = "traces_span_metrics_calls_total"
DURATION_BUCKET = "traces_span_metrics_duration_milliseconds_bucket"
ERROR = 'status_code="STATUS_CODE_ERROR"'
SERVER = 'span_kind="SPAN_KIND_SERVER"'
WINDOW = "2m"

# Candidate queries per signal. {svc} is replaced by the service name. Discovery
# records which ones return data; the map is chosen from the evidence.
CANDIDATES: dict = {
    # "or vector(0)": a service with no errors has no error series at all, which
    # would otherwise turn 0% into "no data". No traffic still gives "no data".
    "error_rate": {
        "server_spans": (f'(sum(rate({CALLS}{{service_name="{{svc}}",{SERVER},{ERROR}}}[{WINDOW}])) or vector(0))'
                         f' / sum(rate({CALLS}{{service_name="{{svc}}",{SERVER}}}[{WINDOW}]))'),
        "all_spans": (f'(sum(rate({CALLS}{{service_name="{{svc}}",{ERROR}}}[{WINDOW}])) or vector(0))'
                      f' / sum(rate({CALLS}{{service_name="{{svc}}"}}[{WINDOW}]))'),
    },
    "latency_p95_ms": {
        "server_spans": (f'histogram_quantile(0.95, sum by (le) (rate({DURATION_BUCKET}'
                         f'{{service_name="{{svc}}",{SERVER}}}[{WINDOW}])))'),
        "all_spans": (f'histogram_quantile(0.95, sum by (le) (rate({DURATION_BUCKET}'
                      f'{{service_name="{{svc}}"}}[{WINDOW}])))'),
    },
    "throughput_rps": {
        "server_spans": f'sum(rate({CALLS}{{service_name="{{svc}}",{SERVER}}}[{WINDOW}]))',
        "all_spans": f'sum(rate({CALLS}{{service_name="{{svc}}"}}[{WINDOW}]))',
    },
}

# Metric-name patterns for signals whose metric names we do not know yet.
NAME_PATTERNS: dict = {
    "memory": re.compile(r"(memory|heap|rss)", re.I),
    "cpu": re.compile(r"cpu", re.I),
    "kafka_lag": re.compile(r"(kafka|lag|consumer|offset)", re.I),
    "span_metrics": re.compile(r"^traces_span_metrics_"),
    "http_rpc": re.compile(r"^(http|rpc)_(server|client)_"),
    "app_specific": re.compile(r"^(demo|app)_"),
}
# Labels dropped from samples: per-process noise that does not help mapping.
NOISY_LABELS = frozenset({"__name__", "instance", "service_instance_id", "host_name", "service_version",
                          "service_namespace", "service_criticality", "telemetry_sdk_language",
                          "telemetry_sdk_name", "telemetry_sdk_version", "os_type", "deployment_environment_name"})
# Always sampled first, whatever the alphabetical order of the rest.
PRIORITY_SAMPLES = ("container_memory_usage_total_bytes", "container_cpu_utilization_ratio",
                    "kafka_consumer_group_lag_sum_ratio", "kafka_consumer_group_lag_ratio",
                    "kafka_consumer_records_lag_max", "feature_flag_evaluation_error_total")
MAX_SAMPLE_METRICS = 60

# Where errors come from at rest: lets us tell chronic background errors from faults.
BREAKDOWN_QUERIES = {
    "errors_by_operation": (f"topk(25, sum by (service_name, span_name, span_kind) "
                            f"(increase({CALLS}{{{ERROR}}}[5m])))"),
    "flag_evaluation_errors": ("topk(25, sum by (service_name, feature_flag_key, exception) "
                               "(increase(feature_flag_evaluation_error_total[5m])))"),
    "server_traffic_5m": (f"sum by (service_name) (increase({CALLS}{{{SERVER}}}[5m]))"),
}


def _finite(x: float) -> Optional[float]:
    return None if (x is None or math.isnan(x) or math.isinf(x)) else round(x, 6)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def classify_metric_names(names: list) -> dict:
    groups = {k: sorted(n for n in names if p.search(n)) for k, p in NAME_PATTERNS.items()}
    return {k: v for k, v in groups.items() if v}


def _sample_metric(prom: PromClient, name: str) -> dict:
    """Up to 25 series of one metric, trimmed to the labels useful for mapping."""
    try:
        samples = prom.query(f"topk(25, {name})")
    except PromError as exc:
        return {"error": str(exc)}
    out = []
    for s in samples:
        labels = {k: v for k, v in s.labels.items() if k not in NOISY_LABELS}
        out.append({"labels": labels, "value": _finite(s.value)})
    return {"series": out, "label_keys": sorted({k for s in samples for k in s.labels})}


def collect_labels(prom: PromClient) -> dict:
    labels = {}
    for label in ("service_name", "span_kind", "status_code", "container_name", "job"):
        try:
            labels[label] = prom.label_values(label)
        except PromError as exc:
            labels[label] = {"error": str(exc)}
    return labels


def baseline(prom: PromClient, services) -> dict:
    """Current value of every candidate query for every service (all flags at rest)."""
    result: dict = {}
    for svc in sorted(services):
        row: dict = {}
        for signal, variants in CANDIDATES.items():
            row[signal] = {}
            for variant, template in variants.items():
                try:
                    row[signal][variant] = _finite(prom.query_value(template.replace("{svc}", svc)))
                except PromError as exc:
                    row[signal][variant] = {"error": str(exc)}
        result[svc] = row
    return result


def breakdown(prom: PromClient) -> dict:
    out: dict = {}
    for key, expr in BREAKDOWN_QUERIES.items():
        try:
            rows = prom.query(expr)
        except PromError as exc:
            out[key] = {"error": str(exc)}
            continue
        out[key] = sorted(
            ({"labels": dict(r.labels), "value": _finite(r.value)} for r in rows),
            key=lambda r: -(r["value"] or 0),
        )
    return out


def candidate_coverage(base: dict) -> dict:
    """For each signal/variant: how many services returned a number."""
    cov: dict = {}
    for row in base.values():
        for signal, variants in row.items():
            for variant, value in variants.items():
                key = f"{signal}.{variant}"
                cov.setdefault(key, 0)
                if isinstance(value, (int, float)):
                    cov[key] += 1
    return cov


def freshness_s(prom: PromClient) -> Optional[float]:
    """Age of the newest span-metrics sample: how far behind 'now' Prometheus is."""
    try:
        return _finite(prom.query_value(f"time() - max(timestamp({CALLS}))"))
    except PromError:
        return None


def _errors_by_service(prom: PromClient, at: float) -> dict:
    expr = f"sum by (service_name) (increase({CALLS}{{{ERROR}}}[{WINDOW}]))"
    return {s.labels.get("service_name", "?"): _finite(s.value) for s in prom.query(expr, at)}


def noise_check(prom: PromClient, store: FlagStore, wait_s: float,
                now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Errors per service in two quiet windows vs the window right after a no-op flag write."""
    dirty = {k: v for k, v in variants_of(store.read()).items() if v != REST_VARIANT}
    if dirty:
        return {"skipped": f"flags not at rest: {dirty}"}
    t0 = now()
    quiet_earlier = _errors_by_service(prom, t0 - 120)
    quiet_before = _errors_by_service(prom, t0)
    store.write(store.read())  # same values: no fault, only a rewrite
    written_at = now()
    sleep(wait_s)
    after = _errors_by_service(prom, written_at + 120)

    rows = {}
    for svc in sorted(set(quiet_earlier) | set(quiet_before) | set(after)):
        quiet = [v for v in (quiet_earlier.get(svc), quiet_before.get(svc)) if v is not None]
        baseline_max = max(quiet) if quiet else 0.0
        post = after.get(svc) or 0.0
        rows[svc] = {"quiet_earlier": quiet_earlier.get(svc), "quiet_before": quiet_before.get(svc),
                     "after_write": after.get(svc), "excess": round(post - baseline_max, 3)}
    suspicious = sorted(s for s, r in rows.items() if r["excess"] >= 1.0)
    return {"written_at_unix": written_at, "window": WINDOW, "per_service": rows,
            "services_with_excess_errors": suspicious}


def discover(prom: PromClient, store: Optional[FlagStore] = None, wait_s: float = 180.0,
             now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
             log: Callable[[str], None] = lambda m: None) -> dict:
    report: dict = {"kind": "discovery", "generated_at": _now_iso(), "prometheus_url": prom.base_url}
    report["prometheus"] = {k: prom.build_info().get(k) for k in ("version", "revision")}
    log("listing metric names ...")
    names = prom.metric_names()
    report["metric_count"] = len(names)
    report["metric_groups"] = classify_metric_names(names)
    report["labels"] = collect_labels(prom)
    report["freshness_s"] = freshness_s(prom)

    log("sampling memory / cpu / kafka metrics ...")
    to_sample = []
    for group in ("memory", "cpu", "kafka_lag", "app_specific"):
        to_sample.extend(report["metric_groups"].get(group, []))
    available = set(report["metric_groups"].get("memory", [])) | set(to_sample) | set(names)
    priority = [n for n in PRIORITY_SAMPLES if n in available]
    rest = sorted(set(to_sample) - set(priority))
    to_sample = (priority + rest)[:MAX_SAMPLE_METRICS]
    report["samples"] = {name: _sample_metric(prom, name) for name in to_sample}

    log("breaking down errors at rest ...")
    report["breakdown"] = breakdown(prom)

    log("measuring healthy baseline per service ...")
    report["baseline"] = baseline(prom, SERVICES)
    report["candidate_coverage"] = candidate_coverage(report["baseline"])

    if store is not None:
        log(f"noise check: rewriting flags without changes, then waiting {wait_s:.0f}s ...")
        report["noise_check"] = noise_check(prom, store, wait_s, now=now, sleep=sleep)
    else:
        report["noise_check"] = {"skipped": "disabled"}
    report["finished_at"] = _now_iso()
    return report


def save(report: dict, directory: Optional[Path] = None) -> Path:
    directory = Path(directory) if directory is not None else REPORTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"discovery-{stamp}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return path


def summary_lines(report: dict) -> list:
    lines = [f"Prometheus {report['prometheus'].get('version')}: {report['metric_count']} metric names"]
    for group, names in report["metric_groups"].items():
        lines.append(f"  {group:14s} {len(names)} metrics")
    lines.append(f"  data freshness: {report.get('freshness_s')} s behind now")
    services = report["labels"].get("service_name")
    if isinstance(services, list):
        missing = sorted(set(SERVICES) - set(services))
        lines.append(f"  services in span metrics: {len(services)}; missing: {missing or 'none'}")
    lines.append("candidate coverage (services with data, of %d):" % len(SERVICES))
    for key, n in sorted(report["candidate_coverage"].items()):
        lines.append(f"  {key:30s} {n}")
    nc = report["noise_check"]
    if "skipped" in nc:
        lines.append(f"noise check skipped: {nc['skipped']}")
    else:
        found = nc["services_with_excess_errors"]
        lines.append(f"noise check: services with extra errors after a no-op flag write: {found or 'none'}")
    return lines
