"""Fault injector: unit tests (fake clock) and contract tests (fake flag UI server)."""
from __future__ import annotations

import copy
import json
import time

import pytest

from eval.injector import (
    FlagEvent,
    FlagStoreError,
    HttpFlagStore,
    InMemoryFlagStore,
    Injector,
    PreflightError,
    build_plan,
)
from eval.injector import __main__ as cli
from eval.injector.flagstore import apply, get_variant, set_variant, variants_of
from eval.injector.plan import group_by_time
from eval.schemas.case import Case, load_cases
from eval.schemas.catalogue import FLAG_VARIANTS, LOAD_FLAG
from eval.tests.fakes import FakeFlagUI, FakeTime, real_flags
from eval.tests.test_checkers import NOISE_CASE, PAYMENT_CASE
from eval.tests.test_dev_cases import DEV_DIR


def case(**over):
    d = copy.deepcopy(PAYMENT_CASE)
    d["faults"][0].update(start_after_s=0)
    for k, v in over.items():
        d[k] = v
    return Case.model_validate(d)


def at_rest(store) -> bool:
    return all(v == "off" for v in variants_of(store.read()).values())


def run_sync(store, c, horizon, hooks=None, check_every=15.0):
    ft = FakeTime(stop_at=horizon, hooks=hooks)
    inj = Injector(store, check_every_s=check_every, clock=ft.clock, waiter=ft.waiter)
    inj.start(c, horizon_s=horizon, run_id="t", background=False)
    return inj


# ---------------------------------------------------------------- fixture sanity

def test_fixture_matches_catalogue():
    flags = real_flags()
    assert set(flags) == set(FLAG_VARIANTS)
    for name, offered in FLAG_VARIANTS.items():
        assert set(flags[name]["variants"]) == set(offered) | {"off"}, name
    assert all(v == "off" for v in variants_of(flags).values())


# ---------------------------------------------------------------- variant helpers

def test_set_variant_uses_default_variant():
    f = set_variant(real_flags()["paymentFailure"], "50%")
    assert f["defaultVariant"] == "50%" and get_variant(f) == "50%"


def test_set_variant_uses_targeting_branch_for_product_catalog():
    original = real_flags()["productCatalogFailure"]
    f = set_variant(original, "on")
    assert f["targeting"]["if"][1] == "on" and f["defaultVariant"] == "off"
    assert get_variant(f) == "on"
    assert original["targeting"]["if"][1] == "off"  # input not mutated


def test_set_variant_rejects_unknown_variant():
    with pytest.raises(ValueError):
        set_variant(real_flags()["paymentFailure"], "60%")


def test_apply_rejects_unknown_flag():
    with pytest.raises(FlagStoreError):
        apply(InMemoryFlagStore(real_flags()), {"paymentBroken": "on"})


def test_apply_multiple_flags_is_one_write():
    store = InMemoryFlagStore(real_flags())
    result = apply(store, {"paymentFailure": "25%", "adHighCpu": "on"})
    assert len(store.writes) == 1
    assert result["paymentFailure"] == "25%" and result["adHighCpu"] == "on"


# ---------------------------------------------------------------- plan

def test_steady_plan():
    c = case(faults=[{"flag": "paymentFailure", "variant": "50%", "start_after_s": 60}])
    assert build_plan(c, 300) == [FlagEvent(60, "paymentFailure", "50%")]


def test_intermittent_plan_alternates_until_horizon():
    c = case(faults=[{"flag": "paymentFailure", "variant": "25%", "mode": "intermittent",
                      "on_s": 60, "off_s": 30, "start_after_s": 10}])
    events = [(e.at_s, e.variant) for e in build_plan(c, 200)]
    assert events == [(10, "25%"), (70, "off"), (100, "25%"), (160, "off"), (190, "25%")]


def test_flood_plan_with_duration():
    c = Case.model_validate({**NOISE_CASE, "load": {"profile": "flood", "flood_start_after_s": 30,
                                                    "flood_duration_s": 60}})
    assert build_plan(c, 300) == [FlagEvent(30, LOAD_FLAG, "on"), FlagEvent(90, LOAD_FLAG, "off")]


def test_fault_after_horizon_is_rejected():
    c = case(faults=[{"flag": "paymentFailure", "variant": "50%", "start_after_s": 600}])
    with pytest.raises(ValueError):
        build_plan(c, 300)


def test_simultaneous_events_grouped():
    groups = group_by_time([FlagEvent(0, "a", "on"), FlagEvent(0, "b", "on"), FlagEvent(5, "a", "off")])
    assert groups == [(0, {"a": "on", "b": "on"}), (5, {"a": "off"})]


