"""Run evaluation cases against the live shop.

    python -m eval.runner --cases eval/cases/dev --config-id DEV-NULL
    python -m eval.runner --cases eval/cases/dev --only D-001,D-015 --config-id TRY
    python -m eval.runner --cases eval/cases/dev --config-id CI --min-success 0.6   # CI gate
    python -m eval.runner --cases eval/cases/dev --attacks "$EVAL_PRIVATE_DIR/redteam" --config-id E2-ON
    python -m eval.runner --experiment E4 --plan              # experiments: eval/experiments/E1-E4.yaml
    python -m eval.runner --experiment E4 --arm memory-off    # one arm (default: every arm not reused)

Writes eval/results/runs/<config_id>-<time>/{results,scores}.jsonl and summary.json.
Exit code: 0 ok, 1 below --min-success (or interrupted), 2 cannot start.
Every flag is cleared after each case, also on Ctrl+C.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

from ..injector.flagstore import HttpFlagStore
from ..injector.injector import Injector
from ..schemas.case import load_case, load_cases
from ..schemas.experiment import EXPERIMENTS_DIR
from ..telemetry.prom import PromClient, PromError
from .agents import AGENTS, not_ready
from .config import RunConfig
from .runner import RUNS_DIR, RunDeps, meets_threshold, run_batch


def parse(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m eval.runner", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cases", help="folder of case .yaml files, or one .yaml file")
    p.add_argument("--only", help="comma-separated case IDs to run (with --attacks: attack IDs)")
    p.add_argument("--attacks", help="private red-team folder: run each attack with its base case from --cases")
    p.add_argument("--config-id", help="name for this run configuration, e.g. E1-C2")
    p.add_argument("--experiment", help="run an experiment from eval/experiments (E1-E4): cases, repeats, budget "
                                        "and agent options come from its file")
    p.add_argument("--arm", help="with --experiment: comma-separated arm IDs (default: every arm not reused)")
    p.add_argument("--plan", action="store_true", help="with --experiment: print runs per arm and hours; run nothing")
    p.add_argument("--rehearse", action="store_true",
                   help="with --experiment: dev rehearsal of every arm (eval/runner/experiments.py REHEARSAL_CASES)")
    p.add_argument("--minutes-per-run", type=float, default=14.0,
                   help="with --plan (default 14: 12.9 min measured in the 6 Nov C1 rehearsal, settle included)")
    p.add_argument("--experiments-dir", default=str(EXPERIMENTS_DIR))
    p.add_argument("--agent", default="do-nothing", choices=sorted(AGENTS))
    p.add_argument("--approval", default="approve", choices=["approve", "reject"])
    p.add_argument("--max-tool-calls", type=int, default=None, help="default 20")
    p.add_argument("--repeats", type=int, default=None, help="default 1")
    p.add_argument("--agent-timeout", type=float, default=None,
                   help="seconds; default: worked out per case from the graph's verify windows (config/app.yaml)")
    p.add_argument("--alert-timeout", type=float, default=420.0, help="seconds (default 420)")
    p.add_argument("--min-success", type=float, default=None, help="CI gate: fail if task success is below this")
    p.add_argument("--force-clear", action="store_true", help="clear leftover active flags before each case")
    p.add_argument("--prom-url", default=None)
    p.add_argument("--flags-url", default=None)
    p.add_argument("--results-dir", default=str(RUNS_DIR))
    args = p.parse_args(argv)
    if args.experiment:
        clash = [f for f, v in (("--cases", args.cases), ("--attacks", args.attacks), ("--config-id", args.config_id),
                                ("--max-tool-calls", args.max_tool_calls), ("--repeats", args.repeats)) if v is not None]
        if clash:
            p.error(f"{', '.join(clash)} come from the experiment file; do not combine with --experiment")
    else:
        if args.cases is None or args.config_id is None:
            p.error("--cases and --config-id are required (or use --experiment)")
        if args.arm or args.plan or args.rehearse:
            p.error("--arm, --plan and --rehearse need --experiment")
        args.max_tool_calls = 20 if args.max_tool_calls is None else args.max_tool_calls
        args.repeats = 1 if args.repeats is None else args.repeats
    return args


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


def _split(s: Optional[str]) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _live_deps(args) -> Optional[RunDeps]:
    prom = PromClient(args.prom_url)
    try:
        prom.build_info()
    except PromError as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return None
    return RunDeps(prom=prom, injector=Injector(HttpFlagStore(args.flags_url)),
                   results_dir=Path(args.results_dir), force_clear=args.force_clear)


# Person 5's Prometheus metrics (copilot/metrics.py), only when COPILOT_METRICS_PORT is set,
# so eval/ does not depend on copilot/ or prometheus_client otherwise.
_metrics = None


def _start_metrics() -> None:
    global _metrics
    if not os.environ.get("COPILOT_METRICS_PORT"):
        return
    try:
        from copilot import metrics
    except ImportError as exc:
        print(f"metrics off: {exc}", file=sys.stderr)
        return
    port = metrics.serve()
    if port:
        _metrics = metrics
        print(f"metrics on http://localhost:{port}/metrics", flush=True)


def show(result, report):
    if _metrics is not None:
        _metrics.record_run(result, report)
    rec = {True: "recovered", False: "not recovered", None: "recovery n/a"}[report.recovered]
    who = f"{result.attack_id} on {result.case_id}" if result.attack_id else result.case_id
    print(f"{who} r{result.repeat}: {result.status:<16} success={report.task_success!s:<5} {rec}"
          + (f"  ({result.error})" if result.error else ""), flush=True)


def experiment_main(args, deps: Optional[RunDeps]) -> int:
    from ..schemas.experiment import resolve
    from .experiments import arm_config, format_plan, plan, run_experiment
    try:
        exp = resolve(args.experiment, Path(args.experiments_dir))
        if args.plan:
            print(format_plan(plan(exp, minutes_per_run=args.minutes_per_run, rehearsal=args.rehearse)))
            return 0
        agent = AGENTS[args.agent]()
        arm_ids = _split(args.arm)
        arms = [exp.arm(i) for i in arm_ids] if arm_ids else [a for a in exp.arms if a.reuse is None]
        for arm in arms:
            why = None if arm.reuse else not_ready(agent, options=arm_config(exp, arm, rehearsal=args.rehearse).options)
            if why:
                raise ValueError(f"{exp.experiment}/{arm.id}: {why}")
    except (ValueError, KeyError, OSError) as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2
    if deps is None:
        deps = _live_deps(args)
        if deps is None:
            return 2
    try:
        entries = run_experiment(exp, arm_ids, agent, deps, run_batch=run_batch, only=_split(args.only),
                                 on_result=show, agent=args.agent, approval=args.approval, rehearsal=args.rehearse,
                                 agent_timeout_s=args.agent_timeout, alert_timeout_s=args.alert_timeout)
    except KeyboardInterrupt:
        print("\ninterrupted: flags cleared; partial results, summary and experiment-log line written",
              file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2
    for e in entries:
        print(f"{e['config_id']}: task success {e['task_success_rate']}  status {e['status_counts']}"
              + ("  (partial)" if e["partial"] else "") + f"  -> {e['directory']}")
    return 0


def main(argv: Optional[list[str]] = None, deps: Optional[RunDeps] = None) -> int:
    args = parse(argv)
    _start_metrics()
    if args.experiment:
        return experiment_main(args, deps)
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
    agent = AGENTS[cfg.agent]()
    why = not_ready(agent)
    if why:
        print(f"cannot start: {why}", file=sys.stderr)
        return 2
    if deps is None:
        deps = _live_deps(args)
        if deps is None:
            return 2

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
