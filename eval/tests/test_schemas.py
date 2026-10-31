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



# ---------------------------------------------------------------- recovery check forms (31 Oct revision)

def _rec(**kw):
    def mut(d):
        d["expected"]["recovery"] = {"signal": "error_rate", "service": "payment"} | kw
    return mut


def test_recovery_needs_exactly_one_of_threshold_or_baseline():
    bad(_rec())                                            # neither
    bad(_rec(threshold=0.01, baseline_factor=2))           # both


def test_recovery_baseline_form_is_valid():
    d = copy.deepcopy(PAYMENT_CASE)
    d["expected"]["recovery"] = {"signal": "latency_p95_ms", "service": "frontend-proxy", "baseline_factor": 3}
    Case.model_validate(d)


def test_since_fix_rules():
    d = copy.deepcopy(PAYMENT_CASE)
    d["expected"]["recovery"] = {"signal": "error_count", "service": "cart", "measure": "since_fix",
                                 "threshold": 1, "within_s": 300, "settle_s": 90}
    Case.model_validate(d)
    bad(_rec(measure="since_fix", threshold=0.05, within_s=150, settle_s=90))     # under 120 s of data
    bad(_rec(measure="since_fix", baseline_factor=2))                             # needs a threshold
    bad(lambda x: x["expected"].update(recovery={"signal": "latency_p95_ms", "service": "payment",
                                                 "measure": "since_fix", "threshold": 100}))


def test_cpu_cannot_be_a_recovery_signal():
    bad(lambda x: x["expected"].update(recovery={"signal": "cpu_percent", "service": "ad", "threshold": 50}))


def test_recovery_unknown_service_rejected():
    bad(_rec(threshold=0.01, service="paymnet"))


def test_excluded_fault_type_is_refused():
    def mut(d):
        d["faults"] = [{"flag": "recommendationCacheFailure", "variant": "on"}]
        d["expected"]["root_causes"] = [{"service": "recommendation", "fault_type": "recommendationCacheFailure"}]
    bad(mut)
