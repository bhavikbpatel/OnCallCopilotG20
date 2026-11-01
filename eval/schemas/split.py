"""Check the dev / held-out split of the golden set without revealing held-out content.

    python -m eval.schemas.split --dev eval/cases/dev --heldout "$EVAL_PRIVATE_DIR/heldout"

Prints counts and coverage only: never a held-out case's faults, actions or answers, so
the output can be shared with the whole team (Persons 2 and 3 must not read held-out cases;
capstone guide p. 5, Sprint Backlog). Exit code 0 when every rule holds, 1 otherwise,
2 when the input cannot be read.

Rules (capstone guide pp. 5-6; System Design §11):
- every case validates; IDs are unique; dev IDs start D-, held-out IDs H-;
- related variants stay together: no ``scenario_group`` appears in both splits;
- no held-out case repeats a dev case's exact fault and load setup;
- the held-out folder is outside the repo;
- sizes: at least 40 held-out cases and 60 in total; every golden fault type, noise and a
  two-fault case appear in both splits.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

from .case import Case, load_cases
from .catalogue import GOLDEN_FAULT_TYPES

REPO_ROOT = Path(__file__).resolve().parents[2]
MIN_HELDOUT = 40
MIN_TOTAL = 60


def _setup(case: Case) -> str:
    """The injected situation, for spotting exact repeats across the split."""
    faults = sorted((f.model_dump(mode="json") for f in case.faults), key=lambda f: json.dumps(f, sort_keys=True))
    return json.dumps({"faults": faults, "load": case.load.model_dump(mode="json")}, sort_keys=True)


def coverage(cases: list[Case]) -> dict:
    by_fault = Counter(f.flag for c in cases for f in c.faults)
    return {
        "cases": len(cases),
        "noise": sum(c.expected.verdict == "noise" for c in cases),
        "two_fault": sum(len(c.faults) >= 2 for c in cases),
        "by_fault": {k: by_fault.get(k, 0) for k in sorted(GOLDEN_FAULT_TYPES)},
        "intermittent": sum(any(f.mode == "intermittent" for f in c.faults) for c in cases),
        "under_flood": sum(c.load.profile == "flood" and bool(c.faults) for c in cases),
        "late_start": sum(any(f.start_after_s > 0 for f in c.faults) for c in cases),
    }


def check(dev: list[Case], heldout: list[Case], heldout_dir: Optional[Path] = None) -> list[str]:
    """Problems with the split; empty when every rule holds. Messages never quote held-out content."""
    problems = []
    if heldout_dir is not None:
        p = heldout_dir.resolve()
        if p == REPO_ROOT or REPO_ROOT in p.parents:
            problems.append("held-out folder is inside the repo; it must live outside (EVAL_PRIVATE_DIR)")
    if any(c.split != "dev" for c in dev):
        problems.append("a case in the dev folder is not split: dev")
    if any(c.split != "heldout" for c in heldout):
        problems.append("a case in the held-out folder is not split: heldout")
    ids = Counter(c.case_id for c in dev + heldout)
    dupes = sorted(i for i, n in ids.items() if n > 1)
    if dupes:
        problems.append(f"duplicate case IDs: {dupes}")
    shared = {c.scenario_group for c in dev} & {c.scenario_group for c in heldout}
    if shared:
        problems.append(f"{len(shared)} scenario group(s) appear in both splits: {sorted(shared)}")
    repeats = {_setup(c) for c in dev} & {_setup(c) for c in heldout}
    if repeats:
        problems.append(f"{len(repeats)} held-out case(s) repeat a dev case's exact fault and load setup")
    if len(heldout) < MIN_HELDOUT:
        problems.append(f"only {len(heldout)} held-out cases; need at least {MIN_HELDOUT}")
    if len(dev) + len(heldout) < MIN_TOTAL:
        problems.append(f"only {len(dev) + len(heldout)} cases in total; need at least {MIN_TOTAL}")
    for name, cases in (("dev", dev), ("held-out", heldout)):
        cov = coverage(cases)
        missing = sorted(k for k, v in cov["by_fault"].items() if v == 0)
        if missing:
            problems.append(f"{name} set has no case for: {missing}")
        if not cov["noise"]:
            problems.append(f"{name} set has no noise case")
        if not cov["two_fault"]:
            problems.append(f"{name} set has no two-fault case")
    return problems


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m eval.schemas.split", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dev", required=True, help="dev case folder (eval/cases/dev)")
    p.add_argument("--heldout", required=True, help="held-out case folder (outside the repo)")
    args = p.parse_args(argv)
    try:
        dev = load_cases(Path(args.dev))
        heldout = load_cases(Path(args.heldout))
    except Exception as exc:  # validation errors may quote content: report the type only
        print(f"cannot read cases: {type(exc).__name__} (run load_cases on one folder privately for details)",
              file=sys.stderr)
        return 2
    d, h = coverage(dev), coverage(heldout)
    print(f"{'':32}{'dev':>6}{'held-out':>10}")
    for key in ("cases", "noise", "two_fault", "intermittent", "under_flood", "late_start"):
        print(f"{key:32}{d[key]:>6}{h[key]:>10}")
    for fault in sorted(GOLDEN_FAULT_TYPES):
        print(f"  {fault:30}{d['by_fault'][fault]:>6}{h['by_fault'][fault]:>10}")
    problems = check(dev, heldout, Path(args.heldout))
    print()
    if problems:
        print("PROBLEMS:")
        for x in problems:
            print(f"- {x}")
        return 1
    print(f"OK: {len(dev)} dev + {len(heldout)} held-out = {len(dev) + len(heldout)} cases; split rules hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