def test_every_dev_case_produces_a_valid_plan():
    for c in load_cases(DEV_DIR):
        plan = build_plan(c, 900)
        assert plan, c.case_id
        for e in plan:
            assert e.variant == "off" or e.variant in FLAG_VARIANTS[e.flag], (c.case_id, e)


# ---------------------------------------------------------------- injector (fake clock)

def test_steady_fault_applied_then_cleared():
    store = InMemoryFlagStore(real_flags())
    inj = run_sync(store, case(), horizon=120)
    assert variants_of(store.read())["paymentFailure"] == "50%"
    rec = inj.stop()
    assert rec.cleared and rec.ok and at_rest(store)
    assert rec.applied[0]["changes"] == {"paymentFailure": "50%"}


def test_late_start_is_applied_at_the_right_time():
    store = InMemoryFlagStore(real_flags())
    c = case(faults=[{"flag": "paymentFailure", "variant": "50%", "start_after_s": 60}])
    inj = run_sync(store, c, horizon=120)
    assert [a["at_s"] for a in inj.record.applied] == [60]
    inj.stop()


def test_intermittent_writes_in_order():
    store = InMemoryFlagStore(real_flags())
    c = case(faults=[{"flag": "paymentFailure", "variant": "25%", "mode": "intermittent",
                      "on_s": 60, "off_s": 60}])
    inj = run_sync(store, c, horizon=250)
    assert [w["paymentFailure"] for w in store.writes] == ["25%", "off", "25%", "off", "25%"]
    assert inj.stop().ok and at_rest(store)


def test_released_flag_is_no_longer_toggled_by_the_plan():
    # Intermittent 60 s on / 60 s off; the agent's fix (flag off) is approved at t=70.
    store = InMemoryFlagStore(real_flags())
    c = case(faults=[{"flag": "paymentFailure", "variant": "25%", "mode": "intermittent",
                      "on_s": 60, "off_s": 60}])
    holder = {}

    def fix():
        holder["inj"].release("paymentFailure", "off")
        apply(store, {"paymentFailure": "off"})          # the agent executes its fix

    ft = FakeTime(stop_at=250, hooks={70: fix})
    inj = holder["inj"] = Injector(store, check_every_s=15, clock=ft.clock, waiter=ft.waiter)
    inj.start(c, horizon_s=250, run_id="t", background=False)
    # plan on at 0, plan off at 60, agent fix at 70; then the plan never switches it on again (120 s, 240 s)
    assert [w["paymentFailure"] for w in store.writes] == ["25%", "off", "off"]
    rec = inj.stop()
    assert rec.ok and not rec.contaminated and rec.released[0]["flag"] == "paymentFailure"
    assert at_rest(store)


def test_release_resets_between_runs():
    store = InMemoryFlagStore(real_flags())
    inj = run_sync(store, case(), horizon=30)
    inj.release("paymentFailure")
    inj.stop()
    ft = FakeTime(stop_at=30)
    inj._clock, inj._wait = ft.clock, ft.waiter           # a fresh fake clock for the second run
    rec = inj.start(case(), horizon_s=30, run_id="b", background=False)
    assert rec.released == [] and rec.applied        # the new run applies the fault again
    assert inj.stop().ok and at_rest(store)


def test_two_faults_at_once_are_one_write():
    store = InMemoryFlagStore(real_flags())
    d = copy.deepcopy(PAYMENT_CASE)
    d["faults"] = [{"flag": "paymentFailure", "variant": "50%"}, {"flag": "adFailure", "variant": "on"}]
    d["expected"]["root_causes"].append({"service": "ad", "fault_type": "adFailure"})
    c = Case.model_validate(d)
    run_sync(store, c, horizon=30).stop()
    assert len(store.writes) == 2  # one apply, one clear
    assert at_rest(store)


def test_preflight_refuses_dirty_shop_and_changes_nothing():
    store = InMemoryFlagStore(real_flags())
    store.poke("cartFailure", "10%")
    inj = Injector(store)
    with pytest.raises(PreflightError, match="not at rest"):
        with inj.run(case(), horizon_s=60, run_id="t"):
            pass
    assert store.writes == []  # nothing touched
    assert variants_of(store.read())["cartFailure"] == "10%"


def test_preflight_force_clear():
    store = InMemoryFlagStore(real_flags())
    store.poke("cartFailure", "10%")
    ft = FakeTime(stop_at=30)
    inj = Injector(store, clock=ft.clock, waiter=ft.waiter)
    inj.start(case(), 30, "t", background=False, force_clear=True)
    assert variants_of(store.read())["cartFailure"] == "off"
    assert inj.stop().ok


