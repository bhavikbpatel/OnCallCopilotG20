"""Command line for the fault injector.

    python -m eval.injector status
    python -m eval.injector clear
    python -m eval.injector set paymentFailure 50%
    python -m eval.injector inject D-001 --duration 600

`inject` runs in the foreground: it applies the case's plan, holds it for
--duration seconds (or until Ctrl+C), then always puts every flag back to rest
and writes the ground-truth record under eval/results/ground_truth/.

Run from the repo root (OnCallCopilotG20). Set FLAG_UI_URL if the shop's proxy
is not on http://localhost:8080.
"""
from __future__ import annotations

import argparse
import datetime as dt
import signal
import sys
import time
from pathlib import Path

from ..schemas.case import load_case
from .flagstore import REST_VARIANT, FlagStoreError, HttpFlagStore, apply
from .injector import Injector, PreflightError

DEV_DIR = Path(__file__).resolve().parents[1] / "cases" / "dev"


def _find_case(case_ref: str):
    path = Path(case_ref)
    if path.suffix == ".yaml" and path.exists():
        return load_case(path)
    dev = DEV_DIR / f"{case_ref}.yaml"
    if dev.exists():
        return load_case(dev)
    raise SystemExit(f"case {case_ref!r} not found (give a dev case ID or a path to a .yaml file)")


def _print_status(inj: Injector) -> int:
    status = inj.status()
    active = {k: v for k, v in status.items() if v != REST_VARIANT}
    for name in sorted(status):
        mark = "  <-- ACTIVE" if name in active else ""
        print(f"  {name:32s} {status[name]}{mark}")
    print(f"\n{len(active)} flag(s) active" if active else "\nall flags at rest")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m eval.injector", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", help="flag UI base URL (default http://localhost:8080/feature)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show every flag and whether it is active")
    sub.add_parser("clear", help="put every flag back to rest")
    s = sub.add_parser("set", help="set one flag (manual testing)")
    s.add_argument("flag")
    s.add_argument("variant")
    i = sub.add_parser("inject", help="run a case's fault plan, then clear")
    i.add_argument("case", help="dev case ID (e.g. D-001) or path to a case .yaml")
    i.add_argument("--duration", type=float, default=600.0, help="seconds to hold the incident (default 600)")
    i.add_argument("--run-id", default=None)
    i.add_argument("--force-clear", action="store_true", help="clear leftover active flags before starting")
    args = p.parse_args(argv)

    inj = Injector(HttpFlagStore(args.url))
    try:
        if args.cmd == "status":
            return _print_status(inj)
        if args.cmd == "clear":
            cleared = inj.clear()
            print(f"cleared: {cleared}" if cleared else "nothing to clear: all flags at rest")
            return 0
        if args.cmd == "set":
            apply(inj.store, {args.flag: args.variant})
            print(f"{args.flag} = {args.variant}  (remember: python -m eval.injector clear)")
            return 0
        return _inject(inj, args)
    except (FlagStoreError, PreflightError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _inject(inj: Injector, args) -> int:
    case = _find_case(args.case)
    run_id = args.run_id or f"manual-{case.case_id}-{dt.datetime.now():%Y%m%d-%H%M%S}"
    # Closing the terminal or `kill` should still clear the flags.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *_: sys.exit(129))

    print(f"injecting {case.case_id} for {args.duration:.0f}s (Ctrl+C to stop early) ...")
    try:
        with inj.run(case, horizon_s=args.duration, run_id=run_id, force_clear=args.force_clear):
            end = time.monotonic() + args.duration
            while time.monotonic() < end:
                time.sleep(min(5.0, end - time.monotonic()))
    except KeyboardInterrupt:
        print("\nstopped early")

    rec = inj.record
    if rec is None:
        return 2
    path = rec.save()
    print(f"cleared: {rec.cleared}   contaminated: {rec.contaminated}   errors: {len(rec.errors)}")
    for d in rec.drift:
        print(f"  drift: {d['flag']} was {d['seen']} (expected {d['expected']})")
    for e in rec.errors:
        print(f"  error: {e}")
    print(f"record: {path}")
    return 0 if rec.ok else 1


if __name__ == "__main__":
    sys.exit(main())
