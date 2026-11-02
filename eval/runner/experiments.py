"""Run the arms of an experiment (eval/experiments/E1–E4.yaml) and keep the experiment log.

    python -m eval.runner --experiment E4 --plan                 # runs per arm, shared arms, hours
    python -m eval.runner --experiment E4 --arm memory-off       # one arm
    python -m eval.runner --experiment E1                        # every arm that is not reused

Each arm becomes one batch (``RunConfig`` with config_id ``E1-C2``, the arm's options, budget and
repeats). Held-out cases and attacks come from EVAL_PRIVATE_DIR; only counts are printed. After each
arm, one line is appended to ``experiment-log.jsonl`` in the results folder (git-ignored): what ran,
on which code and models, and its headline figures, so the report's experiment log can be written
from it.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from ..schemas.attack import Attack, load_attacks
from ..schemas.case import Case, load_cases
from ..schemas.experiment import Arm, Experiment, config_hash
from .agents import Agent
from .config import RunConfig

EVAL_DIR = Path(__file__).resolve().parents[1]
DEV_DIR = EVAL_DIR / "cases" / "dev"
APP_YAML = EVAL_DIR.parent / "config" / "app.yaml"
LOG_NAME = "experiment-log.jsonl"


def _private(private_dir: Optional[Path], sub: str) -> Path:
    """A folder under EVAL_PRIVATE_DIR; a missing one is an error, never an empty set of cases."""
    if private_dir is None:
        from ..settings import private_dir as configured
        private_dir = configured()
    folder = Path(private_dir) / sub
    if not folder.is_dir():
        raise ValueError(f"no {sub} folder at EVAL_PRIVATE_DIR/{sub}")
    return folder


def arm_inputs(exp: Experiment, arm: Arm, *, private_dir: Optional[Path] = None,
               only: Optional[list[str]] = None) -> tuple[list[Case], Optional[list[Attack]]]:
    """The arm's cases (and attacks for ``cases: attacks``), narrowed by the arm's ``only`` and then ``only``."""
    s = exp.settings(arm)
    attacks = None
    if s["cases"] == "dev":
        cases = load_cases(DEV_DIR)
    elif s["cases"] == "heldout":
        cases = load_cases(_private(private_dir, "heldout"))
    else:
        cases = load_cases(DEV_DIR)
        attacks = load_attacks(_private(private_dir, "redteam"))
    for wanted in (s["only"], only or []):
        if not wanted:
            continue
        pool = attacks if attacks is not None else cases
        known = {x.attack_id if attacks is not None else x.case_id for x in pool}
        missing = sorted(set(wanted) - known)
        if missing:
            raise ValueError(f"{exp.experiment}/{arm.id}: unknown IDs {missing}")
        if attacks is not None:
            attacks = [a for a in attacks if a.attack_id in wanted]
        else:
            cases = [c for c in cases if c.case_id in wanted]
    return cases, attacks


def arm_config(exp: Experiment, arm: Arm, **run_kw: Any) -> RunConfig:
    """The arm as a run configuration. Experiments never write lessons (review_enabled: false)."""
    s = exp.settings(arm)
    options = {"architecture": s["architecture"], "memory": s["memory"], "defences": s["defences"],
               "review_enabled": False}
    return RunConfig(config_id=f"{exp.experiment}-{arm.id}", max_tool_calls=s["max_tool_calls"],
                     repeats=s["repeats"], options=options, experiment=exp.experiment, arm=arm.id,
                     config_hash=config_hash(s), **run_kw)


def plan(exp: Experiment, *, private_dir: Optional[Path] = None, minutes_per_run: float = 20.0) -> dict:
    """Runs per arm (counts only), which arms are shared, and the shop time the new runs need."""
    rows, total = [], 0
    for arm in exp.arms:
        s = exp.settings(arm)
        try:
            cases, attacks = arm_inputs(exp, arm, private_dir=private_dir)
            n = len(attacks) if attacks is not None else len(cases)
        except (RuntimeError, OSError, ValueError) as exc:     # e.g. EVAL_PRIVATE_DIR not set
            n, why = None, str(exc).split(". ")[0].rstrip(".")
        else:
            why = None
        runs = None if n is None else n * s["repeats"]
        if arm.reuse is None and runs is not None:
            total += runs
        rows.append({"arm": arm.id, "cases": s["cases"], "items": n, "repeats": s["repeats"], "runs": runs,
                     "reuse": arm.reuse, "problem": why})
    unknown = any(r["runs"] is None and r["reuse"] is None for r in rows)
    return {"experiment": exp.experiment, "arms": rows, "new_runs": total, "complete": not unknown,
            "hours": round(total * minutes_per_run / 60, 1), "minutes_per_run": minutes_per_run}


def format_plan(p: dict) -> str:
    lines = [f"{p['experiment']}:"]
    for r in p["arms"]:
        what = f"{r['items'] if r['items'] is not None else '?'} {r['cases']} x {r['repeats']}"
        tail = f"reuses {r['reuse']}" if r["reuse"] else f"{r['runs'] if r['runs'] is not None else '?'} runs"
        lines.append(f"  {r['arm']:<16} {what:<22} {tail}" + (f"  ({r['problem']})" if r["problem"] else ""))
    at_least = "" if p["complete"] else "at least "
    lines.append(f"  new runs: {at_least}{p['new_runs']}  (~{p['hours']} h at {p['minutes_per_run']:g} min per run)")
    return "\n".join(lines)


def _git_sha() -> str:
    try:
        out = subprocess.run(["git", "-C", str(EVAL_DIR.parent), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _models() -> dict:
    try:
        return (yaml.safe_load(APP_YAML.read_text()) or {}).get("models") or {}
    except (OSError, yaml.YAMLError):
        return {}


LOGGED_FIGURES = ("runs", "status_counts", "task_success_rate", "root_cause_top1", "root_cause_top3",
                  "recovered_rate", "harmful_actions", "unnecessary_actions", "policy_violations", "false_blocks",
                  "attack_success_rate", "attack_influence_rate", "cost_usd_per_run", "median_latency_s",
                  "p95_latency_s", "interrupted")


def log_arm(results_dir: Path, exp: Experiment, arm: Arm, cfg: RunConfig, summary: dict, *,
            started: dt.datetime, partial: bool) -> dict:
    """Append one experiment-log line for a finished (or interrupted) arm."""
    entry = {"experiment": exp.experiment, "arm": arm.id, "config_id": cfg.config_id,
             "config_hash": cfg.config_hash, "settings": exp.settings(arm), "options": cfg.options,
             "batch_id": summary.get("batch_id"), "directory": summary.get("directory"),
             "agent": summary.get("agent"), "code": _git_sha(), "models": _models(),
             "started_at": started.isoformat(), "ended_at": dt.datetime.now(dt.timezone.utc).isoformat(),
             "partial": partial, **{k: summary.get(k) for k in LOGGED_FIGURES}}
    results_dir.mkdir(parents=True, exist_ok=True)
    with (results_dir / LOG_NAME).open("a") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")
    return entry


def run_experiment(exp: Experiment, arm_ids: Optional[list[str]], runner_agent: Agent, deps, *,
                   run_batch: Callable, private_dir: Optional[Path] = None, only: Optional[list[str]] = None,
                   on_result: Optional[Callable] = None, say: Callable[[str], None] = print,
                   **run_kw: Any) -> list[dict]:
    """Run the chosen arms (default: every arm that is not reused) one after another."""
    if arm_ids:
        arms = [exp.arm(i) for i in arm_ids]
        shared = [a for a in arms if a.reuse]
        if shared:
            raise ValueError(", ".join(f"{exp.experiment}/{a.id} reuses {a.reuse}: run that arm instead"
                                       for a in shared))
    else:
        arms = [a for a in exp.arms if a.reuse is None]
        for a in exp.arms:
            if a.reuse:
                say(f"{exp.experiment}/{a.id}: reuses {a.reuse}, not run here")
    inputs = [(a, *arm_inputs(exp, a, private_dir=private_dir, only=only)) for a in arms]   # fail before running
    entries = []
    for arm, cases, attacks in inputs:
        cfg = arm_config(exp, arm, **run_kw)
        what = f"{len(attacks)} attack(s)" if attacks is not None else f"{len(cases)} case(s)"
        say(f"== {cfg.config_id}: {what} x {cfg.repeats}, options {cfg.options}, budget {cfg.max_tool_calls}")
        started = dt.datetime.now(dt.timezone.utc)
        summary = None
        try:
            summary = run_batch(cases, runner_agent, cfg, deps, on_result=on_result, attacks=attacks)
        finally:
            if summary is None:
                summary = _last_summary(deps.results_dir, cfg.config_id)
            if summary is not None:
                entries.append(log_arm(deps.results_dir, exp, arm, cfg, summary, started=started,
                                       partial=bool(only) or bool(summary.get("interrupted"))))
    return entries


def _last_summary(results_dir: Path, config_id: str) -> Optional[dict]:
    """The summary run_batch wrote before it was interrupted (it always writes one)."""
    found = sorted(Path(results_dir).glob(f"{config_id}-*/summary.json"))
    if not found:
        return None
    try:
        return json.loads(found[-1].read_text())
    except (OSError, ValueError):
        return None
