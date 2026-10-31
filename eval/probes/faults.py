"""Fault probe: measure what each fault actually does to the shop's signals.

For each item (a control run, the 12 faults, the traffic flood):
  1. baseline: two samples of every signal for every service
  2. inject at a representative intensity and hold, sampling regularly
  3. clear the flag and keep sampling, to see whether the shop recovers by
     itself (no restart): this answers the "does a leak need a restart" question
The control item does a no-op flag write and is measured the same way, so the
report shows how much signals move when nothing is wrong.

The report is saved after every item; flags are always cleared on exit.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
import time
from pathlib import Path
from typing import Callable, Optional

from ..injector.flagstore import REST_VARIANT, FlagStore, apply, variants_of
from ..schemas.catalogue import FAULT_TYPES, LOAD_FLAG, SERVICES, accepted_services
from ..telemetry import signals as sig
from ..telemetry.prom import PromClient, PromError

REPORTS_DIR = Path(__file__).resolve().parents[1] / "results" / "probes"

CONTROL = "control"
FLOOD = "flood"

# Representative intensity per fault: strong enough to see, not the extreme.
PROBE_VARIANTS: dict = {
    "paymentFailure": "50%",
    "cartFailure": "50%",
    "productCatalogFailure": "on",
    "recommendationCacheFailure": "on",
    "kafkaQueueProblems": "on",
    "adHighCpu": "on",
    "emailMemoryLeak": "1000x",
    "imageSlowLoad": "5sec",
    "paymentUnreachable": "on",
    "productCatalogLockContention": "on",
    "intlShippingSlowdown": "5sec",
    "adFailure": "on",
}
ITEMS = (CONTROL,) + tuple(PROBE_VARIANTS) + (FLOOD,)

# A signal "moved" if it changed by at least this much from baseline.
# (absolute minimum, relative minimum) - both must be exceeded.
MOVE_THRESHOLDS: dict = {
    "error_rate": (0.05, 0.0),          # +5 percentage points
    "error_count": (2.0, 0.0),          # +2 errors in the window (only on services quiet at rest)
    "latency_p95_ms": (50.0, 5.0),      # +50 ms and 6x (control run: frontend p95 swings ~4x on its own)
    "latency_p50_ms": (20.0, 2.0),      # +20 ms and 3x (median is far steadier than p95)
    "throughput_rps": (0.2, 1.5),       # rise to 2.5x or more (see THROUGHPUT_DROP for falls)
    "memory_mb": (20.0, 0.2),           # +20 MB and +20%
    "cpu_percent": (20.0, 0.5),         # +20 points and +50%
    "consumer_lag": (50.0, 0.0),        # +50 messages
}


class ProbeError(ValueError):
    pass


# Locust traffic swings 50-80% over minutes on its own (measured in the control
# run, 31 Oct), so only a fall of 70%+ counts as a traffic drop.
THROUGHPUT_DROP = 0.7
# Below this many requests/s (checkout, payment, email: ~0.02-0.07/s) random gaps
# look like 75%+ "drops"; traffic drops are only judged on busier services.
THROUGHPUT_MIN_BASE_RPS = 0.5
# A signal must stay moved in this many DIFFERENT data points. Prometheus gets a
# new span-metric point only about once a minute and the probe samples every 30 s,
# so consecutive identical readings are the same point and count once. Without
# this, one random spike (ad p95 1,140 ms in a control run) looked "sustained".
SUSTAINED_SAMPLES = 2
# Effects reach Prometheus late (2-minute window + ~60 s delivery): in the full
# probe they first showed ~90-130 s after injection and lasted into the first
# minutes after the clear. Samples up to this long after the clear still count
# as "during the fault"; recovery is judged only on later samples.
EFFECT_LAG_S = 120
# p95 latency is meaningless with a handful of requests (checkout: 1-4 per window).
LATENCY_MIN_RPS = 0.1
# Quiet services (0.1-1 req/s, e.g. ad, shipping) have noisy p95 too: the control
# run saw ad go 6.4x with nothing wrong. They need a 10x rise to count.
LATENCY_LOW_TRAFFIC_RPS = 1.0
LATENCY_LOW_TRAFFIC_FACTOR = 10.0
# error_count only means something for services with (almost) no errors at rest.
ERROR_COUNT_MAX_BASE = 1.0
# Not a service under test: the traffic source's own memory/CPU drift is noise.
EXCLUDED_SERVICES = frozenset({"load-generator"})


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sample_all(prom: PromClient, window: str = "2m", services=SERVICES) -> dict:
    """One value (or None) per supported signal per service."""
    out: dict = {}
    for svc in sorted(services):
        row = {}
        for signal in sig.SIGNALS:
            if sig.supported(signal, svc):
                try:
                    row[signal] = sig.measure(prom, signal, svc, window)
                except PromError:
                    row[signal] = None
        out[svc] = row
    return out


def _moved(signal: str, base: Optional[float], value: Optional[float],
           min_factor: Optional[float] = None) -> bool:
    if base is None or value is None:
        return False
    if min_factor is not None and (base <= 0 or value < base * min_factor):
        return False
    delta = value - base
    if signal == "error_count" and base > ERROR_COUNT_MAX_BASE:
        return False
    if signal == "throughput_rps" and delta < 0:
        return base >= THROUGHPUT_MIN_BASE_RPS and (-delta / base) >= THROUGHPUT_DROP
    absolute, relative = MOVE_THRESHOLDS[signal]
    rel = (delta / abs(base)) if base else float("inf")
    return delta >= absolute and rel >= relative


# Identical readings closer than this are the same Prometheus point. A value can
# legitimately repeat across points (lock contention sits at the 15,000 ms bucket
# ceiling every minute), so repeats further apart count as new points.
SAME_POINT_WINDOW_S = 45.0


def distinct_points(series: list, times: list) -> tuple:
    """Collapse identical readings taken within SAME_POINT_WINDOW_S of the last kept one."""
    out_s, out_t = [], []
    for t, v in zip(times, series):
        if out_s and v is not None and out_s[-1] == v and t - out_t[-1] < SAME_POINT_WINDOW_S:
            continue
        out_s.append(v)
        out_t.append(t)
    return out_s, out_t


def sustained_runs(signal: str, base: Optional[float], series: list, times: list,
                   need: int = SUSTAINED_SAMPLES, min_factor: Optional[float] = None) -> list:
    """Runs of at least ``need`` consecutive moved DATA POINTS, as (start_time, values)."""
    series, times = distinct_points(series, times)
    runs, current, start = [], [], None
    for t, v in zip(times, series):
        if _moved(signal, base, v, min_factor):
            if not current:
                start = t
            current.append(v)
        else:
            if len(current) >= need:
                runs.append((start, current))
            current = []
    if len(current) >= need:
        runs.append((start, current))
    return runs


def _median(values) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def _peak(signal: str, base: Optional[float], values) -> Optional[float]:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    if signal == "throughput_rps" and base is not None:
        return max(vals, key=lambda v: abs(v - base))
    return max(vals)


def analyse(baseline: list, during: list, after: list, root_services: frozenset,
            effect_until_s: Optional[float] = None) -> dict:
    """Compare samples: which signals moved, how fast, and did they come back.

    Each sample is {"t": seconds since injection, "values": {svc: {signal: v}}}.
    ``effect_until_s``: samples up to this time (normally clear time + EFFECT_LAG_S)
    count as the effect window even if taken after the clear; recovery is judged
    on the samples after it. None: effect window is the "during" samples only.
    """
    if effect_until_s is None:
        effect, recovery = list(during), list(after)
    else:
        effect = list(during) + [x for x in after if x["t"] <= effect_until_s]
        recovery = [x for x in after if x["t"] > effect_until_s]
    movers = []
    first_effect_s = None
    services = set()
    for s in baseline + during + after:
        services.update(s["values"])
    for svc in sorted(services - EXCLUDED_SERVICES):
        signals = set()
        for s in baseline + during + after:
            signals.update(s["values"].get(svc, {}))

        def series(samples, signal):
            return [x["values"].get(svc, {}).get(signal) for x in samples]

        base_rps = _median(series(baseline, "throughput_rps"))
        for signal in sorted(signals):
            if signal.startswith("latency_") and (base_rps is None or base_rps < LATENCY_MIN_RPS):
                continue
            factor = (LATENCY_LOW_TRAFFIC_FACTOR if signal == "latency_p95_ms"
                      and base_rps < LATENCY_LOW_TRAFFIC_RPS else None)
            base = _median(series(baseline, signal))
            runs = sustained_runs(signal, base, series(effect, signal), [x["t"] for x in effect],
                                  min_factor=factor)
            if not runs:
                continue
            peak = _peak(signal, base, [v for _, vals in runs for v in vals])
            first = runs[0][0]
            rec_vals, _ = distinct_points(series(recovery, signal), [x["t"] for x in recovery])
            tail = [v for v in rec_vals if v is not None][-SUSTAINED_SAMPLES:]
            last_after = tail[-1] if tail else None
            recovered = (None if len(tail) < SUSTAINED_SAMPLES
                         else not any(_moved(signal, base, v, factor) for v in tail))
            movers.append({"service": svc, "signal": signal, "baseline": base, "peak": peak,
                           "end_after_clear": last_after, "first_seen_s": first,
                           "recovered_after_clear": recovered, "is_root_service": svc in root_services})
            if svc in root_services and first is not None:
                first_effect_s = first if first_effect_s is None else min(first_effect_s, first)
    movers.sort(key=lambda m: (not m["is_root_service"], m["service"], m["signal"]))
    root_moved = [m for m in movers if m["is_root_service"]]
    return {
        "movers": movers,
        "root_service_moved": bool(root_moved),
        "root_first_effect_s": first_effect_s,
        "root_recovered_after_clear": (all(m["recovered_after_clear"] for m in root_moved)
                                       if root_moved and all(m["recovered_after_clear"] is not None
                                                             for m in root_moved) else None),
        "other_services_moved": sorted({m["service"] for m in movers if not m["is_root_service"]}),
    }


SPAN_SIGNALS = ("error_rate", "latency_p95_ms", "throughput_rps")


def check_coverage(values: dict) -> None:
    """Stop early if no request-based signal has data for any service.

    That means the window is too short or there is no traffic, and a long probe
    would measure nothing (this happened once with a 1-minute window).
    """
    if not any(row.get(s) is not None for row in values.values() for s in SPAN_SIGNALS):
        raise ProbeError("no request-based signal has data for any service: is the load generator "
                         "running, and is the window at least 2m?")


def _changes_for(item: str) -> dict:
    if item == CONTROL:
        return {}
    if item == FLOOD:
        return {LOAD_FLAG: "on"}
    return {item: PROBE_VARIANTS[item]}


def _roots_for(item: str) -> frozenset:
    if item in FAULT_TYPES:
        return accepted_services(item)
    if item == FLOOD:
        return frozenset({"frontend", "frontend-proxy"})
    return frozenset()


def _clear_all(store: FlagStore) -> None:
    dirty = {k: REST_VARIANT for k, v in variants_of(store.read()).items() if v != REST_VARIANT}
    if dirty:
        apply(store, dirty)


def _collect(prom, window, phase_s, every_s, t_offset, clock, sleep, samples) -> None:
    start = clock()
    while True:
        elapsed = clock() - start
        samples.append({"t": round(t_offset + elapsed, 1), "values": sample_all(prom, window)})
        if elapsed + every_s > phase_s:
            break
        sleep(every_s)


def probe_item(prom: PromClient, store: FlagStore, item: str, hold_s: float, recover_s: float,
               every_s: float, window: str, clock: Callable[[], float],
               sleep: Callable[[float], None]) -> dict:
    changes = _changes_for(item)
    dirty = {k: v for k, v in variants_of(store.read()).items() if v != REST_VARIANT}
    if dirty:
        return {"item": item, "skipped": f"flags not at rest: {dirty}"}

    baseline = [{"t": -every_s, "values": sample_all(prom, window)}]
    check_coverage(baseline[0]["values"])
    sleep(every_s)
    baseline.append({"t": 0.0, "values": sample_all(prom, window)})

    started = _now_iso()
    if changes:
        apply(store, changes)
    else:
        store.write(store.read())  # control: no-op write
    during: list = []
    try:
        _collect(prom, window, hold_s, every_s, 0.0, clock, sleep, during)
        drift = {k: v for k, v in variants_of(store.read()).items()
                 if v != changes.get(k, REST_VARIANT)}
    finally:
        _clear_all(store)
    cleared = _now_iso()
    after: list = []
    sleep(every_s)
    _collect(prom, window, recover_s, every_s, hold_s + every_s, clock, sleep, after)

    result = {"item": item, "changes": changes, "started_at": started, "cleared_at": cleared,
              "contaminated": bool(drift), "drift": drift,
              "samples": {"baseline": baseline, "during": during, "after": after}}
    result.update(analyse(baseline, during, after, _roots_for(item), effect_until_s=hold_s + EFFECT_LAG_S))
    return result


def run(prom: PromClient, store: FlagStore, items=ITEMS, hold_s: float = 300, recover_s: float = 180,
        every_s: float = 30, window: str = "2m", clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = lambda m: None,
        path: Optional[Path] = None) -> dict:
    min_recover = sig.window_seconds(window) + 60
    if recover_s < min_recover:
        raise ProbeError(f"recover_s must be at least window + 60s = {min_recover}s, so the last sample's "
                         f"window starts after the fault was cleared")
    unknown = [i for i in items if i not in ITEMS]
    if unknown:
        raise ValueError(f"unknown probe items {unknown}; choose from {list(ITEMS)}")
    report = {"kind": "fault_probe", "generated_at": _now_iso(), "settings": {
        "hold_s": hold_s, "recover_s": recover_s, "every_s": every_s, "window": window,
        "variants": {i: PROBE_VARIANTS.get(i) for i in items}, "thresholds": MOVE_THRESHOLDS,
        "throughput_drop": THROUGHPUT_DROP, "throughput_min_base_rps": THROUGHPUT_MIN_BASE_RPS,
        "sustained_samples": SUSTAINED_SAMPLES, "same_point_window_s": SAME_POINT_WINDOW_S, "effect_lag_s": EFFECT_LAG_S,
        "latency_min_rps": LATENCY_MIN_RPS, "error_count_max_base": ERROR_COUNT_MAX_BASE,
        "latency_low_traffic_rps": LATENCY_LOW_TRAFFIC_RPS, "latency_low_traffic_factor": LATENCY_LOW_TRAFFIC_FACTOR},
        "items": []}
    try:
        for n, item in enumerate(items, 1):
            log(f"[{n}/{len(items)}] {item} ...")
            result = probe_item(prom, store, item, hold_s, recover_s, every_s, window, clock, sleep)
            report["items"].append(result)
            log("    " + summary_line(result))
            if path is not None:
                save(report, path)
    finally:
        _clear_all(store)
        report["finished_at"] = _now_iso()
        if path is not None:
            save(report, path)
    return report


def reanalyse(report: dict) -> dict:
    """Re-score a saved report's raw samples with the current rules (no shop needed)."""
    out = dict(report)
    out["items"] = []
    for item in report.get("items", []):
        if "samples" not in item:
            out["items"].append(item)
            continue
        sm = item["samples"]
        new = dict(item)
        hold = report.get("settings", {}).get("hold_s")
        until = hold + EFFECT_LAG_S if hold is not None else None
        new.update(analyse(sm["baseline"], sm["during"], sm["after"], _roots_for(item["item"]), effect_until_s=until))
        out["items"].append(new)
    out["reanalysed_at"] = _now_iso()
    out.setdefault("settings", {})["thresholds_used_for_reanalysis"] = {
        "move": MOVE_THRESHOLDS, "throughput_drop": THROUGHPUT_DROP, "effect_lag_s": EFFECT_LAG_S,
        "latency_min_rps": LATENCY_MIN_RPS, "error_count_max_base": ERROR_COUNT_MAX_BASE,
        "latency_low_traffic_rps": LATENCY_LOW_TRAFFIC_RPS, "latency_low_traffic_factor": LATENCY_LOW_TRAFFIC_FACTOR,
        "throughput_min_base_rps": THROUGHPUT_MIN_BASE_RPS, "sustained_samples": SUSTAINED_SAMPLES}
    return out


def new_path(directory: Optional[Path] = None) -> Path:
    directory = Path(directory) if directory is not None else REPORTS_DIR
    return directory / f"faults-{dt.datetime.now():%Y%m%d-%H%M%S}.json"


def save(report: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)
    return path


def summary_line(result: dict) -> str:
    if "skipped" in result:
        return f"skipped: {result['skipped']}"
    roots = [f"{m['service']}.{m['signal']} {m['baseline']}->{m['peak']}" for m in result["movers"]
             if m["is_root_service"]]
    others = result["other_services_moved"]
    first = result["root_first_effect_s"]
    return (f"root moved: {roots or 'NOTHING'}; first effect: {f'{first}s' if first is not None else 'n/a'}; "
            f"recovered after clear: {result['root_recovered_after_clear']}; "
            f"other services moved: {others or 'none'}"
            + ("; CONTAMINATED" if result["contaminated"] else ""))
