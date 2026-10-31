"""Every dev case file must be valid, and the dev set must cover the catalogue."""
from __future__ import annotations

from pathlib import Path

from eval.schemas.case import load_cases
from eval.schemas.catalogue import FAULT_TYPES

DEV_DIR = Path(__file__).resolve().parents[1] / "cases" / "dev"


def test_dev_cases_load_and_validate():
    cases = load_cases(DEV_DIR)
    assert len(cases) >= 20
    assert all(c.split == "dev" for c in cases)


def test_dev_set_covers_every_fault_type_noise_and_two_faults():
    cases = load_cases(DEV_DIR)
    covered = {f.flag for c in cases for f in c.faults}
    assert covered == set(FAULT_TYPES), f"missing: {set(FAULT_TYPES) - covered}"
    assert any(c.expected.verdict == "noise" for c in cases)
    assert any(len(c.faults) >= 2 for c in cases)
