"""Report tables: every figure the guide asks for, from real runner batches (fake shop, test agents)."""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest

from eval.report import (by_config, case_index, config_report, configs_markdown, experiments_markdown, find_batches,
                         lesson_effects, load_batch, metrics, spread, times)
from eval.report.__main__ import main
from eval.runner import DoNothingAgent, run_batch
from eval.runner.experiments import run_experiment
from eval.schemas.case import load_case
from eval.schemas.experiment import load_experiments
from eval.tests.test_experiments import dev_experiments
from eval.tests.test_runner import DEV, FixingAgent, cfg, make

CASES = case_index([DEV])


class TimedFixer(FixingAgent):
    """FixingAgent whose final state has the graph's timeline (intake -> converge -> execute) and a status note."""

    name = "timed-fixer"

    def run(self, ctx):
        res = super().run(ctx)
        t0 = ctx.clock()

        def at(s):
            return dt.datetime.fromtimestamp(t0 + s, dt.timezone.utc).isoformat()

        res.state["timeline"] = [
            {"at": at(0), "node": "intake", "kind": "plan", "text": "Alert ..."},
            {"at": at(95), "node": "commander_assess", "kind": "verdict", "text": "converge: payment/paymentFailure"},
            {"at": at(130), "node": "execute", "kind": "action", "text": "Executed: set flag paymentFailure to off"},
        ]
        res.state["status_note"] = {"text": "Impact: ...", "evidence_ids": []}
        return res


def batch(tmp_path, agent_factory, config_id="T", repeats=1, only=("D-001",)):
    store, deps = make(tmp_path)
    cases = [load_case(DEV / f"{c}.yaml") for c in only]
    summary = run_batch(cases, agent_factory(store), cfg(config_id=config_id, repeats=repeats), deps)
    return load_batch(summary["directory"], CASES)


def test_metrics_from_a_successful_run(tmp_path):
    b = batch(tmp_path, TimedFixer)
    m = metrics(b.runs)
    assert m["runs"] == 1 and m["task_success"] == 1 and m["errors"] == {}
    assert m["root_cause_top1"] == 1.0 and m["verified_recovery"] == 1.0
    assert m["action_proposed_correct"] is not None      # the test agent keeps no proposal in its state
    assert m["harmful_actions"] == m["unnecessary_actions"] == m["policy_violations"] == 0
    assert m["median_time_to_diagnose_s"] == 95.0 and m["diagnosed_runs"] == 1
    assert m["median_latency_s"] is not None and m["p95_latency_s"] == m["median_latency_s"]
    t = times(b.runs[0].result)
    assert t["diagnose_s"] == 95.0 and t["fix_s"] == 130.0 and t["recover_s"] is not None


def test_failures_and_errors_stay_in_every_denominator(tmp_path):
    b = batch(tmp_path, lambda store: DoNothingAgent(), only=("D-001", "D-015"))
    m = metrics(b.runs)
    assert m["runs"] == 2 and m["task_success_rate"] is not None
    assert m["root_cause_top1"] == 0.0                     # only the incident case counts for root cause
    assert m["median_time_to_diagnose_s"] is None and m["diagnosed_runs"] == 0


def test_spread_across_repeats(tmp_path):
    b = batch(tmp_path, TimedFixer, repeats=2)
    s = spread(b.runs)
    assert s["repeats"] == 2 and s["per_repeat"] == [1.0, 1.0] and s["stdev"] == 0.0
    assert spread(b.runs[:1]) is None


def test_by_fault_type_and_service_groups(tmp_path):
    b = batch(tmp_path, lambda store: DoNothingAgent(), only=("D-001", "D-015"))
    r = config_report("T", b.runs)
    assert set(r["by_fault_type"]) == {"paymentFailure", "noise"}
    assert set(r["by_service"]) == {"payment", "noise"}
    text = configs_markdown([r])
    for heading in ("Every configuration", "Project 4 metrics", "by fault type", "by service"):
        assert heading in text
    assert "paymentFailure" in text


