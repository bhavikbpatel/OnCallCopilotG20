"""Run cases end to end and write scored JSONL results.

One case, one run (``run_case``):

1. Preflight: every flag at rest (the runner never clears someone else's flags
   unless told to).
2. Before injecting: measure the alert's healthy level and, for baseline-form
   recovery checks, the recovery signal's level.
3. Inject the case's faults (eval/injector: background plan, drift detection).
4. Wait for the synthetic alert to fire (eval/runner/alerts.py). If it never
   fires, the run is an infrastructure error, not an agent failure.
5. Run the agent with the alert only, an automatic approver, a deadline and a
   budget. The deadline covers the graph's verify waits (eval/runner/timeouts.py). A run that overruns its deadline is "timeout". When a fix for an
   injected flag is approved, the injector stops changing that flag, so an
   intermittent fault cannot come back after a correct fix.
6. Judge recovery from Prometheus (eval/recovery), from the time of the fix, or
   from when the agent stopped if it did not fix anything.
7. Always stop the injector and clear every flag; save its ground-truth record.
   Flag changes that nobody planned (scheduler, someone in the UI) make the run
   "error: contaminated". The agent's own approved writes are not contamination.
8. Build the result line (eval/adapters/copilot_state.py) and score it
   (eval/checkers).

A batch (``run_batch``) writes ``results.jsonl``, ``scores.jsonl`` and
``summary.json`` under ``eval/results/runs/<batch_id>/`` (git-ignored).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import statistics
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from ..adapters.copilot_state import to_result_line
from ..checkers import score
from ..checkers.attacks import AttackReport, false_blocks, score_attack
from ..checkers.checks import CheckReport
from ..injector.injector import Injector, PreflightError
from ..recovery import check_recovery, measure_baseline
from ..adversarial.deliver import HttpAttackSender
from ..schemas.attack import Attack
from ..schemas.case import Case
from ..schemas.result import RecoveryObservation, ResultLine
from ..telemetry.prom import PromClient
from .agents import Agent, AgentContext, AgentResult, not_ready
from .alerts import measure_alert_baseline, signal_for, threshold_for, wait_for_alert
from .approvals import AutoApprover, with_gate_tokens
from .config import RunConfig
from .timeouts import GraphTiming, agent_timeout_for

RUNS_DIR = Path(__file__).resolve().parents[1] / "results" / "runs"
FLAG_WRITE_TOOLS = ("set_flag",)


@dataclasses.dataclass
class RunDeps:
    prom: PromClient
    injector: Injector
    results_dir: Path = RUNS_DIR
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    force_clear: bool = False
    make_sender: Callable[[], HttpAttackSender] = HttpAttackSender   # red team: delivers HTTP-vector attacks
    timing: GraphTiming = dataclasses.field(default_factory=GraphTiming.load)   # config/app.yaml: verify windows


CLEARED_MARKER = ".last_cleared"      # in the results folder: when the last case's flags were cleared (any process)
RATIO_SIGNALS = ("error_rate",)       # a limit of 1 or more can never be reached
AT_REST_RATIO_MAX = 0.05              # more than 5% errors before injection: an earlier fault is still in the window


def _last_cleared(results_dir: Path) -> Optional[float]:
    try:
        return float((Path(results_dir) / CLEARED_MARKER).read_text().strip())
    except (OSError, ValueError):
        return None


def _mark_cleared(results_dir: Path, t: float) -> None:
    try:
        Path(results_dir).mkdir(parents=True, exist_ok=True)
        (Path(results_dir) / CLEARED_MARKER).write_text(f"{t}\n")
    except OSError:
        pass


def latest_start(case: Case) -> float:
    """Seconds after injection when the case's last fault (or its load flood) starts; the alert can only fire
    after that, so the alert wait counts from there (D-008 and D-020 start their faults 120 s late)."""
    starts = [f.start_after_s for f in case.faults]
    if case.load.profile == "flood":
        starts.append(case.load.flood_start_after_s)
    return float(max(starts, default=0))


def unfireable(alert_base: dict) -> Optional[str]:
    """Why an alert with this baseline could never fire (the shop is not at rest), or None."""
    signal, base = alert_base["signal"], alert_base["value"]
    if signal not in RATIO_SIGNALS or base is None:
        return None
    limit, _ = threshold_for(signal, base, alert_base["rps"])
    if limit is not None and limit >= 1.0:
        return f"{signal} is {base:g} before injection, so the alert limit {limit:g} can never be reached"
    if base > AT_REST_RATIO_MAX:
        return (f"{signal} is {base:g} before injection (above {AT_REST_RATIO_MAX:g}: an earlier fault's errors are "
                f"still in the window)")
    return None


class _NotDeliverable(RuntimeError):
    pass


def _utc(t: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc)


def _stamp(t: float) -> str:
    return _utc(t).strftime("%Y%m%dT%H%M%SZ")


def approved_flag_changes(tool_calls: Optional[list]) -> set[tuple[str, str]]:
    """(flag, variant) pairs the agent itself set through executed set_flag writes."""
    out = set()
    for c in tool_calls or []:
        c = c if isinstance(c, dict) else c.model_dump()
        if (c.get("tool") in FLAG_WRITE_TOOLS and c.get("kind") == "write" and c.get("ok")
                and not c.get("blocked")):
            args = c.get("args") or {}
            if "flag" in args and "variant" in args:
                out.add((args["flag"], args["variant"]))
    return out


def unexplained_drift(record, allowed: set[tuple[str, str]]) -> list[dict]:
    """Flag changes during the run that neither the plan nor the agent's approved writes made."""
    return [d for d in record.drift if (d["flag"], d["seen"]) not in allowed]


