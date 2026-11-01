"""Split checker: rules hold, and the output never reveals held-out content.

Held-out cases here are throwaway copies of dev cases made in a temp folder; the real
held-out set never enters the repo.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from eval.schemas.case import Case, load_cases
from eval.schemas.split import MIN_HELDOUT, check, coverage, main

DEV = Path(__file__).resolve().parents[1] / "cases" / "dev"
SECRET = "SECRET-HELDOUT-MARKER"


def fake_heldout(n: int = MIN_HELDOUT) -> list[dict]:
    """n held-out-shaped cases built from dev cases: new IDs, new groups, shifted start times."""
    dev = [yaml.safe_load(p.read_text()) for p in sorted(DEV.glob("*.yaml"))]
    out = []
    for i in range(n):
        d = copy.deepcopy(dev[i % len(dev)])
        d.update(case_id=f"H-{i + 1:03d}", split="heldout", scenario_group=d["scenario_group"] + "-H",
                 title=f"{SECRET} {i}", notes=SECRET)
        for f in d["faults"]:
            f["start_after_s"] = f.get("start_after_s", 0) + 7 + i       # never an exact dev setup
        if d["load"]["profile"] == "flood":
            d["load"]["flood_start_after_s"] = d["load"].get("flood_start_after_s", 0) + 11 + i
        out.append(d)
    return out


def write(folder: Path, cases: list[dict]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for c in cases:
        (folder / f"{c['case_id']}.yaml").write_text(yaml.safe_dump(c, sort_keys=False))
    return folder


def models(cases: list[dict]) -> list[Case]:
    return [Case.model_validate(c) for c in cases]


def test_good_split_has_no_problems(tmp_path):
    assert check(load_cases(DEV), models(fake_heldout()), tmp_path) == []


def test_shared_scenario_group_is_a_problem(tmp_path):
    ho = fake_heldout()
    ho[0]["scenario_group"] = "payment-A"                     # a dev group
    problems = check(load_cases(DEV), models(ho), tmp_path)
    assert any("both splits" in p and "payment-A" in p for p in problems)


def test_exact_repeat_of_a_dev_setup_is_a_problem(tmp_path):
    dev = load_cases(DEV)
    ho = fake_heldout()
    src = yaml.safe_load((DEV / "D-001.yaml").read_text())
    ho[0]["faults"], ho[0]["load"] = src["faults"], src["load"]
    ho[0]["expected"] = src["expected"]
    assert any("repeat a dev case" in p for p in check(dev, models(ho), tmp_path))


def test_too_few_heldout_cases_is_a_problem(tmp_path):
    problems = check(load_cases(DEV), models(fake_heldout(10)), tmp_path)
    assert any("need at least 40" in p for p in problems)


def test_heldout_folder_inside_repo_is_refused():
    problems = check(load_cases(DEV), models(fake_heldout()), DEV.parent / "heldout")
    assert any("inside the repo" in p for p in problems)


def test_duplicate_ids_are_a_problem(tmp_path):
    ho = fake_heldout()
    ho[1]["case_id"] = ho[0]["case_id"]
    problems = check(load_cases(DEV), models(ho), tmp_path)
    assert any("duplicate case IDs" in p for p in problems)


def test_missing_fault_coverage_is_named(tmp_path):
    ho = [c for c in fake_heldout(60) if not any(f["flag"] == "kafkaQueueProblems" for f in c["faults"])]
    problems = check(load_cases(DEV), models(ho), tmp_path)
    assert any("held-out set has no case for" in p and "kafkaQueueProblems" in p for p in problems)


def test_coverage_counts():
    cov = coverage(load_cases(DEV))
    assert cov["cases"] == 20 and cov["noise"] >= 1 and cov["two_fault"] >= 1
    assert cov["by_fault"]["paymentFailure"] >= 1 and "adHighCpu" not in cov["by_fault"]


def test_cli_prints_counts_only_never_heldout_content(tmp_path, capsys):
    folder = write(tmp_path / "heldout", fake_heldout())
    assert main(["--dev", str(DEV), "--heldout", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "OK:" in out and SECRET not in out
    assert "H-001" not in out                                # not even case IDs


def test_cli_problem_messages_do_not_leak_content(tmp_path, capsys):
    ho = fake_heldout()
    ho[0]["scenario_group"] = "payment-A"
    folder = write(tmp_path / "heldout", ho)
    assert main(["--dev", str(DEV), "--heldout", str(folder)]) == 1
    assert SECRET not in capsys.readouterr().out


def test_cli_unreadable_case_reports_type_only(tmp_path, capsys):
    ho = fake_heldout()
    ho[0]["faults"][0]["flag"] = SECRET                      # invalid: unknown flag
    folder = write(tmp_path / "heldout", ho)
    assert main(["--dev", str(DEV), "--heldout", str(folder)]) == 2
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