def test_preflight_detects_missing_flags():
    flags = real_flags()
    del flags["paymentUnreachable"]
    with pytest.raises(PreflightError, match="missing"):
        Injector(InMemoryFlagStore(flags)).preflight()


def test_drift_from_someone_else_marks_run_contaminated():
    store = InMemoryFlagStore(real_flags())
    inj = run_sync(store, case(), horizon=120, check_every=10,
                   hooks={25: lambda: store.poke("adFailure", "on")})
    rec = inj.stop()
    assert rec.contaminated and not rec.ok
    assert rec.drift[0]["flag"] == "adFailure" and rec.drift[0]["expected"] == "off"
    assert at_rest(store)  # clear also resets the stray flag


def test_planned_flag_changed_by_someone_else_is_drift():
    store = InMemoryFlagStore(real_flags())
    inj = run_sync(store, case(), horizon=120, check_every=10,
                   hooks={25: lambda: store.poke("paymentFailure", "off")})
    rec = inj.stop()
    assert rec.contaminated and rec.drift[0]["expected"] == "50%"


def test_drift_logged_once_per_change():
    store = InMemoryFlagStore(real_flags())
    inj = run_sync(store, case(), horizon=300, check_every=5,
                   hooks={20: lambda: store.poke("adFailure", "on")})
    assert len(inj.stop().drift) == 1


class FailingOnceStore(InMemoryFlagStore):
    def __init__(self, flags):
        super().__init__(flags)
        self.fail_next_write = True

    def write(self, flags):
        if self.fail_next_write:
            self.fail_next_write = False
            raise FlagStoreError("boom")
        super().write(flags)


def test_store_error_is_recorded_and_shop_still_cleared():
    store = FailingOnceStore(real_flags())
    inj = run_sync(store, case(), horizon=60)
    rec = inj.stop()
    assert rec.errors and "boom" in rec.errors[0]
    assert rec.cleared and at_rest(store) and not rec.ok


def test_context_manager_clears_on_exception():
    store = InMemoryFlagStore(real_flags())
    inj = Injector(store, check_every_s=0.05)
    with pytest.raises(RuntimeError, match="agent crashed"):
        with inj.run(case(), horizon_s=5, run_id="t"):
            deadline = time.monotonic() + 2
            while variants_of(store.read())["paymentFailure"] != "50%" and time.monotonic() < deadline:
                time.sleep(0.01)
            raise RuntimeError("agent crashed")
    assert inj.record.cleared and at_rest(store)


def test_stop_is_idempotent():
    store = InMemoryFlagStore(real_flags())
    inj = run_sync(store, case(), horizon=30)
    first = inj.stop()
    writes = len(store.writes)
    assert inj.stop() is first and len(store.writes) == writes


def test_record_saves_json(tmp_path):
    store = InMemoryFlagStore(real_flags())
    rec = run_sync(store, case(), horizon=30).stop()
    data = json.loads(rec.save(tmp_path).read_text())
    assert data["case_id"] == "D-901" and data["cleared"] and data["ok"] and not data["contaminated"]
    assert data["applied"][0]["applied_at"]  # ISO timestamp


# ---------------------------------------------------------------- HTTP client (contract)

def test_http_read_and_confirmed_write():
    with FakeFlagUI() as ui:
        store = HttpFlagStore(ui.url)
        assert set(store.read()) == set(FLAG_VARIANTS)
        apply(store, {"paymentFailure": "75%"})
        assert get_variant(ui.config["flags"]["paymentFailure"]) == "75%"
        assert ui.write_bodies[0]["data"]["$schema"].startswith("https://flagd.dev")
        assert set(ui.write_bodies[0]["data"]["flags"]) == set(FLAG_VARIANTS)  # whole config sent


def test_http_write_waits_for_async_apply():
    with FakeFlagUI(apply_delay_s=0.3) as ui:
        store = HttpFlagStore(ui.url, poll_s=0.05)
        apply(store, {"adHighCpu": "on"})
        assert get_variant(ui.config["flags"]["adHighCpu"]) == "on"


def test_http_write_that_never_lands_raises():
    with FakeFlagUI(drop_writes=True) as ui:
        store = HttpFlagStore(ui.url, confirm_timeout_s=0.3, poll_s=0.05)
        with pytest.raises(FlagStoreError, match="not confirmed"):
            apply(store, {"adHighCpu": "on"})


