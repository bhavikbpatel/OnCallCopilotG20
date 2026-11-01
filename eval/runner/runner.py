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
   budget. A run that overruns its deadline is "timeout".
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
from ..checkers.checks import CheckReport
from ..injector.injector import Injector, PreflightError
from ..recovery import check_recovery, measure_baseline
from ..schemas.case import Case
from ..schemas.result import RecoveryObservation, ResultLine
from ..telemetry.prom import PromClient
from .agents import Agent, AgentContext, AgentResult
from .alerts import measure_alert_baseline, wait_for_alert
from .approvals import AutoApprover
from .config import RunConfig

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
    clock, sleep = deps.clock, deps.sleep
    started = clock()
    run_id = f"{cfg.config_id}-{case.case_id}-r{repeat}-{_stamp(started)}"
    status, error = "completed", None
    agent_result = AgentResult()
    approver = AutoApprover(cfg.approval)
    recovery = RecoveryObservation()
    record = None
    injecting = False
    rc = case.expected.recovery
    try:
        deps.injector.preflight(force_clear=deps.force_clear)
        alert_base = measure_alert_baseline(deps.prom, case.alert, readings=cfg.baseline_readings,
                                            every_s=cfg.poll_s, clock=clock, sleep=sleep)
        rec_base = measure_baseline(deps.prom, rc) if rc is not None else None
        horizon = cfg.alert_timeout_s + cfg.agent_timeout_s + (rc.within_s if rc else 0) + 120
        injecting = True
        record = deps.injector.start(case, horizon, run_id)
        alert, why = wait_for_alert(deps.prom, case.alert, alert_base, run_id=run_id,
                                    deadline=clock() + cfg.alert_timeout_s, clock=clock, sleep=sleep,
                                    poll_s=cfg.poll_s)
        if alert is None:
            status, error = "error", why
        else:
            ctx = AgentContext(run_id=run_id, alert=alert, approve=approver, deadline=clock() + cfg.agent_timeout_s,
                               max_tool_calls=cfg.max_tool_calls, clock=clock)
            try:
                agent_result, status, error = _run_agent(agent, ctx, cfg.agent_timeout_s)
            finally:
                approver.close()      # a timed-out agent thread can never get another approval
            if rc is not None and status != "error":
                fixed_at = agent_result.fixed_at if agent_result.fixed_at is not None else clock()
                recovery = check_recovery(deps.prom, rc, fixed_at, baseline=rec_base, clock=clock, sleep=sleep,
                                          poll_s=cfg.poll_s)
    except PreflightError as exc:
        status, error = "error", f"preflight: {exc}"
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        status, error = "error", f"runner: {type(exc).__name__}: {exc}"
    finally:
        if injecting and deps.injector.record is not None:
            record = deps.injector.stop()
            record.save(deps.results_dir / "ground_truth")

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
        approvals=approver.approvals, recovery=recovery.model_dump(), max_tool_calls=cfg.max_tool_calls,
        tool_log_source="agent" if agent_result.tool_calls is not None else None,
    )
    return result, score(case, result)


def summarise(cfg: RunConfig, agent: Agent, reports: list[CheckReport], results: list[ResultLine],
              cases: list[Case]) -> dict:
    """Batch figures. Errors and timeouts stay in the denominator of task success (fail closed)."""
    n = len(reports)

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
        "per_run": [{"run_id": res.run_id, "case_id": res.case_id, "repeat": res.repeat, "status": res.status,
                     "task_success": r.task_success, "recovered": r.recovered, "error": res.error}
                    for r, res in zip(reports, results)],
    }


def run_batch(cases: list[Case], agent: Agent, cfg: RunConfig, deps: RunDeps,
              on_result: Optional[Callable[[ResultLine, CheckReport], None]] = None) -> dict:
    """Run every case ``cfg.repeats`` times; write JSONL and a summary as it goes."""
    batch_id = f"{cfg.config_id}-{_stamp(deps.clock())}"
    out = deps.results_dir / batch_id
    out.mkdir(parents=True, exist_ok=True)
    results, reports, ran = [], [], []
    interrupted = False
    try:
        with (out / "results.jsonl").open("a") as rf, (out / "scores.jsonl").open("a") as sf:
            for repeat in range(1, cfg.repeats + 1):
                for case in cases:
                    result, report = run_case(case, agent, cfg, deps, repeat=repeat)
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
        summary = summarise(cfg, agent, reports, results, ran)
        summary.update(batch_id=batch_id, interrupted=interrupted, directory=str(out))
        (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def meets_threshold(summary: dict, min_success: float) -> bool:
    """CI gate: task success over ALL runs (errors and timeouts count as failures)."""
    rate = summary.get("task_success_rate")
    return rate is not None and rate >= min_success and not summary.get("interrupted")
