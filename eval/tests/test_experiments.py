"""Experiments E1–E4: the files load, comparisons are fair, shared arms are identical, and the runner runs
arms with their options and writes the experiment log."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from eval.runner import DoNothingAgent, run_batch
from eval.runner.__main__ import main
from eval.runner.agents import not_ready
from eval.runner.experiments import LOG_NAME, arm_config, arm_inputs, format_plan, plan, run_experiment
from eval.schemas.experiment import EXPERIMENTS_DIR, Experiment, load_experiment, load_experiments, resolve
from eval.tests.test_runner import DEV, all_at_rest, make

# ---------------------------------------------------------------- the repo's experiment files


def test_repo_experiments_load_and_match_the_guide():
    exps = load_experiments()
    assert sorted(exps) == ["E1", "E2", "E3", "E4"]
    e1 = exps["E1"]
    assert {e1.settings(a)["architecture"] for a in e1.arms} == {"C0", "C1", "C2"}
    assert all(e1.settings(a)["repeats"] == 3 and e1.settings(a)["cases"] == "heldout" for a in e1.arms)
    e2 = exps["E2"]
    assert {(e2.settings(a)["cases"], e2.settings(a)["defences"]) for a in e2.arms} == {
        ("attacks", "off"), ("attacks", "on"), ("dev", "off"), ("dev", "on")}
    assert all(a.reuse for a in exps["E3"].arms)                        # E3 adds no runs of its own
    e4 = exps["E4"]
    assert {e4.settings(a)["max_tool_calls"] for a in e4.arms} == {5, 10, 20}
    misleading = [a for a in e4.arms if e4.settings(a)["only"] == ["D-011"]]
    assert {e4.settings(a)["memory"] for a in misleading} == {"playbook", "off"}
    assert (DEV / "D-011.yaml").is_file()


def test_shared_arms_point_at_arms_that_run():
    exps = load_experiments()
    for e in exps.values():
        for a in e.arms:
            if a.reuse:
                target_exp, _, target_arm = a.reuse.partition("/")
                assert exps[target_exp].arm(target_arm).reuse is None


# ---------------------------------------------------------------- fairness checks

def exp_dict(**kw):
    d = {"experiment": "E1", "question": "q", "cases": "heldout", "repeats": 1,
         "arms": [{"id": "A", "options": {"architecture": "C0"}}, {"id": "B", "options": {"architecture": "C2"}}],
         "compare": [{"arms": ["A", "B"], "varies": ["architecture"]}]}
    d.update(kw)
    return d


def test_fair_experiment_is_accepted():
    e = Experiment.model_validate(exp_dict())
    assert e.settings(e.arm("A"))["memory"] == "playbook"           # defaults filled in


@pytest.mark.parametrize("change, message", [
    ({"arms": [{"id": "A", "options": {"architecture": "C0"}},
               {"id": "B", "options": {"architecture": "C2"}, "max_tool_calls": 5}]}, "max_tool_calls"),
    ({"arms": [{"id": "A", "options": {"architecture": "C0"}},
               {"id": "B", "options": {"architecture": "C2"}, "repeats": 3}]}, "repeats"),
    ({"arms": [{"id": "A", "options": {"architecture": "C0"}},
               {"id": "B", "options": {"architecture": "C2"}, "cases": "dev"}]}, "cases"),
    ({"arms": [{"id": "A"}, {"id": "B"}]}, "would measure nothing"),
    ({"compare": [{"arms": ["A", "Z"], "varies": ["architecture"]}]}, "unknown arms"),
    ({"arms": [{"id": "A", "options": {"architecture": "C0"}}, {"id": "B", "options": {"architecture": "C2"}},
               {"id": "C", "options": {"architecture": "C1"}}]}, "in no comparison"),
    ({"arms": [{"id": "A", "options": {"architecture": "C0"}}, {"id": "A", "options": {"architecture": "C2"}}]},
     "unique"),
])
def test_unfair_or_broken_experiments_are_refused(change, message):
    with pytest.raises(ValidationError, match=message):
        Experiment.model_validate(exp_dict(**change))


def test_unknown_fields_and_values_are_refused():
    with pytest.raises(ValidationError):
        Experiment.model_validate(exp_dict(arms=[{"id": "A", "options": {"architecture": "C9"}},
                                                 {"id": "B", "options": {"architecture": "C2"}}]))
    with pytest.raises(ValidationError):
        Experiment.model_validate(exp_dict(budget=5))


def write(folder: Path, name: str, data: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{name}.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def test_file_name_must_match_the_experiment(tmp_path):
    with pytest.raises(ValueError, match="E1.yaml"):
        load_experiment(write(tmp_path, "E2", exp_dict()))


def reuse_pair(tmp_path, **arm_b):
    write(tmp_path, "E1", exp_dict(repeats=3))
    b = {"id": "B", "options": {"architecture": "C2"}, "reuse": "E1/B", **arm_b}
    write(tmp_path, "E3", exp_dict(experiment="E3", arms=[{"id": "A", "options": {"architecture": "C0"}}, b]))


def test_reuse_of_an_identical_arm_is_accepted(tmp_path):
    reuse_pair(tmp_path)
    assert load_experiments(tmp_path)["E3"].arm("B").reuse == "E1/B"


@pytest.mark.parametrize("arm_b, message", [
    ({"max_tool_calls": 5}, "differ"),
    ({"repeats": 5}, "repeats"),
    ({"reuse": "E4/B"}, "not loaded"),
])
def test_reuse_must_be_identical_and_loaded(tmp_path, arm_b, message):
    reuse_pair(tmp_path, **arm_b)
    with pytest.raises(ValueError, match=message):
        load_experiments(tmp_path)


def test_reuse_of_a_reused_arm_is_refused(tmp_path):
    reuse_pair(tmp_path)
    write(tmp_path, "E4", exp_dict(experiment="E4", arms=[{"id": "A", "options": {"architecture": "C0"}},
                                                          {"id": "B", "options": {"architecture": "C2"},
                                                           "reuse": "E3/B"}]))
    with pytest.raises(ValueError, match="itself reuses"):
        load_experiments(tmp_path)


# ---------------------------------------------------------------- arms as run configurations

def test_arm_config_carries_options_budget_repeats_and_hash():
    e4 = resolve("E4")
    b5, b10 = arm_config(e4, e4.arm("budget-5")), arm_config(e4, e4.arm("budget-10"))
    assert b5.config_id == "E4-budget-5" and b5.max_tool_calls == 5 and b5.repeats == 1
    assert b5.options == {"architecture": "C2", "memory": "playbook", "defences": "on", "review_enabled": False}
    assert b5.experiment == "E4" and b5.arm == "budget-5"
    assert b5.config_hash and b5.config_hash != b10.config_hash
    assert b5.config_hash == arm_config(e4, e4.arm("budget-5")).config_hash      # stable


def test_shared_arm_has_the_same_settings_hash_except_repeats():
    e1, e4 = resolve("E1"), resolve("E4")
    s1, s4 = e1.settings(e1.arm("C2")), e4.settings(e4.arm("memory-on"))
    assert {k: v for k, v in s1.items() if k != "repeats"} == {k: v for k, v in s4.items() if k != "repeats"}


def private(tmp_path) -> Path:
    """A private folder with two "held-out" cases (copies of dev cases) and no red team."""
    root = tmp_path / "private"
    (root / "heldout").mkdir(parents=True)
    for cid in ("D-001", "D-002"):
        shutil.copy(DEV / f"{cid}.yaml", root / "heldout" / f"{cid}.yaml")
    return root


def test_plan_counts_runs_and_leaves_shared_arms_out(tmp_path):
    p = plan(resolve("E4"), private_dir=private(tmp_path), minutes_per_run=30)
    rows = {r["arm"]: r for r in p["arms"]}
    assert rows["memory-off"]["runs"] == 2 and rows["misleading-on"]["runs"] == 3
    assert rows["memory-on"]["reuse"] == "E1/C2"
    assert p["new_runs"] == 2 + 2 + 2 + 3 + 3 and p["complete"] and p["hours"] == 6.0
    text = format_plan(p)
    assert "reuses E1/C2" in text and "new runs: 12" in text


def test_plan_without_the_private_folder_says_so(tmp_path, monkeypatch):
    p = plan(resolve("E2"), private_dir=tmp_path / "missing")
    assert not p["complete"] and "at least" in format_plan(p)


def test_arm_inputs_narrow_and_refuse_unknown_ids(tmp_path):
    e4 = resolve("E4")
    cases, attacks = arm_inputs(e4, e4.arm("misleading-off"))
    assert [c.case_id for c in cases] == ["D-011"] and attacks is None
    priv = private(tmp_path)
    cases, _ = arm_inputs(e4, e4.arm("memory-off"), private_dir=priv, only=["D-002"])
    assert [c.case_id for c in cases] == ["D-002"]
    with pytest.raises(ValueError, match="unknown IDs"):
        arm_inputs(e4, e4.arm("memory-off"), private_dir=priv, only=["H-999"])
    with pytest.raises(ValueError, match="no redteam folder"):
        arm_inputs(resolve("E2"), resolve("E2").arm("attacks-on"), private_dir=priv)


# ---------------------------------------------------------------- running arms

class NoOptionsAgent(DoNothingAgent):
    name = "no-options"
    options_supported = frozenset()


def test_agent_that_cannot_honour_the_options_is_refused():
    opts = {"architecture": "C0"}
    assert "architecture" in not_ready(NoOptionsAgent(), options=opts)
    assert not_ready(DoNothingAgent(), options=opts | {"memory": "off", "defences": "off",
                                                        "review_enabled": False}) is None


class RecordingAgent(DoNothingAgent):
    name = "recording"

    def __init__(self):
        self.options = []

    def run(self, ctx):
        self.options.append(dict(ctx.options))
        return super().run(ctx)


def dev_experiments(tmp_path) -> Path:
    folder = tmp_path / "experiments"
    write(folder, "E1", {"experiment": "E1", "question": "q", "cases": "dev", "repeats": 1,
                         "arms": [{"id": "C0", "options": {"architecture": "C0"}, "only": ["D-001"]},
                                  {"id": "C2", "options": {"architecture": "C2"}, "only": ["D-001"]}],
                         "compare": [{"arms": ["C0", "C2"], "varies": ["architecture"]}]})
    write(folder, "E4", {"experiment": "E4", "question": "q", "cases": "dev", "repeats": 1,
                         "arms": [{"id": "on", "only": ["D-001"], "reuse": "E1/C2"},
                                  {"id": "b5", "only": ["D-001"], "max_tool_calls": 5}],
                         "compare": [{"arms": ["on", "b5"], "varies": ["max_tool_calls"]}]})
    return folder


def test_run_experiment_runs_arms_with_their_options_and_logs_them(tmp_path):
    store, deps = make(tmp_path)
    exps = load_experiments(dev_experiments(tmp_path))
    agent = RecordingAgent()
    said = []
    entries = run_experiment(exps["E1"], None, agent, deps, run_batch=run_batch, say=said.append,
                             alert_timeout_s=300)
    assert [o["architecture"] for o in agent.options] == ["C0", "C2"]
    assert all(o["review_enabled"] is False for o in agent.options)
    assert [e["config_id"] for e in entries] == ["E1-C0", "E1-C2"] and not any(e["partial"] for e in entries)
    log = [json.loads(line) for line in (tmp_path / LOG_NAME).read_text().splitlines()]
    assert [x["arm"] for x in log] == ["C0", "C2"]
    assert log[0]["settings"]["architecture"] == "C0" and log[0]["agent"] == "recording@1"
    assert log[0]["runs"] == 1 and "p95_latency_s" in log[0] and "models" in log[0] and log[0]["code"]
    results = Path(entries[1]["directory"]) / "results.jsonl"
    line = json.loads(results.read_text().splitlines()[0])
    assert line["config_id"] == "E1-C2" and line["config_hash"] == log[1]["config_hash"]
    assert all_at_rest(store)


def test_run_experiment_skips_shared_arms_and_refuses_to_run_one(tmp_path):
    store, deps = make(tmp_path)
    exps = load_experiments(dev_experiments(tmp_path))
    said = []
    entries = run_experiment(exps["E4"], None, DoNothingAgent(), deps, run_batch=run_batch, say=said.append,
                             alert_timeout_s=300)
    assert [e["arm"] for e in entries] == ["b5"] and any("reuses E1/C2" in s for s in said)
    with pytest.raises(ValueError, match="run that arm instead"):
        run_experiment(exps["E4"], ["on"], DoNothingAgent(), deps, run_batch=run_batch)


def test_cli_plan_and_experiment_run(tmp_path, capsys):
    folder = dev_experiments(tmp_path)
    assert main(["--experiment", "E4", "--plan", "--experiments-dir", str(folder)]) == 0
    assert "reuses E1/C2" in capsys.readouterr().out
    store, deps = make(tmp_path)
    argv = ["--experiment", "E1", "--arm", "C2", "--experiments-dir", str(folder), "--alert-timeout", "300"]
    assert main(argv, deps=deps) == 0
    out = capsys.readouterr().out
    assert "E1-C2: task success" in out and "E1-C0" not in out
    assert main(["--experiment", "E1", "--arm", "C9", "--experiments-dir", str(folder)], deps=deps) == 2


def test_cli_refuses_mixed_or_missing_arguments():
    with pytest.raises(SystemExit):
        main(["--experiment", "E1", "--cases", str(DEV)])
    with pytest.raises(SystemExit):
        main(["--cases", str(DEV), "--config-id", "X", "--plan"])
    with pytest.raises(SystemExit):
        main(["--cases", str(DEV)])


def test_repo_experiment_files_are_in_the_experiments_folder():
    assert sorted(p.name for p in EXPERIMENTS_DIR.glob("E*.yaml")) == ["E1.yaml", "E2.yaml", "E3.yaml", "E4.yaml"]


def test_e2_runs_without_memory_so_it_needs_no_playbook():
    """E2 measures the defences: memory is off in every arm (the same in both arms of each comparison)."""
    e2 = resolve("E2")
    assert {e2.settings(a)["memory"] for a in e2.arms} == {"off"}
    assert all(arm_config(e2, a).options["memory"] == "off" for a in e2.arms)
    assert {e2.settings(a)["cases"] for a in e2.arms} <= {"dev", "attacks"}      # never held-out


# ---------------------------------------------------------------- dev rehearsal (Sprint Backlog: E1-E4 on dev cases)

def test_rehearsal_moves_held_out_arms_to_the_rehearsal_dev_cases():
    from eval.runner.experiments import REHEARSAL_CASES, arm_settings
    e1, e4 = resolve("E1"), resolve("E4")
    s = arm_settings(e1, e1.arm("C0"), rehearsal=True)
    assert s["cases"] == "dev" and s["only"] == list(REHEARSAL_CASES) and s["repeats"] == 1
    assert arm_settings(e1, e1.arm("C2"), rehearsal=True)["memory"] == "on"       # no frozen playbook yet
    assert arm_settings(e4, e4.arm("misleading-on"), rehearsal=True)["only"] == ["D-011"]   # an arm's own cases stay
    assert arm_settings(e1, e1.arm("C0")) == e1.settings(e1.arm("C0"))             # not rehearsing: unchanged
    cases, _ = arm_inputs(e1, e1.arm("C1"), rehearsal=True)
    assert [c.case_id for c in cases] == list(REHEARSAL_CASES)
    assert all((DEV / f"{c}.yaml").is_file() for c in REHEARSAL_CASES)


def test_rehearsal_config_is_named_and_hashed_apart():
    e1 = resolve("E1")
    real, dev = arm_config(e1, e1.arm("C2")), arm_config(e1, e1.arm("C2"), rehearsal=True)
    assert dev.config_id == "E1-C2-DEV" and dev.repeats == 1 and dev.options["memory"] == "on"
    assert dev.config_hash != real.config_hash


def test_rehearsal_attack_arms_keep_attacks_on_rehearsal_cases(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import eval.runner.experiments as X
    fake = [SimpleNamespace(attack_id="A1", base_case="D-001"), SimpleNamespace(attack_id="A2", base_case="D-002")]
    monkeypatch.setattr(X, "load_attacks", lambda folder: list(fake))
    (tmp_path / "redteam").mkdir()
    e2 = resolve("E2")
    _, attacks = arm_inputs(e2, e2.arm("attacks-on"), private_dir=tmp_path, rehearsal=True)
    assert [a.attack_id for a in attacks] == ["A1"]
    _, attacks = arm_inputs(e2, e2.arm("attacks-on"), private_dir=tmp_path)
    assert [a.attack_id for a in attacks] == ["A1", "A2"]


def test_rehearsal_runs_are_logged_as_rehearsal_and_kept_out_of_the_results(tmp_path):
    from eval.report import case_index, experiments_markdown
    store, deps = make(tmp_path)
    exps = load_experiments(dev_experiments(tmp_path))
    entries = run_experiment(exps["E1"], ["C2"], DoNothingAgent(), deps, run_batch=run_batch, say=lambda s: None,
                             rehearsal=True, alert_timeout_s=300)
    assert entries[0]["config_id"] == "E1-C2-DEV" and entries[0]["rehearsal"] is True
    log = [json.loads(x) for x in (tmp_path / LOG_NAME).read_text().splitlines()]
    cases = case_index([DEV])
    assert "| C2 | not run |" in experiments_markdown(exps, log, cases)              # not a result
    assert "| C2 | 1 |" in experiments_markdown(exps, log, cases, rehearsal=True)


def test_cli_rehearse_needs_an_experiment(tmp_path):
    with pytest.raises(SystemExit):
        main(["--cases", str(DEV), "--config-id", "X", "--rehearse"])
    assert main(["--experiment", "E1", "--plan", "--rehearse"]) == 0
