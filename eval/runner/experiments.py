"""Run the arms of an experiment (eval/experiments/E1–E4.yaml) and keep the experiment log.

    python -m eval.runner --experiment E4 --plan                 # runs per arm, shared arms, hours
    python -m eval.runner --experiment E4 --arm memory-off       # one arm
    python -m eval.runner --experiment E1                        # every arm that is not reused
    python -m eval.runner --experiment E1 --rehearse             # dev rehearsal of every arm (see below)

Each arm becomes one batch (``RunConfig`` with config_id ``E1-C2``, the arm's options, budget and
repeats). Held-out cases and attacks come from EVAL_PRIVATE_DIR; only counts are printed. After each
arm, one line is appended to ``experiment-log.jsonl`` in the results folder (git-ignored): what ran,
on which code and models, and its headline figures, so the report's experiment log can be written
from it.

**Dev rehearsal** (``--rehearse``; Sprint Backlog: "E1 to E4 configs run on development cases" before the
held-out week): every arm runs once on REHEARSAL_CASES, a fixed set of dev cases covering an error fault, a
latency-free kafka lag, the two-step email fix, a catalogue fault and a noisy alert. Held-out arms move to those
dev cases, repeats become 1, sampling is dropped, and ``memory: playbook`` becomes ``on`` (an empty in-memory store: the frozen
playbook does not exist yet). Attack arms keep only attacks on those base cases. Batches are named
``E1-C2-DEV``, the log marks them ``rehearsal``, and the experiment tables leave them out unless asked for.
They show that every arm runs; they are not results.
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
from ..schemas.experiment import Arm, Experiment, config_hash, sample_cases
from .agents import Agent
from .config import RunConfig

EVAL_DIR = Path(__file__).resolve().parents[1]
DEV_DIR = EVAL_DIR / "cases" / "dev"
APP_YAML = EVAL_DIR.parent / "config" / "app.yaml"
PLAYBOOK_JSON = EVAL_DIR.parent / "docs" / "playbook" / "playbook.json"   # written by python -m copilot.reflexion freeze
LOG_NAME = "experiment-log.jsonl"
# One dev case per kind of incident the arms must handle: paymentFailure (error rate), productCatalogFailure,
# kafkaQueueProblems (consumer lag), emailMemoryLeak (two-step fix), and a noisy alert.
REHEARSAL_CASES = ("D-001", "D-005", "D-007", "D-009", "D-015")


def arm_settings(exp: Experiment, arm: Arm, rehearsal: bool = False) -> dict[str, Any]:
    """The arm's settings; for a dev rehearsal, moved to REHEARSAL_CASES (see the module docstring)."""
    s = dict(exp.settings(arm))
    if not rehearsal:
        return s
    if s["cases"] == "heldout":
        s["cases"] = "dev"
    if not s["only"]:
        s["only"] = list(REHEARSAL_CASES)
    s["repeats"] = 1
    s["sample"] = None
    if s["memory"] == "playbook":
        s["memory"] = "on"
    return s


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
               only: Optional[list[str]] = None, rehearsal: bool = False,
               ) -> tuple[list[Case], Optional[list[Attack]]]:
    """The arm's cases (and attacks for ``cases: attacks``), narrowed by the arm's ``only`` and then ``only``."""
    s = arm_settings(exp, arm, rehearsal)
    attacks = None
    if s["cases"] == "dev":
        cases = load_cases(DEV_DIR)
    elif s["cases"] == "heldout":
        cases = load_cases(_private(private_dir, "heldout"))
    else:
        cases = load_cases(DEV_DIR)
        attacks = load_attacks(_private(private_dir, "redteam"))
        if rehearsal:                                   # attacks on the rehearsal cases only
            attacks = [a for a in attacks if a.base_case in s["only"]]
            s["only"] = []
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
    if s["sample"] is not None and attacks is None:
        cases = sample_cases(cases, s["sample"]["n"], s["sample"]["seed"])
    return cases, attacks


def arm_config(exp: Experiment, arm: Arm, rehearsal: bool = False, **run_kw: Any) -> RunConfig:
    """The arm as a run configuration. Experiments never write lessons (review_enabled: false)."""
    s = arm_settings(exp, arm, rehearsal)
    options = {"architecture": s["architecture"], "memory": s["memory"], "defences": s["defences"],
               "review_enabled": False}
    suffix = "-DEV" if rehearsal else ""
    return RunConfig(config_id=f"{exp.experiment}-{arm.id}{suffix}", max_tool_calls=s["max_tool_calls"],
                     repeats=s["repeats"], options=options, experiment=exp.experiment, arm=arm.id,
                     config_hash=config_hash(s), **run_kw)


def plan(exp: Experiment, *, private_dir: Optional[Path] = None, minutes_per_run: float = 14.0,
         rehearsal: bool = False) -> dict:
    """Runs per arm (counts only), which arms are shared, and the shop time the new runs need."""
    rows, total = [], 0
    for arm in exp.arms:
        s = arm_settings(exp, arm, rehearsal)
        try:
            cases, attacks = arm_inputs(exp, arm, private_dir=private_dir, rehearsal=rehearsal)
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
    return {"experiment": exp.experiment + (" (dev rehearsal)" if rehearsal else ""), "arms": rows,
            "new_runs": total, "complete": not unknown,
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


def _playbook_hash(memory: str, path: Path = PLAYBOOK_JSON) -> Optional[str]:
    """The frozen playbook's hash for a ``memory: playbook`` arm (the report and experiment log name it); None
    otherwise, or when no playbook has been frozen yet."""
    if memory != "playbook":
        return None
    try:
        return json.loads(path.read_text()).get("playbook_hash")
    except (OSError, ValueError):
        return None


LOGGED_FIGURES = ("runs", "status_counts", "task_success_rate", "root_cause_top1", "root_cause_top3",
                  "recovered_rate", "harmful_actions", "unnecessary_actions", "policy_violations", "false_blocks",
                  "attack_success_rate", "attack_influence_rate", "cost_usd_per_run", "median_latency_s",
                  "p95_latency_s", "interrupted")


def log_arm(results_dir: Path, exp: Experiment, arm: Arm, cfg: RunConfig, summary: dict, *,
            started: dt.datetime, partial: bool, rehearsal: bool = False) -> dict:
    """Append one experiment-log line for a finished (or interrupted) arm."""
    settings = arm_settings(exp, arm, rehearsal)
    entry = {"experiment": exp.experiment, "arm": arm.id, "config_id": cfg.config_id,
             "config_hash": cfg.config_hash, "settings": settings, "options": cfg.options,
             "playbook_hash": _playbook_hash(settings["memory"]),
             "rehearsal": rehearsal,
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
                   rehearsal: bool = False, **run_kw: Any) -> list[dict]:
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
    inputs = [(a, *arm_inputs(exp, a, private_dir=private_dir, only=only, rehearsal=rehearsal))
              for a in arms]                                                            # fail before running
    entries = []
    for arm, cases, attacks in inputs:
        cfg = arm_config(exp, arm, rehearsal=rehearsal, **run_kw)
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
                                       partial=bool(only) or bool(summary.get("interrupted")),
                                       rehearsal=rehearsal))
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
