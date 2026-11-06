"""Tables for the evaluation report, from the runner's batches (capstone guide p. 6 and Project 4, p. 14).

    python -m eval.report                                   # every batch under eval/results/runs
    python -m eval.report --batch DEV-C2-20261104T150000Z   # one batch (or several, comma-separated)
    python -m eval.report --experiments                     # E1-E4 comparisons from the experiment log

For every configuration (guide p. 6): task success, policy violations, attack success, false blocks, cost per
case, median and 95th-percentile latency, errors (failed and timed-out runs stay in every denominator), and the
spread across repeats. Project 4 (p. 14): root cause top-1 and top-3 by service and fault type, time to
diagnose, tool calls, correct action proposed, verified recovery, harmful and unnecessary actions.

Only aggregates are printed, never case contents, so tables over held-out runs reveal nothing about the cases.

Times are measured from the run's own timeline, from the moment the agent received the alert (intake):
- time to diagnose: the commander converging on a hypothesis (incidents), or triage calling it noise;
- time to fix: the first executed action;
- time to recover: the independent recovery check's reading (eval/recovery), only when it recovered.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

from ..checkers.attacks import AttackReport, false_blocks
from ..checkers.checks import CheckReport
from ..schemas.case import Case, load_cases
from ..schemas.result import ResultLine

RUNS_DIR = Path(__file__).resolve().parents[1] / "results" / "runs"


@dataclasses.dataclass
class Run:
    result: ResultLine
    score: CheckReport
    case: Optional[Case] = None
    attack: Optional[AttackReport] = None

    @property
    def is_incident(self) -> bool:
        return self.case is not None and self.case.expected.verdict == "incident"


@dataclasses.dataclass
class Batch:
    batch_id: str
    config_id: str
    runs: list[Run]
    summary: dict


# ---------------------------------------------------------------- loading

def _jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def case_index(folders: Iterable[Path]) -> dict[str, Case]:
    out: dict[str, Case] = {}
    for f in folders:
        if f is not None and Path(f).is_dir():
            out.update({c.case_id: c for c in load_cases(f)})
    return out


def load_batch(folder: Path, cases: dict[str, Case]) -> Batch:
    folder = Path(folder)
    results = [ResultLine.model_validate(r) for r in _jsonl(folder / "results.jsonl")]
    scores = {s["run_id"]: CheckReport.model_validate(s) for s in _jsonl(folder / "scores.jsonl")}
    attacks = {a["run_id"]: AttackReport.model_validate(a) for a in _jsonl(folder / "attacks.jsonl")}
    summary = json.loads((folder / "summary.json").read_text()) if (folder / "summary.json").is_file() else {}
    runs = [Run(r, scores[r.run_id], cases.get(r.case_id), attacks.get(r.run_id))
            for r in results if r.run_id in scores]
    config_id = summary.get("config_id") or (results[0].config_id if results else folder.name)
    return Batch(folder.name, config_id, runs, summary)


def find_batches(root: Path = RUNS_DIR, ids: Optional[list[str]] = None) -> list[Path]:
    root = Path(root)
    found = sorted(p for p in root.iterdir() if p.is_dir() and (p / "results.jsonl").is_file()) if root.is_dir() else []
    if ids:
        missing = sorted(set(ids) - {p.name for p in found})
        if missing:
            raise ValueError(f"batches not found under {root}: {missing}")
        found = [p for p in found if p.name in ids]
    return found


# ---------------------------------------------------------------- per-run measures

def _parse(at: Any) -> Optional[dt.datetime]:
    if isinstance(at, dt.datetime):
        return at
    try:
        return dt.datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _timeline(r: ResultLine) -> list[tuple[dt.datetime, str, str]]:
    events = (r.explanation.timeline if r.explanation else []) or []
    out = [(_parse(e.at), e.node, e.text or "") for e in events]
    return sorted((t, n, x) for t, n, x in out if t is not None)


def times(r: ResultLine) -> dict[str, Optional[float]]:
    """Seconds from intake to diagnosis, first executed fix, and independently observed recovery."""
    tl = _timeline(r)
    t0 = next((t for t, n, _ in tl if n == "intake"), None)
    if t0 is None:
        return {"diagnose_s": None, "fix_s": None, "recover_s": None}

    def first(pred) -> Optional[float]:
        t = next((t for t, n, x in tl if pred(n, x)), None)
        return None if t is None else round((t - t0).total_seconds(), 1)

    diagnose = first(lambda n, x: (n == "commander_assess" and x.startswith("converge"))
                     or (n == "triage" and x.startswith("noise")))
    fix = first(lambda n, x: n == "execute" and x.startswith("Executed"))
    rec = r.recovery
    recover = None
    if rec.recovered is True and rec.observed_at is not None:
        recover = round(rec.observed_at - t0.timestamp(), 1)
    return {"diagnose_s": diagnose, "fix_s": fix, "recover_s": recover}


# ---------------------------------------------------------------- aggregates

def _rate(xs: list[bool]) -> Optional[float]:
    return round(sum(xs) / len(xs), 3) if xs else None


def _median(xs: list[float]) -> Optional[float]:
    return round(statistics.median(xs), 1) if xs else None


def _p95(xs: list[float]) -> Optional[float]:
    if not xs:
        return None
    if len(xs) == 1:
        return round(xs[0], 1)
    return round(statistics.quantiles(xs, n=20, method="inclusive")[18], 1)


def metrics(runs: list[Run]) -> dict[str, Any]:
    """Every figure the guide asks for, over ``runs`` (errors and timeouts included in every denominator)."""
    n = len(runs)
    inc = [r for r in runs if r.is_incident]
    errors: dict[str, int] = defaultdict(int)
    for r in runs:
        if r.result.status != "completed":
            errors[r.result.status] += 1
    judged = [r.attack for r in runs if r.attack is not None and r.attack.succeeded is not None]
    t = [times(r.result) for r in runs]
    fb = [false_blocks(r.case, r.result) if r.case is not None else [] for r in runs]
    recovery_checked = [r.score.recovered for r in runs if r.score.recovered is not None]
    return {
        "runs": n,
        "task_success": sum(r.score.task_success for r in runs),
        "task_success_rate": _rate([r.score.task_success for r in runs]),
        "errors": dict(errors),
        "policy_violations": sum(len(r.score.policy_violations) for r in runs),
        "harmful_actions": sum(len(r.score.harmful_actions) for r in runs),
        "unnecessary_actions": sum(len(r.score.unnecessary_actions) for r in runs),
        "root_cause_top1": _rate([r.score.root_cause_top1 for r in inc]),
        "root_cause_top3": _rate([r.score.root_cause_top3 for r in inc]),
        "root_cause_service_top1": _rate([r.score.root_cause_service_top1 for r in inc]),
        "action_proposed_correct": _rate([r.score.action_proposed_correct for r in inc]),
        "verified_recovery": _rate(recovery_checked),
        "recovery_not_judged": sum(1 for r in runs if r.result.recovery.checked and r.result.recovery.recovered is None),
        "self_rollbacks": sum(r.score.self_rollbacks for r in runs),
        "loops": sum(r.score.loops_detected for r in runs),
        "median_tool_calls": _median([r.score.tool_calls_used for r in runs]),
        "median_time_to_diagnose_s": _median([x["diagnose_s"] for x in t if x["diagnose_s"] is not None]),
        "diagnosed_runs": sum(1 for x in t if x["diagnose_s"] is not None),
        "median_time_to_recover_s": _median([x["recover_s"] for x in t if x["recover_s"] is not None]),
        "cost_usd_per_case": round(sum(r.result.cost.usd for r in runs) / n, 4) if n else None,
        "cost_usd_total": round(sum(r.result.cost.usd for r in runs), 4),
        "median_latency_s": _median([r.result.latency_s for r in runs]),
        "p95_latency_s": _p95([r.result.latency_s for r in runs]),
        "false_blocks": sum(len(x) for x in fb),
        "false_block_run_rate": _rate([bool(x) for x in fb]),
        "attacks_run": sum(1 for r in runs if r.attack is not None),
        "attacks_judged": len(judged),
        "attack_success_rate": _rate([bool(a.succeeded) for a in judged]),
        "attack_influence_rate": _rate([a.influenced for a in judged]),
    }


def spread(runs: list[Run]) -> Optional[dict[str, Any]]:
    """Task-success rate per repeat and its spread (guide: repeat the final held-out comparison three times)."""
    by_rep: dict[int, list[bool]] = defaultdict(list)
    for r in runs:
        by_rep[r.result.repeat].append(r.score.task_success)
    if len(by_rep) < 2:
        return None
    rates = [round(sum(v) / len(v), 3) for _, v in sorted(by_rep.items())]
    return {"repeats": len(rates), "per_repeat": rates, "min": min(rates), "max": max(rates),
            "mean": round(statistics.mean(rates), 3), "stdev": round(statistics.stdev(rates), 3)}


def _primary(case: Optional[Case], attr: str) -> str:
    if case is None:
        return "unknown"
    if case.expected.verdict == "noise":
        return "noise"
    values = sorted({getattr(rc, attr) for rc in case.expected.root_causes})
    return "+".join(values) or "unknown"


def by_group(runs: list[Run], attr: str) -> dict[str, dict[str, Any]]:
    """Diagnosis and action figures per root-cause fault type or service (two-fault cases: "a+b")."""
    groups: dict[str, list[Run]] = defaultdict(list)
    for r in runs:
        groups[_primary(r.case, attr)].append(r)
    out = {}
    for key, rs in sorted(groups.items()):
        m = metrics(rs)
        out[key] = {k: m[k] for k in ("runs", "task_success", "root_cause_top1", "root_cause_top3",
                                      "action_proposed_correct", "verified_recovery", "harmful_actions",
                                      "unnecessary_actions", "median_time_to_diagnose_s")}
    return out


def attacks_by_goal(runs: list[Run]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(lambda: {"run": 0, "judged": 0, "succeeded": 0, "influenced": 0})
    for r in runs:
        a = r.attack
        if a is None:
            continue
        g = out[a.goal]
        g["run"] += 1
        if a.succeeded is not None:
            g["judged"] += 1
            g["succeeded"] += int(bool(a.succeeded))
            g["influenced"] += int(a.influenced)
    return dict(sorted(out.items()))


def config_report(config_id: str, runs: list[Run]) -> dict[str, Any]:
    return {"config_id": config_id, "metrics": metrics(runs), "spread": spread(runs),
            "by_fault_type": by_group(runs, "fault_type"), "by_service": by_group(runs, "service"),
            "attacks_by_goal": attacks_by_goal(runs)}


def by_config(batches: list[Batch]) -> dict[str, list[Run]]:
    out: dict[str, list[Run]] = defaultdict(list)
    for b in batches:
        out[b.config_id].extend(b.runs)
    return dict(out)


# ---------------------------------------------------------------- experiments (E1-E4)

def experiment_runs(exp, log: list[dict], cases: dict[str, Case],
                    rehearsal: bool = False) -> dict[str, Optional[list[Run]]]:
    """Runs for every arm of ``exp``: the latest complete (not partial) batch logged for it, or for the arm it
    reuses; a reusing arm takes only its own number of repeats. None when the arm has not run."""
    latest: dict[tuple[str, str], dict] = {}
    for e in log:
        if not e.get("partial") and e.get("directory") and bool(e.get("rehearsal")) == rehearsal:
            latest[(e["experiment"], e["arm"])] = e
    out: dict[str, Optional[list[Run]]] = {}
    for arm in exp.arms:
        src = tuple(arm.reuse.split("/")) if arm.reuse else (exp.experiment, arm.id)
        entry = latest.get(src)
        if entry is None or not Path(entry["directory"]).is_dir():
            out[arm.id] = None
            continue
        runs = load_batch(Path(entry["directory"]), cases).runs
        repeats = exp.settings(arm)["repeats"]
        out[arm.id] = [r for r in runs if r.result.repeat <= repeats]
    return out


def lesson_effects(before: list[Run], after: list[Run]) -> dict[str, Any]:
    """E3 (guide p. 6, "did any lesson make results worse?"): every lesson the ``after`` arm used or rejected, with
    the runs that used it and how many of them succeeded, and the runs that got worse: the case succeeded in every
    ``before`` run and failed here. Only lesson IDs and counts, never case IDs."""
    passed_before: dict[str, bool] = {}
    for r in before:
        ok = bool(r.score.task_success)
        passed_before[r.result.case_id] = passed_before.get(r.result.case_id, True) and ok
    used: dict[str, list[bool]] = defaultdict(list)
    rejected: dict[str, int] = defaultdict(int)
    worse = worse_with_lessons = 0
    worse_lessons: dict[str, int] = defaultdict(int)
    for r in after:
        ok = bool(r.score.task_success)
        for lesson in r.result.lessons_used:
            used[lesson].append(ok)
        for lesson in r.result.lessons_rejected:
            rejected[lesson] += 1
        if passed_before.get(r.result.case_id) and not ok:
            worse += 1
            if r.result.lessons_used:
                worse_with_lessons += 1
            for lesson in r.result.lessons_used:
                worse_lessons[lesson] += 1
    lessons = [{"lesson": lid, "runs_used": len(used.get(lid, [])), "task_success": sum(used.get(lid, [])),
                "runs_rejected": rejected.get(lid, 0), "in_worse_runs": worse_lessons.get(lid, 0)}
               for lid in sorted(set(used) | set(rejected))]
    return {"lessons": lessons, "worse_runs": worse, "worse_runs_with_lessons": worse_with_lessons}


def _lessons_markdown(exp, arms: list[str], arm_runs: dict[str, Optional[list[Run]]]) -> list[str]:
    """For a memory comparison of two arms (one with memory off): which lessons the other arm used or rejected."""
    if len(arms) != 2:
        return []
    off = [a for a in arms if exp.settings(exp.arm(a))["memory"] == "off"]
    if len(off) != 1:
        return []
    before, after = off[0], next(a for a in arms if a != off[0])
    if arm_runs.get(before) is None or arm_runs.get(after) is None:
        return []
    fx = lesson_effects(arm_runs[before], arm_runs[after])
    rows = [[x["lesson"], x["runs_used"], x["task_success"], x["runs_rejected"], x["in_worse_runs"]]
            for x in fx["lessons"]]
    out = [f"Lessons in {after} (against {before}): {fx['worse_runs']} run(s) failed on a case that succeeded "
           f"in {before}; {fx['worse_runs_with_lessons']} of them used a lesson.", ""]
    if rows:
        out += [_table(["Lesson", "Runs used", "Task success", "Runs rejected (preconditions)", "Used in worse runs"],
                       rows), ""]
    else:
        out += ["No lesson was used or rejected.", ""]
    return out


# ---------------------------------------------------------------- markdown

def _fmt(v: Any) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".") if abs(v) < 10 else f"{v:.1f}"
    if isinstance(v, dict):
        return ", ".join(f"{k} {x}" for k, x in v.items()) or "0"
    return str(v)


def _table(header: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_fmt(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


MAIN_COLUMNS = [("runs", "Runs"), ("task_success", "Task success"), ("task_success_rate", "Rate"),
                ("policy_violations", "Policy violations"), ("attack_success_rate", "Attack success"),
                ("false_blocks", "False blocks"), ("cost_usd_per_case", "Cost/case ($)"),
                ("median_latency_s", "Median latency (s)"), ("p95_latency_s", "p95 latency (s)"),
                ("errors", "Errors")]
P4_COLUMNS = [("root_cause_top1", "Root cause top-1"), ("root_cause_top3", "Top-3"),
              ("action_proposed_correct", "Correct action"), ("verified_recovery", "Verified recovery"),
              ("harmful_actions", "Harmful"), ("unnecessary_actions", "Unnecessary"),
              ("median_time_to_diagnose_s", "Time to diagnose (s, median)"),
              ("median_time_to_recover_s", "Time to recover (s, median)"),
              ("median_tool_calls", "Tool calls (median)"), ("self_rollbacks", "Rollbacks")]
GROUP_COLUMNS = [("runs", "Runs"), ("root_cause_top1", "Top-1"), ("root_cause_top3", "Top-3"),
                 ("action_proposed_correct", "Correct action"), ("verified_recovery", "Recovered"),
                 ("harmful_actions", "Harmful"), ("unnecessary_actions", "Unnecessary"),
                 ("median_time_to_diagnose_s", "Diagnose (s)")]


def configs_markdown(reports: list[dict]) -> str:
    parts = ["## Every configuration (guide p. 6)", "",
             _table(["Config"] + [h for _, h in MAIN_COLUMNS],
                    [[r["config_id"]] + [r["metrics"][k] for k, _ in MAIN_COLUMNS] for r in reports]), "",
             "## Project 4 metrics (guide p. 14)", "",
             _table(["Config"] + [h for _, h in P4_COLUMNS],
                    [[r["config_id"]] + [r["metrics"][k] for k, _ in P4_COLUMNS] for r in reports])]
    spreads = [r for r in reports if r["spread"]]
    if spreads:
        parts += ["", "## Spread across repeats", "",
                  _table(["Config", "Repeats", "Task success per repeat", "Min", "Max", "Mean", "Std dev"],
                         [[r["config_id"], s["repeats"], " / ".join(_fmt(x) for x in s["per_repeat"]), s["min"],
                           s["max"], s["mean"], s["stdev"]] for r in spreads for s in [r["spread"]]])]
    for r in reports:
        parts += ["", f"### {r['config_id']}: by fault type", "",
                  _table(["Fault type"] + [h for _, h in GROUP_COLUMNS],
                         [[k] + [v[c] for c, _ in GROUP_COLUMNS] for k, v in r["by_fault_type"].items()]),
                  "", f"### {r['config_id']}: by service", "",
                  _table(["Service"] + [h for _, h in GROUP_COLUMNS],
                         [[k] + [v[c] for c, _ in GROUP_COLUMNS] for k, v in r["by_service"].items()])]
        if r["attacks_by_goal"]:
            parts += ["", f"### {r['config_id']}: attacks by goal", "",
                      _table(["Goal", "Run", "Judged", "Succeeded", "Influenced"],
                             [[g, v["run"], v["judged"], v["succeeded"], v["influenced"]]
                              for g, v in r["attacks_by_goal"].items()])]
    return "\n".join(parts) + "\n"


def experiments_markdown(exps: dict, log: list[dict], cases: dict[str, Case], rehearsal: bool = False) -> str:
    parts = ["## Experiments" + (" (dev rehearsal: shows the arms run; not results)" if rehearsal else ""), ""]
    for name, exp in sorted(exps.items()):
        arm_runs = experiment_runs(exp, log, cases, rehearsal)
        parts += [f"### {name}: {exp.question}", ""]
        rows = []
        for arm in exp.arms:
            runs = arm_runs[arm.id]
            label = arm.id + (f" (= {arm.reuse})" if arm.reuse else "")
            if runs is None:
                rows.append([label, "not run"] + [None] * (len(MAIN_COLUMNS) + len(P4_COLUMNS) - 1))
                continue
            m = metrics(runs)
            rows.append([label] + [m[k] for k, _ in MAIN_COLUMNS] + [m[k] for k, _ in P4_COLUMNS])
        parts += [_table(["Arm"] + [h for _, h in MAIN_COLUMNS] + [h for _, h in P4_COLUMNS], rows), ""]
        for c in exp.compare:
            parts.append(f"Compared: {', '.join(c.arms)} (varies {', '.join(c.varies)}).")
        parts.append("")
        for c in exp.compare:
            if list(c.varies) == ["memory"]:
                parts += _lessons_markdown(exp, list(c.arms), arm_runs)
    return "\n".join(parts)


def experiment_log_markdown(log: list[dict]) -> str:
    """The experiment log (a required deliverable, guide p. 6): every arm that ran, in order, with what it ran on
    and its headline figures. Partial runs (narrowed with --only, or interrupted) are marked."""
    rows = []
    for e in log:
        rows.append([(e.get("started_at") or "")[:16].replace("T", " "), e.get("experiment"), e.get("arm"),
                     e.get("batch_id"), e.get("agent"), e.get("code"), e.get("config_hash"),
                     (e.get("playbook_hash") or "")[:12] or None,
                     e.get("runs"), e.get("task_success_rate"), e.get("status_counts"), e.get("cost_usd_per_run"),
                     ("rehearsal, " if e.get("rehearsal") else "") + ("yes" if e.get("partial") else "no")])
    models = sorted({f"{k}: {v.get('model')}" for e in log for k, v in (e.get("models") or {}).items()
                     if isinstance(v, dict)})
    parts = ["## Experiment log", "",
             _table(["Started (UTC)", "Exp", "Arm", "Batch", "Agent", "Code", "Config hash", "Playbook", "Runs",
                     "Task success", "Status", "Cost/run ($)", "Partial"], rows)]
    if models:
        parts += ["", "Models (config/app.yaml): " + "; ".join(models) + "."]
    return "\n".join(parts) + "\n"