def _run_agent(agent: Agent, ctx: AgentContext, timeout_s: float) -> tuple[AgentResult, str, Optional[str]]:
    """Run the agent in a worker thread; returns (result, status, error)."""
    box: dict = {}

    def target():
        try:
            box["result"] = agent.run(ctx)
        except BaseException as exc:  # recorded, never raised into the runner thread
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True, name=f"agent-{ctx.run_id}")
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        return AgentResult(), "timeout", f"agent did not finish within {timeout_s:g} s"
    if "error" in box:
        exc = box["error"]
        if isinstance(exc, KeyboardInterrupt):
            raise exc
        return AgentResult(), "error", f"agent raised {type(exc).__name__}: {exc}"
    result: AgentResult = box["result"]
    return result, ("budget_exhausted" if result.budget_exhausted else "completed"), None


def run_case(case: Case, agent: Agent, cfg: RunConfig, deps: RunDeps, *, repeat: int = 1,
             ) -> tuple[ResultLine, CheckReport]:
    result, report, _ = _run(case, agent, cfg, deps, repeat=repeat, attack=None)
    return result, report


def run_attack(attack: Attack, case: Case, agent: Agent, cfg: RunConfig, deps: RunDeps, *, repeat: int = 1,
               ) -> tuple[ResultLine, CheckReport, AttackReport]:
    """Run ``case`` with ``attack`` delivered during the incident; score both."""
    if attack.base_case != case.case_id:
        raise ValueError(f"{attack.attack_id} runs with {attack.base_case}, not {case.case_id}")
    result, report, delivered = _run(case, agent, cfg, deps, repeat=repeat, attack=attack)
    return result, report, score_attack(attack, case, result, delivered=delivered)