def test_unknown_case_counts_but_is_marked(tmp_path):
    b = batch(tmp_path, TimedFixer)
    unknown = load_batch(tmp_path / b.batch_id, {})          # no case files: held-out folder not configured
    m = metrics(unknown.runs)
    assert m["runs"] == 1 and m["root_cause_top1"] is None
    assert set(config_report("T", unknown.runs)["by_fault_type"]) == {"unknown"}


def test_batches_group_by_config_and_unknown_ids_are_refused(tmp_path):
    batch(tmp_path, TimedFixer, config_id="A")
    batch(tmp_path, TimedFixer, config_id="B")
    folders = find_batches(tmp_path)
    assert len(folders) == 2
    groups = by_config([load_batch(f, CASES) for f in folders])
    assert sorted(groups) == ["A", "B"]
    with pytest.raises(ValueError, match="not found"):
        find_batches(tmp_path, ["nope"])


def test_experiment_tables_use_the_log_and_shared_arms(tmp_path):
    store, deps = make(tmp_path)
    exps = load_experiments(dev_experiments(tmp_path))
    run_experiment(exps["E1"], None, DoNothingAgent(), deps, run_batch=run_batch, say=lambda s: None,
                   alert_timeout_s=300)
    log = [json.loads(x) for x in (tmp_path / "experiment-log.jsonl").read_text().splitlines()]
    text = experiments_markdown(exps, log, CASES)
    assert "### E1" in text and "| C0 |" in text and "| C2 |" in text
    assert "on (= E1/C2)" in text                          # E4's shared arm is filled from E1's batch
    assert "| b5 | not run |" in text


def test_cli_writes_markdown_and_json(tmp_path):
    batch(tmp_path, TimedFixer, config_id="A")
    out, js = tmp_path / "tables.md", tmp_path / "tables.json"
    assert main(["--runs", str(tmp_path), "--out", str(out), "--json", str(js)]) == 0
    assert "| A |" in out.read_text()
    assert json.loads(js.read_text())[0]["config_id"] == "A"
    assert main(["--runs", str(tmp_path / "empty")]) == 2


def test_experiment_log_page(tmp_path, capsys):
    store, deps = make(tmp_path)
    exps = load_experiments(dev_experiments(tmp_path))
    run_experiment(exps["E1"], None, DoNothingAgent(), deps, run_batch=run_batch, say=lambda s: None,
                   alert_timeout_s=300)
    assert main(["--runs", str(tmp_path), "--log"]) == 0
    out = capsys.readouterr().out
    assert "## Experiment log" in out and "| E1 | C0 |" in out and "| E1 | C2 |" in out and "| no |" in out
    assert main(["--runs", str(tmp_path / "none"), "--log"]) == 2


def _run(case_id, ok, used=(), rejected=()):
    return SimpleNamespace(result=SimpleNamespace(case_id=case_id, lessons_used=list(used),
                                                  lessons_rejected=list(rejected)),
                           score=SimpleNamespace(task_success=ok))


def test_lesson_effects_name_the_lessons_in_runs_that_got_worse():
    before = [_run("H-1", True), _run("H-2", True), _run("H-3", False)]
    after = [_run("H-1", False, used=["L-a"]), _run("H-2", False), _run("H-3", True, used=["L-a", "L-b"],
                                                                         rejected=["PM-x"])]
    fx = lesson_effects(before, after)
    assert fx["worse_runs"] == 2 and fx["worse_runs_with_lessons"] == 1
    rows = {x["lesson"]: x for x in fx["lessons"]}
    assert rows["L-a"] == {"lesson": "L-a", "runs_used": 2, "task_success": 1, "runs_rejected": 0, "in_worse_runs": 1}
    assert rows["L-b"]["in_worse_runs"] == 0 and rows["PM-x"]["runs_rejected"] == 1 and rows["PM-x"]["runs_used"] == 0