def test_http_unreachable_raises_clear_error():
    waits = []
    store = HttpFlagStore("http://127.0.0.1:9/feature", timeout_s=0.5, sleep=waits.append)
    with pytest.raises(FlagStoreError, match="not reachable.*after 4 attempts"):
        store.read()
    assert waits == [1.0, 2.0, 4.0]                      # retried with growing waits, then gave up


def test_http_brief_unavailability_is_retried():
    """Seen live right after a shop restart: one 503 from the flag UI ended the injection and voided the run."""
    with FakeFlagUI(fail_next=[503, 502]) as ui:
        waits = []
        store = HttpFlagStore(ui.url, sleep=waits.append)
        assert set(store.read()) == set(FLAG_VARIANTS)
        assert waits == [1.0, 2.0] and ui.requests == 3


def test_http_write_is_retried_and_still_confirmed():
    with FakeFlagUI(fail_next=[504]) as ui:
        store = HttpFlagStore(ui.url, poll_s=0.01, sleep=lambda s: None)
        apply(store, {"paymentFailure": "75%"})            # read 504 -> retried; write; confirmed by read
        assert get_variant(ui.config["flags"]["paymentFailure"]) == "75%"


def test_http_unavailable_for_too_long_still_fails():
    with FakeFlagUI(fail_next=[503] * 4) as ui:
        store = HttpFlagStore(ui.url, sleep=lambda s: None)
        with pytest.raises(FlagStoreError, match="503.*after 4 attempts"):
            store.read()
        assert ui.requests == 4


@pytest.mark.parametrize("code", [400, 404, 500])
def test_http_other_errors_are_not_retried(code):
    with FakeFlagUI(fail_next=[code]) as ui:
        waits = []
        store = HttpFlagStore(ui.url, sleep=waits.append)
        with pytest.raises(FlagStoreError, match=str(code)):
            store.read()
        assert waits == [] and ui.requests == 1


def test_injection_survives_a_brief_outage_during_the_run():
    with FakeFlagUI(apply_delay_s=0.02) as ui:
        store = HttpFlagStore(ui.url, poll_s=0.02, backoff_s=0.01)
        inj = Injector(store, check_every_s=0.05)
        with inj.run(case(), horizon_s=2, run_id="t"):
            time.sleep(0.3)
            with ui.lock:
                ui.fail_next = [503, 503]                    # the drift check meets a short outage
            time.sleep(0.3)
            assert get_variant(ui.config["flags"]["paymentFailure"]) == "50%"
        assert inj.record.ok and not inj.record.errors


def test_end_to_end_over_http_with_real_threads():
    with FakeFlagUI(apply_delay_s=0.05) as ui:
        store = HttpFlagStore(ui.url, poll_s=0.02)
        inj = Injector(store, check_every_s=0.05)
        with inj.run(case(), horizon_s=2, run_id="t"):
            time.sleep(0.4)
            assert get_variant(ui.config["flags"]["paymentFailure"]) == "50%"
        assert inj.record.ok
        assert all(get_variant(f) == "off" for f in ui.config["flags"].values())


# ---------------------------------------------------------------- CLI

def test_cli_status_set_clear(capsys):
    with FakeFlagUI() as ui:
        assert cli.main(["--url", ui.url, "status"]) == 0
        assert "all flags at rest" in capsys.readouterr().out
        assert cli.main(["--url", ui.url, "set", "cartFailure", "10%"]) == 0
        cli.main(["--url", ui.url, "status"])
        assert "1 flag(s) active" in capsys.readouterr().out
        assert cli.main(["--url", ui.url, "clear"]) == 0
        assert all(get_variant(f) == "off" for f in ui.config["flags"].values())


def test_cli_inject_dev_case_and_clear(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("eval.injector.record.RECORDS_DIR", tmp_path)
    with FakeFlagUI() as ui:
        code = cli.main(["--url", ui.url, "inject", "D-003", "--duration", "0.5", "--run-id", "cli-test"])
        assert code == 0, capsys.readouterr()
        assert all(get_variant(f) == "off" for f in ui.config["flags"].values())
    rec = json.loads((tmp_path / "cli-test.json").read_text())
    assert rec["applied"][0]["changes"] == {"cartFailure": "25%"} and rec["ok"]


def test_cli_reports_errors_without_traceback(capsys, monkeypatch):
    import functools
    monkeypatch.setattr(cli, "HttpFlagStore", functools.partial(cli.HttpFlagStore, sleep=lambda s: None))
    assert cli.main(["--url", "http://127.0.0.1:9/feature", "status"]) == 2
    assert "not reachable" in capsys.readouterr().err