def _run(case: Case, agent: Agent, cfg: RunConfig, deps: RunDeps, *, repeat: int,
         attack: Optional[Attack]) -> tuple[ResultLine, CheckReport, bool]:
    clock, sleep = deps.clock, deps.sleep
    started = clock()
    tag = f"-{attack.attack_id}" if attack is not None else ""
    run_id = f"{cfg.config_id}-{case.case_id}{tag}-r{repeat}-{_stamp(started)}"
    sender = None
    delivered = False
    status, error = "completed", None
    agent_result = AgentResult()

    def release_fixed_flag(tool: str, args: dict) -> None:
        # An approved fix for a flag the plan controls: the plan must not switch it back on
        # (intermittent faults would otherwise return after a correct fix).
        record_now = deps.injector.record
        if tool == "set_flag" and record_now is not None and args.get("flag") in record_now.planned_flags:
            deps.injector.release(args["flag"], args.get("variant", "off"))

    approver = AutoApprover(cfg.approval, on_approved=release_fixed_flag)
    recovery = RecoveryObservation()
    record = None
    injecting = False
    rc = case.expected.recovery
    try:
        if attack is not None and not attack.deliverable:
            raise _NotDeliverable(f"attack {attack.attack_id}: vector {attack.vector} cannot be delivered by "
                                  f"the harness yet (needs a way in from its owner)")
        deps.injector.preflight(force_clear=deps.force_clear)
        last = _last_cleared(deps.results_dir)
        if last is not None and cfg.settle_s and clock() - last < cfg.settle_s:
            sleep(cfg.settle_s - (clock() - last))      # let the previous case's effect leave the windows
        for attempt in range(cfg.baseline_retries + 1):
            alert_base = measure_alert_baseline(deps.prom, case.alert, readings=cfg.baseline_readings,
                                                every_s=cfg.poll_s, clock=clock, sleep=sleep)
            not_at_rest = unfireable(alert_base)
            if not_at_rest is None:
                break
            if attempt < cfg.baseline_retries:
                sleep(cfg.settle_s or cfg.poll_s)
        if not_at_rest is not None:
            raise PreflightError(f"shop not at rest: {not_at_rest}")
        rec_base = measure_baseline(deps.prom, rc) if rc is not None else None
        agent_timeout_s = cfg.agent_timeout_s or agent_timeout_for(signal_for(case.alert.name), deps.timing,
                                                                     cfg.step_allowance_s)
        horizon = cfg.alert_timeout_s + latest_start(case) + agent_timeout_s + (rc.within_s if rc else 0) + 120
        injecting = True
        record = deps.injector.start(case, horizon, run_id)
        if attack is not None:
            sender = deps.make_sender()
            sender.start(attack)          # hostile text is in the logs and traces while the agent works
        alert, why = wait_for_alert(deps.prom, case.alert, alert_base, run_id=run_id,
                                    deadline=clock() + cfg.alert_timeout_s + latest_start(case),
                                    clock=clock, sleep=sleep,
                                    poll_s=cfg.poll_s)
        if alert is None:
            status, error = "error", why
        else:
            ctx = AgentContext(run_id=run_id, alert=alert, approve=approver, deadline=clock() + agent_timeout_s,
                               max_tool_calls=cfg.max_tool_calls, clock=clock, options=dict(cfg.options))
            try:
                agent_result, status, error = _run_agent(agent, ctx, agent_timeout_s)
            finally:
                approver.close()      # a timed-out agent thread can never get another approval
            if rc is not None and status != "error":
                fixed_at = agent_result.fixed_at if agent_result.fixed_at is not None else clock()
                recovery = check_recovery(deps.prom, rc, fixed_at, baseline=rec_base, clock=clock, sleep=sleep,
                                          poll_s=cfg.poll_s)
    except _NotDeliverable as exc:
        status, error = "error", str(exc)
    except PreflightError as exc:
        status, error = "error", f"preflight: {exc}"
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        status, error = "error", f"runner: {type(exc).__name__}: {exc}"
    finally:
        if sender is not None:
            delivered = bool(sender.stop().get("delivered"))
        if injecting and deps.injector.record is not None:
            record = deps.injector.stop()
            record.save(deps.results_dir / "ground_truth")
            _mark_cleared(deps.results_dir, clock())

    if record is not None:
        problems = []
        drift = unexplained_drift(record, approved_flag_changes(agent_result.tool_calls))
        if drift:
            problems.append("contaminated: " + ", ".join(f"{d['flag']}={d['seen']}" for d in drift))
        if record.errors:
            problems.append("injector: " + "; ".join(record.errors))
        if not record.cleared:
            problems.append("flags were not cleared after the run")
        if problems:
            status, error = "error", "; ".join(filter(None, [error, *problems]))

    result = to_result_line(
        agent_result.state, case_id=case.case_id, config_id=cfg.config_id, run_id=run_id,
        started_at=_utc(started), ended_at=_utc(clock()), status=status, error=error, repeat=repeat,
        agent_version=f"{agent.name}@{agent.version}", tool_calls=agent_result.tool_calls,
        approvals=with_gate_tokens(approver.approvals, agent_result.state), recovery=recovery.model_dump(), max_tool_calls=cfg.max_tool_calls,
        tool_log_source="agent" if agent_result.tool_calls is not None else None,
        attack_id=attack.attack_id if attack is not None else None, config_hash=cfg.config_hash,
    )
    return result, score(case, result), delivered


def summarise(cfg: RunConfig, agent: Agent, reports: list[CheckReport], results: list[ResultLine],
              cases: list[Case], attack_reports: Optional[list[AttackReport]] = None) -> dict:
    """Batch figures. Errors and timeouts stay in the denominator of task success (fail closed)."""
    n = len(reports)
    fb = [false_blocks(c, res) for c, res in zip(cases, results)]

    def rate(xs) -> Optional[float]:
        xs = list(xs)
        return round(sum(xs) / len(xs), 4) if xs else None

    incidents = [r for r, c in zip(reports, cases) if c.expected.verdict == "incident"]   # noise has no root cause
    errors: dict[str, int] = {}
    for res in results:
        if res.status != "completed":
            errors[res.status] = errors.get(res.status, 0) + 1
    return {
        "config_id": cfg.config_id,
        "agent": f"{agent.name}@{agent.version}",
        "config": cfg.model_dump(),
        "runs": n,
        "status_counts": {"completed": n - sum(errors.values()), **errors},
        "task_success_rate": rate(r.task_success for r in reports),
        "root_cause_top1": rate(r.root_cause_top1 for r in incidents),
        "root_cause_top3": rate(r.root_cause_top3 for r in incidents),
        "recovered_rate": rate(r.recovered is True for r in reports if r.recovered is not None),
        "recovery_not_judged": sum(1 for res in results if res.recovery.checked and res.recovery.recovered is None),
        "harmful_actions": sum(len(r.harmful_actions) for r in reports),
        "unnecessary_actions": sum(len(r.unnecessary_actions) for r in reports),
        "policy_violations": sum(len(r.policy_violations) for r in reports),
        "median_latency_s": (round(statistics.median(res.latency_s for res in results), 1) if results else None),
        "p95_latency_s": _p95([res.latency_s for res in results]),
        "cost_usd_per_run": (round(sum(res.cost.usd for res in results) / n, 4) if n else None),
        "tokens_per_run": (round(sum(res.cost.tokens_total for res in results) / n, 1) if n else None),
        "false_blocks": sum(len(x) for x in fb),
        "false_block_run_rate": rate(bool(x) for x in fb),
        **_attack_summary(attack_reports),
        "per_run": [{"run_id": res.run_id, "case_id": res.case_id, "repeat": res.repeat, "status": res.status,
                     "task_success": r.task_success, "recovered": r.recovered, "error": res.error}
                    for r, res in zip(reports, results)],
    }


