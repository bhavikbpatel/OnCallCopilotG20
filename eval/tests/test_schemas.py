"""Case files with mistakes must be rejected before anyone runs them."""
from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from eval.schemas.case import Case
from eval.schemas.result import payload_hash
from eval.tests.test_checkers import NOISE_CASE, PAYMENT_CASE


def bad(mutate, base=PAYMENT_CASE):
    data = copy.deepcopy(base)
    mutate(data)
    with pytest.raises(ValidationError):
        Case.model_validate(data)


def test_valid_cases_load():
    Case.model_validate(PAYMENT_CASE)
    Case.model_validate(NOISE_CASE)


def test_unknown_flag():
    bad(lambda d: d["faults"][0].update(flag="paymentBroken"))


def test_variant_not_offered_by_flag():
    bad(lambda d: d["faults"][0].update(variant="60%"))


def test_off_is_not_a_fault_variant():
    bad(lambda d: d["faults"][0].update(variant="off"))


def test_load_flag_is_not_a_fault():
    bad(lambda d: d["faults"][0].update(flag="loadGeneratorFloodHomepage", variant="on"))


def test_wrong_root_cause_service():
    bad(lambda d: d["expected"]["root_causes"][0].update(service="checkout"))


def test_root_causes_must_match_injected_faults():
    bad(lambda d: d["expected"]["root_causes"][0].update(service="cart", fault_type="cartFailure"))


def test_split_and_prefix_must_agree():
    bad(lambda d: d.update(split="heldout"))


def test_intermittent_needs_timings():
    bad(lambda d: d["faults"][0].update(mode="intermittent"))


def test_unknown_field_is_rejected():
    bad(lambda d: d.update(answer="paymentFailure"))


def test_unknown_action_tool():
    bad(lambda d: d["expected"]["correct_actions"][0][0].update(tool="exec"))


def test_incident_needs_recovery_check():
    bad(lambda d: d["expected"].pop("recovery"))


def test_noise_cannot_inject_faults():
    bad(lambda d: d.update(faults=[{"flag": "adFailure", "variant": "on"}]), NOISE_CASE)


def test_noise_needs_load_spike():
    bad(lambda d: d.update(load={"profile": "normal"}), NOISE_CASE)


def test_noise_severity_is_none():
    bad(lambda d: d["expected"]["severity"].update(level="SEV3"), NOISE_CASE)


def test_payload_hash_ignores_tokens_and_key_order():
    a = payload_hash("set_flag", {"flag": "x", "value": "off"})
    b = payload_hash("set_flag", {"value": "off", "flag": "x", "approval_token": "t"})
    c = payload_hash("set_flag", {"flag": "x", "value": "on"})
    assert a == b != c


def test_example_result_line_scores_as_success():
    import json
    from pathlib import Path

    from eval.checkers import score
    from eval.schemas.case import load_case
    from eval.schemas.result import ResultLine

    root = Path(__file__).resolve().parents[1]
    result = ResultLine.model_validate(json.loads((root / "schemas/examples/result_D-001.json").read_text()))
    report = score(load_case(root / "cases/dev/D-001.yaml"), result)
    assert report.task_success, report
