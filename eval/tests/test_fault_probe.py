"""Fault probe: analysis logic (pure) and a full run against fakes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.injector import InMemoryFlagStore
from eval.injector.flagstore import get_variant, variants_of
from eval.probes import __main__ as probes_cli
from eval.probes import faults as flt
from eval.schemas.catalogue import FAULT_TYPES, FLAG_VARIANTS
from eval.telemetry import PromClient
from eval.tests.fakes import FakeFlagUI, FakePrometheus, real_flags


def s(t, **values):
    """Sample helper: s(0, payment={"error_rate": 0.5})."""
    return {"t": t, "values": values}


# ---------------------------------------------------------------- configuration

def test_probe_variants_cover_every_fault_and_are_valid():
    assert set(flt.PROBE_VARIANTS) == set(FAULT_TYPES)
    for flag, variant in flt.PROBE_VARIANTS.items():
        assert variant in FLAG_VARIANTS[flag]
    assert flt.ITEMS[0] == "control" and flt.ITEMS[-1] == "flood"


# ---------------------------------------------------------------- movement rules

@pytest.mark.parametrize("signal,base,value,moved", [
    ("error_rate", 0.0, 0.5, True),
    ("error_rate", 0.0, 0.03, False),          # below +5 points
    ("latency_p95_ms", 40.0, 5000.0, True),
    ("latency_p95_ms", 40.0, 70.0, False),     # +30 ms, not doubled
    ("latency_p95_ms", 400.0, 470.0, False),   # +70 ms but not doubled
    ("memory_mb", 100.0, 300.0, True),
    ("memory_mb", 1000.0, 1030.0, False),      # +30 MB but only +3%
    ("cpu_percent", 5.0, 90.0, True),
    ("throughput_rps", 10.0, 2.0, True),       # fall of 80%
    ("throughput_rps", 10.0, 4.0, False),      # fall of 60%: normal Locust swing
    ("throughput_rps", 10.0, 9.0, False),
    ("throughput_rps", 7.9, 12.2, False),      # real control-run swing (+54%)
    ("throughput_rps", 10.0, 30.0, True),      # 3x: flood-like
    ("throughput_rps", 0.067, 0.017, False),   # real checkout gap: too little traffic to judge
    ("throughput_rps", 0.008, 0.0, False),     # real control-run value on payment
    ("throughput_rps", 0.6, 0.1, True),        # busy enough to judge a drop
    ("consumer_lag", 0.0, 400.0, True),
    ("error_rate", None, 0.5, False),          # no baseline: cannot judge
    ("error_rate", 0.0, None, False),
])
def test_moved(signal, base, value, moved):
    assert flt._moved(signal, base, value) is moved


# ---------------------------------------------------------------- analysis

# Real series from the live control run on 31 Oct (no fault active). Baseline is
# the first two values, the next five are "during", the last four "after".
REAL_CONTROL = {
    "cart": [0.65, 0.867, 0.867, 0.867, 1.233, 1.233, 1.267, 1.267, 1.133, 1.133, 1.295, 1.295, 1.338],
    "currency": [0.5, 0.4, 0.4, 0.4, 0.833, 0.833, 0.933, 0.933, 0.467, 0.467, 0.83, 0.83, 0.753],
    "frontend": [6.736, 9.134, 9.134, 9.134, 12.233, 12.233, 11.967, 11.967, 10.8, 10.8, 11.009, 11.009, 9.837],
    "frontend-proxy": [7.686, 9.217, 9.217, 9.217, 12.899, 12.899, 13.267, 13.267, 11.133, 11.133, 11.175,
                       11.175, 11.778],
    "product-catalog": [3.151, 3.133, 3.133, 3.133, 4.45, 4.45, 5.067, 5.067, 4.2, 4.2, 4.018, 4.018, 4.316],
}
# Real payment memory during the live paymentFailure run: a sawtooth, not an effect.
REAL_PAYMENT_MEMORY = [66.359, 66.367, 66.367, 66.367, 95.412, 84.824, 66.437, 66.441, 66.445, 66.445,
                       66.486, 94.958, 66.503]


def _split(series_by_service, signal):
    def phase(lo, hi, t0):
        return [{"t": t0 + 30 * k, "values": {svc: {signal: vals[i]} for svc, vals in series_by_service.items()}}
                for k, i in enumerate(range(lo, hi))]
    return phase(0, 2, -30), phase(2, 9, 0), phase(9, 13, 240)


def test_real_control_run_has_no_movers():
    a = flt.analyse(*_split(REAL_CONTROL, "throughput_rps"), frozenset())
    assert a["movers"] == []


def test_real_low_traffic_gaps_are_not_effects():
    # payment/checkout/email throughput in the live paymentFailure run (~1-4 requests/min).
    quiet = {"checkout": [0.067, 0.067, 0.067, 0.017, 0.017, 0.048, 0.048, 0.087, 0.087, 0.05, 0.05, 0.05, 0.05]}
    assert flt.analyse(*_split(quiet, "throughput_rps"), frozenset({"checkout"}))["movers"] == []


def test_real_memory_sawtooth_is_not_an_effect():
    a = flt.analyse(*_split({"payment": REAL_PAYMENT_MEMORY}, "memory_mb"), frozenset({"payment"}))
    assert a["movers"] == []


def test_single_spike_ignored_but_sustained_change_counts():
    base = [s(-30, svc={"error_rate": 0.0}), s(0, svc={"error_rate": 0.0})]
    after = [s(200, svc={"error_rate": 0.0}), s(230, svc={"error_rate": 0.0})]
    spike = [s(0, svc={"error_rate": 0.0}), s(30, svc={"error_rate": 0.6}), s(60, svc={"error_rate": 0.0})]
    assert flt.analyse(base, spike, after, frozenset({"svc"}))["movers"] == []
    steady = [s(0, svc={"error_rate": 0.0}), s(30, svc={"error_rate": 0.6}), s(60, svc={"error_rate": 0.5})]
    a = flt.analyse(base, steady, after, frozenset({"svc"}))
    assert a["root_first_effect_s"] == 30 and a["movers"][0]["peak"] == 0.6


def test_recovery_needs_last_two_samples_back_to_normal():
    base = [s(-30, svc={"error_rate": 0.0}), s(0, svc={"error_rate": 0.0})]
    during = [s(30, svc={"error_rate": 0.5}), s(60, svc={"error_rate": 0.55})]
    relapse = [s(200, svc={"error_rate": 0.0}), s(230, svc={"error_rate": 0.4})]
    assert flt.analyse(base, during, relapse, frozenset({"svc"}))["root_recovered_after_clear"] is False
    one_sample = [s(200, svc={"error_rate": 0.0})]
    assert flt.analyse(base, during, one_sample, frozenset({"svc"}))["root_recovered_after_clear"] is None

def test_analyse_root_effect_timing_and_recovery():
    baseline = [s(-30, payment={"error_rate": 0.0}, cart={"error_rate": 0.0}),
                s(0, payment={"error_rate": 0.0}, cart={"error_rate": 0.0})]
    during = [s(0, payment={"error_rate": 0.0}), s(30, payment={"error_rate": 0.4}, cart={"error_rate": 0.2}),
              s(60, payment={"error_rate": 0.5}, cart={"error_rate": 0.25})]
    after = [s(90, payment={"error_rate": 0.1}), s(120, payment={"error_rate": 0.0}, cart={"error_rate": 0.0}),
             s(150, payment={"error_rate": 0.01}, cart={"error_rate": 0.01})]
    a = flt.analyse(baseline, during, after, frozenset({"payment"}))
    assert a["root_service_moved"] and a["root_first_effect_s"] == 30
    assert a["root_recovered_after_clear"] is True
    assert a["other_services_moved"] == ["cart"]
    pay = a["movers"][0]
    assert pay["service"] == "payment" and pay["peak"] == 0.5 and pay["end_after_clear"] == 0.01


def test_analyse_leak_that_does_not_recover():
    mem = lambda v: {"email": {"memory_mb": v}}  # noqa: E731
    a = flt.analyse([s(-30, **mem(80)), s(0, **mem(80))],
                    [s(0, **mem(90)), s(30, **mem(250)), s(60, **mem(400))],
                    [s(90, **mem(410)), s(120, **mem(405))], frozenset({"email"}))
    assert a["root_service_moved"] and a["root_recovered_after_clear"] is False


def test_analyse_nothing_moved():
    flat = s(0, payment={"error_rate": 0.0})
    a = flt.analyse([flat], [flat, flat], [flat], frozenset({"payment"}))
    assert a["movers"] == [] and not a["root_service_moved"] and a["root_recovered_after_clear"] is None


# ---------------------------------------------------------------- full run with fakes

class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, sec):
        self.t += sec


def shop_responder(store):
    """Fake shop: paymentFailure raises payment errors; emailMemoryLeak grows memory and never drops."""
    leak = {"mb": 80.0}

    def responder(expr, at):
        flags = variants_of(store.read())
        if 'service_name="payment"' in expr and "or vector(0)" in expr and expr.startswith("(sum(rate("):
            return [({}, 0.5 if flags["paymentFailure"] != "off" else 0.0)]
        if 'container_memory_usage_total_bytes{container_name="email"}' in expr:
            if flags["emailMemoryLeak"] != "off":
                leak["mb"] += 60
            return [({}, leak["mb"])]
        if "throughput" in expr:
            return []
        return [({}, 1.0)] if "container_" in expr else []
    return responder


def test_run_measures_faults_clears_flags_and_saves_after_each(tmp_path):
    store = InMemoryFlagStore(real_flags())
    clock = Clock()
    path = tmp_path / "faults.json"
    with FakePrometheus(shop_responder(store)) as fp:
        report = flt.run(PromClient(fp.url), store, ("control", "paymentFailure", "emailMemoryLeak"),
                         hold_s=90, recover_s=180, every_s=30, clock=clock.now, sleep=clock.sleep, path=path)
    assert all(v == "off" for v in variants_of(store.read()).values())
    items = {i["item"]: i for i in report["items"]}
    assert items["control"]["movers"] == [] and items["control"]["changes"] == {}
    pay = items["paymentFailure"]
    assert pay["root_service_moved"] and pay["root_first_effect_s"] == 0.0
    assert pay["root_recovered_after_clear"] is True and not pay["contaminated"]
    leak = items["emailMemoryLeak"]
    assert leak["root_service_moved"] and leak["root_recovered_after_clear"] is False
    saved = json.loads(path.read_text())
    assert len(saved["items"]) == 3 and "finished_at" in saved


def test_run_clears_flags_when_interrupted(tmp_path):
    store = InMemoryFlagStore(real_flags())
    clock = Clock()

    def sleep(sec):
        clock.sleep(sec)
        if variants_of(store.read())["paymentFailure"] != "off":
            raise KeyboardInterrupt

    with FakePrometheus(shop_responder(store)) as fp:
        with pytest.raises(KeyboardInterrupt):
            flt.run(PromClient(fp.url), store, ("paymentFailure",), hold_s=90, recover_s=180, every_s=30,
                    clock=clock.now, sleep=sleep, path=tmp_path / "f.json")
    assert all(v == "off" for v in variants_of(store.read()).values())
    assert "finished_at" in json.loads((tmp_path / "f.json").read_text())


def test_item_skipped_if_flags_not_at_rest():
    store = InMemoryFlagStore(real_flags())
    store.poke("adFailure", "on")
    with FakePrometheus(lambda e, at: []) as fp:
        result = flt.probe_item(PromClient(fp.url), store, "paymentFailure", 30, 180, 30, "2m",
                                Clock().now, Clock().sleep)
    assert "skipped" in result and "adFailure" in result["skipped"]


def test_unknown_item_rejected():
    with pytest.raises(ValueError, match="unknown probe items"):
        flt.run(PromClient("http://127.0.0.1:9"), InMemoryFlagStore(real_flags()), ("nope",))


def test_summary_line_mentions_root_and_others():
    line = flt.summary_line({"movers": [{"service": "payment", "signal": "error_rate", "baseline": 0.0,
                                         "peak": 0.5, "is_root_service": True}],
                             "root_first_effect_s": 30, "root_recovered_after_clear": True,
                             "other_services_moved": ["checkout"], "contaminated": False})
    assert "payment.error_rate" in line and "checkout" in line and "first effect: 30s" in line
    empty = flt.summary_line({"movers": [], "root_first_effect_s": None, "root_recovered_after_clear": None,
                              "other_services_moved": [], "contaminated": False})
    assert "first effect: n/a" in empty and "Nones" not in empty


def test_cli_faults_refuses_recover_shorter_than_window(capsys):
    with FakePrometheus(lambda e, at: [({}, 1.0)]) as fp, FakeFlagUI() as ui:
        code = probes_cli.main(["--prom-url", fp.url, "--flag-url", ui.url, "faults", "--only", "control",
                                "--recover", "60"])
    assert code == 2 and "at least window + 60s" in capsys.readouterr().err
    assert all(get_variant(f) == "off" for f in ui.config["flags"].values())


def test_cli_faults_window_option_reaches_the_probe(capsys):
    # --window 5m needs --recover >= 360 s; 300 s is refused, which shows the window was passed through.
    with FakePrometheus(lambda e, at: [({}, 1.0)]) as fp, FakeFlagUI() as ui:
        code = probes_cli.main(["--prom-url", fp.url, "--flag-url", ui.url, "faults", "--only", "control",
                                "--window", "5m", "--recover", "300"])
    assert code == 2 and "360s" in capsys.readouterr().err
    assert all(get_variant(f) == "off" for f in ui.config["flags"].values())


def test_coverage_check_stops_probe_when_no_span_data():
    # The real failure on 31 Oct: every request-based signal empty (window too short).
    with pytest.raises(flt.ProbeError, match="no request-based signal"):
        flt.check_coverage({"payment": {"error_rate": None, "memory_mb": 66.0},
                            "frontend": {"throughput_rps": None, "cpu_percent": 3.0}})
    flt.check_coverage({"frontend": {"throughput_rps": 12.0}})  # fine


def test_run_aborts_before_injecting_when_no_span_data(tmp_path):
    store = InMemoryFlagStore(real_flags())
    clock = Clock()
    responder = lambda e, at: [({}, 50.0)] if "container_" in e else []  # noqa: E731
    with FakePrometheus(responder) as fp:
        with pytest.raises(flt.ProbeError):
            flt.run(PromClient(fp.url), store, ("paymentFailure",), clock=clock.now, sleep=clock.sleep,
                    path=tmp_path / "f.json")
    assert store.writes == []  # nothing was injected


def test_recover_must_cover_window():
    with pytest.raises(flt.ProbeError, match="at least window"):
        flt.run(PromClient("http://127.0.0.1:9"), InMemoryFlagStore(real_flags()), ("control",),
                recover_s=120, window="2m")


def test_cli_faults_refuses_dirty_shop(capsys):
    with FakePrometheus(lambda e, at: []) as fp, FakeFlagUI() as ui:
        import copy

        from eval.injector.flagstore import set_variant
        ui.config["flags"]["cartFailure"] = set_variant(copy.deepcopy(ui.config["flags"]["cartFailure"]), "10%")
        code = probes_cli.main(["--prom-url", fp.url, "--flag-url", ui.url, "faults", "--only", "control"])
    assert code == 2 and "not at rest" in capsys.readouterr().err


# ---------------------------------------------------------------- reanalyse

def _saved_report(tmp_path):
    base, during, after = _split(REAL_CONTROL, "throughput_rps")
    stale = {"item": "control", "changes": {}, "contaminated": False, "movers": [{"service": "cart"}],
             "root_service_moved": False, "root_first_effect_s": None, "root_recovered_after_clear": None,
             "other_services_moved": ["cart"],  # what the old, too-sensitive rules said
             "samples": {"baseline": base, "during": during, "after": after}}
    path = tmp_path / "faults-test.json"
    path.write_text(json.dumps({"kind": "fault_probe", "settings": {}, "items": [stale]}))
    return path


def test_reanalyse_rescores_with_current_rules():
    report = json.loads(json.dumps({"kind": "fault_probe", "items": [
        {"item": "control", "movers": ["stale"], "samples": dict(zip(("baseline", "during", "after"),
                                                                     _split(REAL_CONTROL, "throughput_rps"))),
         "contaminated": False}]}))
    new = flt.reanalyse(report)
    assert new["items"][0]["movers"] == [] and new["items"][0]["other_services_moved"] == []
    assert "reanalysed_at" in new and report["items"][0]["movers"] == ["stale"]  # input untouched


def test_cli_reanalyse_writes_new_file_and_keeps_original(tmp_path, capsys):
    path = _saved_report(tmp_path)
    original = path.read_text()
    assert probes_cli.main(["reanalyse", str(path)]) == 0
    assert path.read_text() == original
    new = json.loads((tmp_path / "faults-test-reanalysed.json").read_text())
    assert new["items"][0]["other_services_moved"] == []
    assert "other services moved: none" in capsys.readouterr().out


def test_cli_reanalyse_rejects_bad_input(tmp_path, capsys):
    assert probes_cli.main(["reanalyse", str(tmp_path / "missing.json")]) == 2
    bad = tmp_path / "x.json"
    bad.write_text('{"kind": "discovery"}')
    assert probes_cli.main(["reanalyse", str(bad)]) == 2
    assert "not a fault-probe report" in capsys.readouterr().err


# ---------------------------------------------------------------- regression: real full probe (31 Oct)

REAL_FULL = json.loads((Path(__file__).resolve().parent / "fixtures" / "probe_full_2026-10-31.json").read_text())
# With the 150 s hold of that run, these show their effect in 2+ distinct data points.
DETECTED_ON_REAL_SHOP = ["paymentFailure", "productCatalogFailure", "emailMemoryLeak", "imageSlowLoad",
                         "paymentUnreachable", "productCatalogLockContention", "adFailure"]
# Visible in that run but in ONE data point only: inconclusive at 150 s (needs the 300 s hold).
SINGLE_POINT_AT_150S = ["adHighCpu", "intlShippingSlowdown", "flood"]


def _real_items():
    return {i["item"]: i for i in flt.reanalyse(REAL_FULL)["items"]}


def test_real_full_probe_control_has_no_movers_on_any_service():
    assert _real_items()["control"]["movers"] == []


@pytest.mark.parametrize("item", DETECTED_ON_REAL_SHOP)
def test_real_full_probe_detects_fault_on_root_service(item):
    assert _real_items()[item]["root_service_moved"], item


def test_real_full_probe_recovery_results():
    items = _real_items()
    for item in DETECTED_ON_REAL_SHOP:
        if item != "productCatalogLockContention":
            assert items[item]["root_recovered_after_clear"] is True, item
    # Lock contention: latency still ~2x baseline 3 minutes after the clear.
    assert items["productCatalogLockContention"]["root_recovered_after_clear"] is False


@pytest.mark.parametrize("item", SINGLE_POINT_AT_150S)
def test_real_full_probe_single_point_effects_are_not_counted(item):
    # One data point cannot be told apart from a one-off spike (ad p95 jumped 10x
    # in a control run with nothing wrong), so these stay undecided at 150 s.
    assert not _real_items()[item]["root_service_moved"], item


def test_real_full_probe_recommendation_fault_has_no_visible_effect():
    # The published 3.1.0 recommendation image is Java and shows no change for this flag.
    assert not _real_items()["recommendationCacheFailure"]["root_service_moved"]


def test_real_low_traffic_latency_needs_tenfold_rise():
    base = [s(-30, ad={"latency_p95_ms": 88.0, "throughput_rps": 0.2}),
            s(0, ad={"latency_p95_ms": 88.0, "throughput_rps": 0.2})]
    six_x = [s(30, ad={"latency_p95_ms": 560.0}), s(60, ad={"latency_p95_ms": 560.0})]
    ten_x = [s(30, ad={"latency_p95_ms": 1000.0}), s(60, ad={"latency_p95_ms": 1050.0})]
    assert flt.analyse(base, six_x, [], frozenset({"ad"}))["movers"] == []
    assert flt.analyse(base, ten_x, [], frozenset({"ad"}))["root_service_moved"]


def test_latency_ignored_without_enough_traffic():
    base = [s(-30, checkout={"latency_p95_ms": 95.0, "throughput_rps": 0.03}),
            s(0, checkout={"latency_p95_ms": 95.0, "throughput_rps": 0.03})]
    big = [s(30, checkout={"latency_p95_ms": 9000.0}), s(60, checkout={"latency_p95_ms": 9000.0})]
    assert flt.analyse(base, big, [], frozenset({"checkout"}))["movers"] == []


def test_error_count_only_on_services_quiet_at_rest():
    quiet = [s(-30, cart={"error_count": 0.0}), s(0, cart={"error_count": 0.0})]
    hit = [s(30, cart={"error_count": 3.0}), s(60, cart={"error_count": 4.0})]
    assert flt.analyse(quiet, hit, [], frozenset({"cart"}))["root_service_moved"]
    noisy = [s(-30, flagd={"error_count": 600.0}), s(0, flagd={"error_count": 620.0})]
    more = [s(30, flagd={"error_count": 640.0}), s(60, flagd={"error_count": 650.0})]
    assert flt.analyse(noisy, more, [], frozenset({"flagd"}))["movers"] == []


def test_effect_window_extends_past_clear_by_lag():
    base = [s(-30, svc={"error_rate": 0.0}), s(0, svc={"error_rate": 0.0})]
    during = [s(0, svc={"error_rate": 0.0}), s(120, svc={"error_rate": 0.5})]
    after = [s(180, svc={"error_rate": 0.5}), s(330, svc={"error_rate": 0.0}), s(360, svc={"error_rate": 0.01})]
    a = flt.analyse(base, during, after, frozenset({"svc"}), effect_until_s=150 + flt.EFFECT_LAG_S)
    assert a["root_first_effect_s"] == 120 and a["root_recovered_after_clear"] is True
    # Without the lag only one moved sample is inside the window: missed (the original bug).
    assert not flt.analyse(base, during, after, frozenset({"svc"}))["root_service_moved"]


def test_load_generator_is_never_a_mover():
    base = [s(-30, **{"load-generator": {"memory_mb": 838.0}}), s(0, **{"load-generator": {"memory_mb": 840.0}})]
    up = [s(30, **{"load-generator": {"memory_mb": 1050.0}}), s(60, **{"load-generator": {"memory_mb": 1050.0}})]
    assert flt.analyse(base, up, [], frozenset())["movers"] == []


# ---------------------------------------------------------------- regression: real re-probe, 300 s hold (31 Oct)

REAL_REPROBE = json.loads((Path(__file__).resolve().parent / "fixtures" / "probe_reprobe_2026-10-31.json").read_text())


def test_real_reprobe_control_clean_despite_single_point_spikes():
    # The control run had ad p95 spikes of 1,140 and 1,580 ms, each one data point.
    items = {i["item"]: i for i in flt.reanalyse(REAL_REPROBE)["items"]}
    assert items["control"]["movers"] == []


@pytest.mark.parametrize("item,signal", [("cartFailure", "error_count"), ("kafkaQueueProblems", "consumer_lag")])
def test_real_reprobe_detects_and_recovers(item, signal):
    it = {i["item"]: i for i in flt.reanalyse(REAL_REPROBE)["items"]}[item]
    assert it["root_service_moved"] and it["root_recovered_after_clear"] is True
    assert any(m["signal"] == signal and m["is_root_service"] for m in it["movers"])


def test_distinct_points_collapses_duplicates_but_keeps_repeats_a_minute_apart():
    vals, times = flt.distinct_points([1140, 1140, 80, 15000, 15000, 15000], [0, 33, 60, 90, 120, 180])
    assert vals == [1140, 80, 15000, 15000] and times == [0, 60, 90, 180]


def test_latency_p50_signal_is_judged_too():
    base = [s(-30, ad={"latency_p50_ms": 5.0, "throughput_rps": 0.3}), s(0, ad={"latency_p50_ms": 5.0, "throughput_rps": 0.3})]
    up = [s(30, ad={"latency_p50_ms": 40.0}), s(90, ad={"latency_p50_ms": 45.0})]
    assert flt.analyse(base, up, [], frozenset({"ad"}))["root_service_moved"]


# ---------------------------------------------------------------- regression: real 300 s re-probe (31 Oct evening)

REAL_300 = json.loads((Path(__file__).resolve().parent / "fixtures" / "probe_full300_2026-10-31.json").read_text())
DETECTED_AT_300S = ["paymentFailure", "cartFailure", "productCatalogFailure", "kafkaQueueProblems",
                    "emailMemoryLeak", "imageSlowLoad", "paymentUnreachable", "productCatalogLockContention",
                    "adFailure", "flood"]


def _items_300():
    return {i["item"]: i for i in flt.reanalyse(REAL_300)["items"]}


def test_300s_control_is_clean_on_every_service_with_the_2x_traffic_bar():
    assert _items_300()["control"]["movers"] == []


@pytest.mark.parametrize("item", DETECTED_AT_300S)
def test_300s_detects_fault_on_root_service(item):
    assert _items_300()[item]["root_service_moved"], item


def test_300s_flood_is_caught_by_the_2x_traffic_bar():
    flood = _items_300()["flood"]
    assert any(m["service"] == "frontend" and m["signal"] == "throughput_rps" for m in flood["movers"])


def test_300s_adhighcpu_has_no_visible_effect():
    # Excluded from the golden set on this evidence (catalogue.EXCLUDED_FAULT_TYPES).
    assert not _items_300()["adHighCpu"]["root_service_moved"]


def test_300s_intl_shipping_spikes_never_make_two_points_in_a_row():
    # Real effect (shipping p95 5,750-7,000 ms vs a control max of 170 ms) but each spike sits
    # between normal readings at ~0.2 req/s; a 5-minute window is being tested for this.
    items = _items_300()
    assert not items["intlShippingSlowdown"]["root_service_moved"]
    p95 = [s["values"]["shipping"]["latency_p95_ms"] for s in REAL_300["items"][10]["samples"]["during"]]
    assert max(p95) > 5000 and min(p95) < 100


def test_300s_slow_recoveries():
    items = _items_300()
    # Email memory stayed at ~96 MB for 3 minutes after the flag went off; image latency was still
    # ~5x baseline 2.5 minutes after. The dev cases give these 900 s and 600 s to recover.
    assert items["emailMemoryLeak"]["root_recovered_after_clear"] is False
    assert items["imageSlowLoad"]["root_recovered_after_clear"] is False
    for item in ("paymentFailure", "cartFailure", "kafkaQueueProblems", "productCatalogLockContention", "adFailure"):
        assert items[item]["root_recovered_after_clear"] is True, item


# ---------------------------------------------------------------- slow_requests (1 Nov)

def test_slow_requests_moves_on_one_slow_request_only_where_none_at_rest():
    assert flt._moved("slow_requests", 0.0, 1.0)
    assert not flt._moved("slow_requests", 0.0, 0.0)
    assert not flt._moved("slow_requests", 3.0, 20.0)      # already slow at rest: a count means nothing


def test_sample_all_measures_slow_requests_for_span_services():
    with FakePrometheus(lambda e, at: [({}, 1.0)]) as fp:
        row = flt.sample_all(PromClient(fp.url), services={"shipping", "recommendation"})
    assert row["shipping"]["slow_requests"] == 1.0          # the fake answers 1.0 to every query
    assert "slow_requests" not in row["recommendation"]