def _p95(xs: list[float]) -> Optional[float]:
    """95th percentile (inclusive method); one run gives its own value."""
    if not xs:
        return None
    if len(xs) == 1:
        return round(xs[0], 1)
    return round(statistics.quantiles(xs, n=20, method="inclusive")[18], 1)


def _attack_summary(reports: Optional[list[AttackReport]]) -> dict:
    if not reports:
        return {}
    judged = [r for r in reports if r.succeeded is not None]
    by_goal: dict[str, dict] = {}
    for r in judged:
        g = by_goal.setdefault(r.goal, {"judged": 0, "succeeded": 0})
        g["judged"] += 1
        g["succeeded"] += int(bool(r.succeeded))
    return {
        "attacks_run": len(reports),
        "attacks_judged": len(judged),
        "attacks_not_judged": len(reports) - len(judged),
        "attack_success_rate": round(sum(bool(r.succeeded) for r in judged) / len(judged), 4) if judged else None,
        "attack_influence_rate": round(sum(r.influenced for r in judged) / len(judged), 4) if judged else None,
        "attacks_by_goal": by_goal,
    }


def run_batch(cases: list[Case], agent: Agent, cfg: RunConfig, deps: RunDeps,
              on_result: Optional[Callable[[ResultLine, CheckReport], None]] = None,
              attacks: Optional[list[Attack]] = None) -> dict:
    """Run every case (or, with ``attacks``, every attack with its base case) ``cfg.repeats`` times;
    write JSONL and a summary as it goes. Refuses to start an agent that cannot run (``not_ready``)."""
    why = not_ready(agent, options=cfg.options)
    if why:
        raise RuntimeError(f"cannot start: {why}")
    batch_id = f"{cfg.config_id}-{_stamp(deps.clock())}"
    out = deps.results_dir / batch_id
    out.mkdir(parents=True, exist_ok=True)
    by_id = {c.case_id: c for c in cases}
    if attacks:
        missing = sorted({a.base_case for a in attacks} - set(by_id))
        if missing:
            raise ValueError(f"base cases not loaded: {missing}")
        plan = [(by_id[a.base_case], a) for a in attacks]
    else:
        plan = [(c, None) for c in cases]
    results, reports, ran, attack_reports = [], [], [], []
    interrupted = False
    try:
        with (out / "results.jsonl").open("a") as rf, (out / "scores.jsonl").open("a") as sf:
            for repeat in range(1, cfg.repeats + 1):
                for case, attack in plan:
                    if attack is None:
                        result, report = run_case(case, agent, cfg, deps, repeat=repeat)
                    else:
                        result, report, ar = run_attack(attack, case, agent, cfg, deps, repeat=repeat)
                        attack_reports.append(ar)
                        with (out / "attacks.jsonl").open("a") as af:
                            af.write(ar.model_dump_json() + "\n")
                    rf.write(result.model_dump_json() + "\n")
                    sf.write(report.model_dump_json() + "\n")
                    rf.flush()
                    sf.flush()
                    results.append(result)
                    reports.append(report)
                    ran.append(case)
                    if on_result:
                        on_result(result, report)
    except KeyboardInterrupt:
        interrupted = True
        raise
    finally:
        summary = summarise(cfg, agent, reports, results, ran, attack_reports or None)
        summary.update(batch_id=batch_id, interrupted=interrupted, directory=str(out))
        (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def meets_threshold(summary: dict, min_success: float) -> bool:
    """CI gate: task success over ALL runs (errors and timeouts count as failures)."""
    rate = summary.get("task_success_rate")
    return rate is not None and rate >= min_success and not summary.get("interrupted")
