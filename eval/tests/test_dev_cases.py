"""Every dev case file must be valid, and the dev set must cover the catalogue."""
from __future__ import annotations

from pathlib import Path

from eval.schemas.case import load_cases
from eval.schemas.catalogue import EXCLUDED_FAULT_TYPES, GOLDEN_FAULT_TYPES, SEVERITY_RULES

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
    for cid in ("D-001", "D-003", "D-011", "D-013"):
        assert cases[cid].expected.recovery.measure == "since_fix", cid
    # Latency and memory are judged against the run's own baseline.
    for cid in ("D-006", "D-009", "D-010", "D-012"):
        assert cases[cid].expected.recovery.baseline_factor is not None, cid
    # Slow recoveries measured in the 300 s re-probe get longer windows.
    for cid in ("D-006", "D-010"):
        assert cases[cid].expected.recovery.within_s >= 600, cid       # imageSlowLoad: still ~5x after 2.5 min
    for cid in ("D-009", "D-020"):
        assert cases[cid].expected.recovery.within_s >= 900, cid       # emailMemoryLeak: stayed high 3 min


def test_email_leak_flag_off_alone_or_with_restart_is_correct():
    cases = {c.case_id: c for c in load_cases(DEV_DIR)}
    for cid in ("D-009", "D-020"):
        options = [[a.tool for a in opt] for opt in cases[cid].expected.correct_actions]
        assert options == [["set_flag"], ["set_flag", "restart_service"]], cid


def test_no_dev_case_uses_an_excluded_fault():
    cases = load_cases(DEV_DIR)
    assert not {f.flag for c in cases for f in c.faults} & set(EXCLUDED_FAULT_TYPES)
    assert {"adHighCpu", "intlShippingSlowdown", "recommendationCacheFailure"} <= set(EXCLUDED_FAULT_TYPES)


def test_every_dev_case_cites_a_policy_rule_matching_its_level():
    # Person 1's policy (docs/severity-policy.md): S1.1 SEV-1, S2.1 SEV-2, S3.x SEV-3, S0.1 NONE.
    for c in load_cases(DEV_DIR):
        sev = c.expected.severity
        assert sev.rule is not None, c.case_id
        assert SEVERITY_RULES[sev.rule] == sev.level, c.case_id
    cases = {c.case_id: c for c in load_cases(DEV_DIR)}
    for cid in ("D-001", "D-017", "D-019"):           # checkout error rate >= 10%: S1.1
        assert cases[cid].expected.severity.level == "SEV-1", cid
    assert cases["D-005"].expected.severity.level == "SEV-2"   # partial, one product: S2.1
