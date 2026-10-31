"""Command line for live-shop probes.

    python -m eval.probes discover              # ~4 minutes, includes the noise check
    python -m eval.probes discover --no-noise   # ~10 seconds, read-only

Run from the repo root (OnCallCopilotG20) with the shop running and the flag UI
scheduler stopped. Writes a JSON report under eval/results/probes/.
"""
from __future__ import annotations

import argparse
import sys

from ..injector.flagstore import FlagStoreError, HttpFlagStore
from ..telemetry.prom import PromClient, PromError
from . import discover as disc


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m eval.probes", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prom-url", help="Prometheus URL (default http://localhost:9090)")
    p.add_argument("--flag-url", help="flag UI URL (default http://localhost:8080/feature)")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover", help="list metrics, measure baselines, check flag-write noise")
    d.add_argument("--no-noise", action="store_true", help="skip the noise check (fully read-only)")
    d.add_argument("--wait", type=float, default=180.0, help="seconds to wait after the no-op write (default 180)")
    args = p.parse_args(argv)

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


if __name__ == "__main__":
    sys.exit(main())
