"""Every dev case file must be valid, and the dev set must cover the catalogue."""
from __future__ import annotations

from pathlib import Path

from eval.schemas.case import load_cases
from eval.schemas.catalogue import EXCLUDED_FAULT_TYPES, GOLDEN_FAULT_TYPES

DEV_DIR = Path(__file__).resolve().parents[1] / "cases" / "dev"


def test_dev_cases_load_and_validate():
    cases = load_cases(DEV_DIR)
    assert len(cases) >= 20
    assert all(c.split == "dev" for c in cases)


def test_dev_set_covers_every_fault_type_noise_and_two_faults():
    cases = load_cases(DEV_DIR)
    covered = {f.flag for c in cases for f in c.faults}
    assert covered == set(GOLDEN_FAULT_TYPES), f"missing: {set(GOLDEN_FAULT_TYPES) - covered}"
    assert not covered & set(EXCLUDED_FAULT_TYPES)
    assert any(c.expected.verdict == "noise" for c in cases)
    assert any(len(c.faults) >= 2 for c in cases)


def test_dev_cases_use_measured_recovery_checks():
    cases = {c.case_id: c for c in load_cases(DEV_DIR)}
    signals = {c.expected.recovery.signal for c in cases.values() if c.expected.recovery}
    assert "cpu_percent" not in signals  # cannot show recovery in this shop
    # Low-traffic checkout path and rare cart operations are judged since the fix.
    for cid in ("D-001", "D-003", "D-011"):
        assert cases[cid].expected.recovery.measure == "since_fix", cid
    # Latency and memory are judged against the run's own baseline.
    for cid in ("D-008", "D-009", "D-010", "D-013"):
        assert cases[cid].expected.recovery.baseline_factor is not None, cid


def test_email_leak_restart_is_acceptable_not_required():
    cases = {c.case_id: c for c in load_cases(DEV_DIR)}
    for cid in ("D-009", "D-020"):
        exp = cases[cid].expected
        assert [a.tool for a in exp.correct_actions[0]] == ["set_flag"]
        assert any(a.tool == "restart_service" for a in exp.acceptable_actions)
