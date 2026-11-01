"""Run evaluation cases against the live shop.

    python -m eval.runner --cases eval/cases/dev --config-id DEV-NULL
    python -m eval.runner --cases eval/cases/dev --only D-001,D-015 --config-id TRY
    python -m eval.runner --cases eval/cases/dev --config-id CI --min-success 0.6   # CI gate
    python -m eval.runner --cases eval/cases/dev --attacks "$EVAL_PRIVATE_DIR/redteam" --config-id E2-ON

Writes eval/results/runs/<config_id>-<time>/{results,scores}.jsonl and summary.json.
Exit code: 0 ok, 1 below --min-success (or interrupted), 2 cannot start.
Every flag is cleared after each case, also on Ctrl+C.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from ..injector.flagstore import HttpFlagStore
from ..injector.injector import Injector
from ..schemas.case import load_case, load_cases
from ..telemetry.prom import PromClient, PromError
from .agents import AGENTS
from .config import RunConfig
from .runner import RUNS_DIR, RunDeps, meets_threshold, run_batch


def parse(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m eval.runner", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cases", required=True, help="folder of case .yaml files, or one .yaml file")
    p.add_argument("--only", help="comma-separated case IDs to run (with --attacks: attack IDs)")
    p.add_argument("--attacks", help="private red-team folder: run each attack with its base case from --cases")
    p.add_argument("--config-id", required=True, help="name for this run configuration, e.g. E1-C2")
    p.add_argument("--agent", default="do-nothing", choices=sorted(AGENTS))
    p.add_argument("--approval", default="approve", choices=["approve", "reject"])
    p.add_argument("--max-tool-calls", type=int, default=20)
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--agent-timeout", type=float, default=600.0, help="seconds (default 600)")
    p.add_argument("--alert-timeout", type=float, default=420.0, help="seconds (default 420)")
    p.add_argument("--min-success", type=float, default=None, help="CI gate: fail if task success is below this")
    p.add_argument("--force-clear", action="store_true", help="clear leftover active flags before each case")
    p.add_argument("--prom-url", default=None)
    p.add_argument("--flags-url", default=None)
    p.add_argument("--results-dir", default=str(RUNS_DIR))
    return p.parse_args(argv)


def select_cases(path: str, only: Optional[str]):
    p = Path(path)
    cases = [load_case(p)] if p.is_file() else load_cases(p)
    if only:
        wanted = [c.strip() for c in only.split(",") if c.strip()]
        known = {c.case_id for c in cases}
        missing = [c for c in wanted if c not in known]
        if missing:
            raise SystemExit(f"unknown case IDs: {missing}")
        cases = [c for c in cases if c.case_id in wanted]
    return cases


def main(argv: Optional[list[str]] = None, deps: Optional[RunDeps] = None) -> int:
    args = parse(argv)
    try:
        cfg = RunConfig(config_id=args.config_id, agent=args.agent, approval=args.approval,
                        max_tool_calls=args.max_tool_calls, agent_timeout_s=args.agent_timeout,
                        alert_timeout_s=args.alert_timeout, repeats=args.repeats)
        attacks = None
        if args.attacks:
            from ..schemas.attack import load_attacks
            attacks = load_attacks(Path(args.attacks))
            if args.only:
                wanted = {a.strip() for a in args.only.split(",") if a.strip()}
                unknown = sorted(wanted - {a.attack_id for a in attacks})
                if unknown:
                    raise SystemExit(f"unknown attack IDs: {unknown}")
                attacks = [a for a in attacks if a.attack_id in wanted]
            cases = select_cases(args.cases, None)
        else:
            cases = select_cases(args.cases, args.only)
    except (ValueError, OSError) as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2
    if not cases:
        print("cannot start: no cases selected", file=sys.stderr)
        return 2
    if deps is None:
        prom = PromClient(args.prom_url)
        try:
            prom.build_info()
        except PromError as exc:
            print(f"cannot start: {exc}", file=sys.stderr)
            return 2
        deps = RunDeps(prom=prom, injector=Injector(HttpFlagStore(args.flags_url)),
                       results_dir=Path(args.results_dir), force_clear=args.force_clear)
    agent = AGENTS[cfg.agent]()

    def show(result, report):
        rec = {True: "recovered", False: "not recovered", None: "recovery n/a"}[report.recovered]
        who = f"{result.attack_id} on {result.case_id}" if result.attack_id else result.case_id
        print(f"{who} r{result.repeat}: {result.status:<16} success={report.task_success!s:<5} {rec}"
              + (f"  ({result.error})" if result.error else ""), flush=True)

    what = f"{len(attacks)} attack(s)" if attacks else f"{len(cases)} case(s)"
    print(f"{what} x {cfg.repeats} repeat(s), agent {agent.name}, config {cfg.config_id}", flush=True)
    try:
        summary = run_batch(cases, agent, cfg, deps, on_result=show, attacks=attacks)
    except KeyboardInterrupt:
        print("\ninterrupted: flags cleared; partial results and summary written", file=sys.stderr)
        return 1
    print(f"\ntask success {summary['task_success_rate']}  status {summary['status_counts']}  -> {summary['directory']}")
    if attacks:
        print(f"attack success {summary.get('attack_success_rate')}  influence {summary.get('attack_influence_rate')}  "
              f"judged {summary.get('attacks_judged')}/{summary.get('attacks_run')}  "
              f"false blocks {summary.get('false_blocks')}")
    if args.min_success is not None and not meets_threshold(summary, args.min_success):
        print(f"FAIL: task success {summary['task_success_rate']} is below {args.min_success}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
