"""python -m eval.report [--batch ID,...] [--experiments] [--out FILE] [--json FILE]  (see eval/report/__init__.py)

Exit code: 0 ok, 2 cannot start (no batches, unknown batch ID).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from . import (RUNS_DIR, by_config, case_index, config_report, configs_markdown, experiment_log_markdown,
               experiments_markdown, find_batches, load_batch)

DEV_DIR = Path(__file__).resolve().parents[1] / "cases" / "dev"


def _private_heldout() -> Optional[Path]:
    """EVAL_PRIVATE_DIR/heldout when it is configured (held-out runs need their cases for per-fault tables)."""
    try:
        from ..settings import private_dir
        return private_dir() / "heldout"
    except Exception:
        return None


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m eval.report", description=__doc__)
    p.add_argument("--runs", default=str(RUNS_DIR), help="folder of batches (default eval/results/runs)")
    p.add_argument("--batch", help="comma-separated batch IDs (default: every batch)")
    p.add_argument("--experiments", action="store_true", help="E1-E4 comparisons from experiment-log.jsonl")
    p.add_argument("--log", action="store_true", help="the experiment log as a table (only that is printed)")
    p.add_argument("--rehearsal", action="store_true", help="with --experiments: the dev rehearsal arms instead")
    p.add_argument("--experiments-dir", default=None)
    p.add_argument("--heldout", default=None, help="held-out cases folder (default EVAL_PRIVATE_DIR/heldout)")
    p.add_argument("--out", help="write the markdown here (default: print)")
    p.add_argument("--json", help="also write every figure as JSON here")
    args = p.parse_args(argv)

    log_path = Path(args.runs) / "experiment-log.jsonl"
    if args.log:
        if not log_path.is_file():
            print(f"cannot start: no experiment log at {log_path}", file=sys.stderr)
            return 2
        text = experiment_log_markdown([json.loads(x) for x in log_path.read_text().splitlines() if x.strip()])
        if args.out:
            Path(args.out).write_text(text)
            print(f"wrote {args.out}")
        else:
            print(text)
        return 0
    heldout = Path(args.heldout) if args.heldout else _private_heldout()
    cases = case_index([DEV_DIR, heldout])
    try:
        ids = [x.strip() for x in (args.batch or "").split(",") if x.strip()] or None
        folders = find_batches(Path(args.runs), ids)
    except ValueError as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2
    if not folders:
        print(f"cannot start: no batches under {args.runs}", file=sys.stderr)
        return 2
    batches = [load_batch(f, cases) for f in folders]
    unknown = sorted({r.result.case_id for b in batches for r in b.runs if r.case is None})
    reports = [config_report(cid, runs) for cid, runs in sorted(by_config(batches).items())]
    text = configs_markdown(reports)
    if args.experiments:
        from ..schemas.experiment import EXPERIMENTS_DIR, load_experiments
        log = [json.loads(x) for x in log_path.read_text().splitlines() if x.strip()] if log_path.is_file() else []
        exps = load_experiments(Path(args.experiments_dir) if args.experiments_dir else EXPERIMENTS_DIR)
        text = experiments_markdown(exps, log, cases, rehearsal=args.rehearsal) + "\n" + text
    if unknown:
        text += (f"\nNote: {len(unknown)} case(s) not found in the case folders; their runs count in every "
                 f"table but appear as fault type and service 'unknown'.\n")
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out} ({len(batches)} batch(es), {sum(len(b.runs) for b in batches)} run(s))")
    else:
        print(text)
    if args.json:
        Path(args.json).write_text(json.dumps(reports, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
