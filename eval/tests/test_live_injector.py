"""Live tests against the running Astronomy Shop. Skipped unless EVAL_LIVE=1.

    cd OnCallCopilotG20/eval && EVAL_LIVE=1 python -m pytest -m live -v

They change real flags briefly and always clear them. Make sure the flag UI
scheduler is off and nobody else is using the flag UI.
"""
from __future__ import annotations

import os
import time

import pytest

from eval.injector import HttpFlagStore, Injector
from eval.injector.flagstore import apply, variants_of
from eval.schemas.case import load_case
from eval.schemas.catalogue import FLAG_VARIANTS
from eval.tests.test_dev_cases import DEV_DIR

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("EVAL_LIVE") != "1", reason="set EVAL_LIVE=1 to run against the real shop"),
]


@pytest.fixture
def injector():
    inj = Injector(HttpFlagStore(), check_every_s=1.0)
    inj.preflight()  # refuses to run if the shop is not clean
    yield inj
    inj.clear()


def test_live_shop_has_every_3_1_0_flag_at_rest(injector):
    status = injector.status()
    assert set(FLAG_VARIANTS) <= set(status)
    assert all(v == "off" for v in status.values()), status


def test_live_set_and_clear_round_trip(injector):
    confirmed = apply(injector.store, {"paymentFailure": "10%"})
    assert confirmed["paymentFailure"] == "10%"
    injector.clear()
    assert all(v == "off" for v in injector.status().values())


def test_live_targeting_flag_round_trip(injector):
    confirmed = apply(injector.store, {"productCatalogFailure": "on"})
    assert confirmed["productCatalogFailure"] == "on"
    injector.clear()
    assert variants_of(injector.store.read())["productCatalogFailure"] == "off"


def test_live_short_injection_of_a_dev_case(injector):
    case = load_case(DEV_DIR / "D-003.yaml")  # cartFailure 50%, starts immediately
    with injector.run(case, horizon_s=5, run_id="live-test-D-003"):
        time.sleep(2)
        assert injector.status()["cartFailure"] == "50%"
    rec = injector.record
    assert rec.cleared and not rec.errors, rec.errors
    assert not rec.contaminated, f"something else changed flags (scheduler?): {rec.drift}"
