"""Command line for live-shop probes.

    python -m eval.probes discover              # ~4 minutes, includes the noise check
    python -m eval.probes discover --no-noise   # ~10 seconds, read-only
    python -m eval.probes faults                # ~90 minutes: control, 12 faults, flood
    python -m eval.probes faults --only control,paymentFailure   # quick check
    python -m eval.probes reanalyse eval/results/probes/faults-....json  # re-score, no shop needed

Run from the repo root (OnCallCopilotG20) with the shop running and the flag UI
scheduler stopped. Writes a JSON report under eval/results/probes/.
"""
from __future__ import annotations

import argparse
import signal
import sys

from ..injector.flagstore import FlagStoreError, HttpFlagStore
from ..telemetry.prom import PromClient, PromError
from ..injector.injector import Injector, PreflightError
from . import discover as disc
from . import faults as flt


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m eval.probes", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prom-url", help="Prometheus URL (default http://localhost:9090)")
    p.add_argument("--flag-url", help="flag UI URL (default http://localhost:8080/feature)")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover", help="list metrics, measure baselines, check flag-write noise")
    d.add_argument("--no-noise", action="store_true", help="skip the noise check (fully read-only)")
    d.add_argument("--wait", type=float, default=180.0, help="seconds to wait after the no-op write (default 180)")
    f = sub.add_parser("faults", help="inject each fault in turn and measure its effect")
    f.add_argument("--only", help="comma-separated items (default: all). Items: " + ",".join(flt.ITEMS))
    f.add_argument("--hold", type=float, default=150.0, help="seconds each fault is held (default 150)")
    f.add_argument("--recover", type=float, default=180.0,
                   help="seconds sampled after clearing; at least window + 60 (default 180)")
    f.add_argument("--every", type=float, default=30.0, help="seconds between samples (default 30)")
    r = sub.add_parser("reanalyse", help="re-score a saved fault-probe report with the current rules")
    r.add_argument("report", help="path to a faults-*.json report")
    args = p.parse_args(argv)
    if args.cmd == "reanalyse":
        return _reanalyse(args.report)
    if args.cmd == "faults":
        return _faults(args)

    prom = PromClient(args.prom_url)
    store = None if args.no_noise else HttpFlagStore(args.flag_url)
    try:
        report = disc.discover(prom, store, wait_s=args.wait, log=lambda m: print(m, flush=True))
    except (PromError, FlagStoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    path = disc.save(report)
    print()
    print("\n".join(disc.summary_lines(report)))
    print(f"\nreport: {path}")
    return 0


def _reanalyse(report_path: str) -> int:
    import json
    from pathlib import Path
    src = Path(report_path)
    if not src.exists():
        print(f"error: {src} not found", file=sys.stderr)
        return 2
    try:
        report = json.loads(src.read_text())
    except json.JSONDecodeError:
        print(f"error: {src} is not valid JSON", file=sys.stderr)
        return 2
    if report.get("kind") != "fault_probe":
        print(f"error: {src} is not a fault-probe report", file=sys.stderr)
        return 2
    new = flt.reanalyse(report)
    for item in new["items"]:
        print(f"{item['item']:30s} {flt.summary_line(item)}")
    out = src.with_name(src.stem + "-reanalysed.json")
    flt.save(new, out)  # the original report is never modified
    print(f"\nreport: {out}")
    return 0


def _faults(args) -> int:
    items = tuple(i.strip() for i in args.only.split(",")) if args.only else flt.ITEMS
    prom = PromClient(args.prom_url)
    store = HttpFlagStore(args.flag_url)
    path = flt.new_path()
    # Closing the terminal or `kill` must still clear the flags (run() clears in finally).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *_: sys.exit(129))
    per_item = 2 * args.every + args.hold + args.recover
    print(f"probing {len(items)} item(s), about {len(items) * per_item / 60:.0f} min. Ctrl+C stops safely.")
    try:
        Injector(store).preflight()
        flt.run(prom, store, items, args.hold, args.recover, args.every, log=lambda m: print(m, flush=True),
                path=path)
    except flt.ProbeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nstopped early: flags cleared; partial report kept")
    except (PromError, FlagStoreError, PreflightError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"\nreport: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
