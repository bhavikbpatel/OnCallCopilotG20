"""Prometheus client (contract) and discovery probe (logic)."""
from __future__ import annotations

import json
import math

import pytest

from eval.injector import InMemoryFlagStore
from eval.probes import __main__ as probes_cli
from eval.probes import discover as disc
from eval.schemas.catalogue import SERVICES
from eval.telemetry import PromClient, PromError
from eval.tests.fakes import FakeFlagUI, FakePrometheus, real_flags


# ---------------------------------------------------------------- client

def test_query_vector_scalar_and_nan():
    def responder(expr, at):
        if expr == "scalar_q":
            return "scalar"
        if expr == "nan_q":
            return [({}, "NaN")]
        return [({"service_name": "payment"}, 0.5), ({"service_name": "cart"}, 1)]

    with FakePrometheus(responder) as fp:
        prom = PromClient(fp.url)
        samples = prom.query("up")
        assert [(s.labels["service_name"], s.value) for s in samples] == [("payment", 0.5), ("cart", 1.0)]
        assert prom.query_value("scalar_q") == 42.0
        assert math.isnan(prom.query_value("nan_q"))


def test_query_passes_time_and_handles_empty():
    with FakePrometheus(lambda e, at: []) as fp:
        prom = PromClient(fp.url)
        assert math.isnan(prom.query_value("x", at=1000.5))
        assert fp.queries[-1] == ("x", 1000.5)


def test_query_value_refuses_multiple_series():
    with FakePrometheus(lambda e, at: [({"a": "1"}, 1), ({"a": "2"}, 2)]) as fp:
        with pytest.raises(PromError, match="expected one series"):
            PromClient(fp.url).query_value("x")


def test_bad_query_raises_with_prometheus_message():
    def responder(expr, at):
        raise ValueError("parse error: unexpected character")

    with FakePrometheus(responder) as fp:
        with pytest.raises(PromError, match="parse error"):
            PromClient(fp.url).query("sum(")


def test_unreachable_prometheus():
    with pytest.raises(PromError, match="not reachable"):
        PromClient("http://127.0.0.1:9", timeout_s=0.5).metric_names()


def test_label_values_and_build_info():
    with FakePrometheus(lambda e, at: [], labels={"__name__": ["a_total", "b"]}) as fp:
        prom = PromClient(fp.url)
        assert prom.metric_names() == ["a_total", "b"]
        assert prom.build_info()["version"] == "3.13.1"


# ---------------------------------------------------------------- discovery logic

NAMES = ["traces_span_metrics_calls_total", "traces_span_metrics_duration_milliseconds_bucket",
         "container_memory_usage_total_bytes", "container_cpu_utilization_ratio",
         "kafka_consumer_lag", "demo_cart_add_item_latency_seconds_bucket", "http_server_request_duration_seconds_count"]


def test_classify_metric_names():
    groups = disc.classify_metric_names(NAMES)
    assert groups["memory"] == ["container_memory_usage_total_bytes"]
    assert groups["cpu"] == ["container_cpu_utilization_ratio"]
    assert groups["kafka_lag"] == ["kafka_consumer_lag"]
    assert "traces_span_metrics_calls_total" in groups["span_metrics"]
    assert groups["app_specific"] == ["demo_cart_add_item_latency_seconds_bucket"]


def test_candidate_queries_are_well_formed():
    for signal, variants in disc.CANDIDATES.items():
        for variant, template in variants.items():
            q = template.replace("{svc}", "payment")
            assert "{svc}" not in q and q.count("(") == q.count(")") and q.count("{") == q.count("}"), q
            assert 'service_name="payment"' in q


def test_candidate_coverage_counts_numbers_only():
    base = {"a": {"error_rate": {"server_spans": 0.0, "all_spans": None}},
            "b": {"error_rate": {"server_spans": 0.1, "all_spans": {"error": "x"}}}}
    assert disc.candidate_coverage(base) == {"error_rate.server_spans": 2, "error_rate.all_spans": 0}


class Clock:
    def __init__(self, t=10_000.0):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def noisy_responder(excess_after_write_at=None):
    """Errors: 2 per window for payment always; flagd spikes after the write time."""
    def responder(expr, at):
        if expr.startswith("sum by (service_name) (increase("):
            rows = [({"service_name": "payment"}, 2.0)]
            if excess_after_write_at is not None and at is not None and at > excess_after_write_at:
                rows.append(({"service_name": "flagd"}, 18.0))
            return rows
        if "time() - max(timestamp(" in expr:
            return [({}, 12.0)]
        if expr.startswith("topk(25, sum by (service_name, span_name"):
            return [({"service_name": "cart", "span_name": "EvaluateFlag", "span_kind": "SPAN_KIND_CLIENT"}, 120.0),
                    ({"service_name": "flagd", "span_name": "resolve", "span_kind": "SPAN_KIND_SERVER"}, 900.0)]
        if expr.startswith("topk(25,"):
            return [({"container_name": "payment", "service_name": "payment", "instance": "x", "group": "g1"}, 123.0)]
        if 'service_name="payment"' in expr:
            return [({}, 0.01)]
        return []
    return responder


def test_noise_check_flags_spike_after_write_and_changes_nothing():
    clock = Clock()
    store = InMemoryFlagStore(real_flags())
    with FakePrometheus(noisy_responder(excess_after_write_at=clock.t + 1)) as fp:
        result = disc.noise_check(PromClient(fp.url), store, wait_s=180, now=clock.now, sleep=clock.sleep)
    assert result["services_with_excess_errors"] == ["flagd"]
    assert result["per_service"]["payment"]["excess"] == 0
    assert result["per_service"]["flagd"]["excess"] == 18.0
    assert len(store.writes) == 1 and all(v == "off" for v in store.writes[0].values())  # no-op write


def test_noise_check_quiet_when_no_spike():
    clock = Clock()
    with FakePrometheus(noisy_responder()) as fp:
        result = disc.noise_check(PromClient(fp.url), InMemoryFlagStore(real_flags()), 180,
                                  now=clock.now, sleep=clock.sleep)
    assert result["services_with_excess_errors"] == []


def test_noise_check_skips_if_flags_active():
    store = InMemoryFlagStore(real_flags())
    store.poke("cartFailure", "10%")
    with FakePrometheus(noisy_responder()) as fp:
        result = disc.noise_check(PromClient(fp.url), store, 180, now=Clock().now, sleep=Clock().sleep)
    assert "skipped" in result and store.writes == []


def test_discover_report_shape():
    clock = Clock()
    labels = {"__name__": NAMES, "service_name": sorted(SERVICES - {"astronomy-db"})}
    with FakePrometheus(noisy_responder(), labels=labels) as fp:
        report = disc.discover(PromClient(fp.url), InMemoryFlagStore(real_flags()), wait_s=180,
                               now=clock.now, sleep=clock.sleep)
    assert report["metric_count"] == len(NAMES)
    assert report["freshness_s"] == 12.0
    assert set(report["baseline"]) == set(SERVICES)
    assert report["baseline"]["payment"]["error_rate"]["server_spans"] == 0.01
    assert report["baseline"]["cart"]["error_rate"]["server_spans"] is None
    assert report["candidate_coverage"]["error_rate.server_spans"] == 1
    sample = report["samples"]["container_memory_usage_total_bytes"]
    assert sample["series"][0]["labels"] == {"container_name": "payment", "service_name": "payment", "group": "g1"}
    assert "instance" in sample["label_keys"]  # dropped from labels, still listed
    assert list(report["samples"])[0] == "container_memory_usage_total_bytes"  # priority first
    ops = report["breakdown"]["errors_by_operation"]
    assert [r["labels"]["service_name"] for r in ops] == ["flagd", "cart"]  # sorted by count
    json.dumps(report)  # serialisable
    text = "\n".join(disc.summary_lines(report))
    assert "astronomy-db" in text and "noise check" in text


def test_cli_discover_no_noise_writes_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(disc, "REPORTS_DIR", tmp_path)
    with FakePrometheus(noisy_responder(), labels={"__name__": NAMES}) as fp:
        assert probes_cli.main(["--prom-url", fp.url, "discover", "--no-noise"]) == 0
    files = list(tmp_path.glob("discovery-*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["noise_check"] == {"skipped": "disabled"}
    assert "report:" in capsys.readouterr().out


def test_cli_discover_unreachable(capsys):
    assert probes_cli.main(["--prom-url", "http://127.0.0.1:9", "discover", "--no-noise"]) == 2
    assert "not reachable" in capsys.readouterr().err


def test_cli_discover_with_noise_over_http(tmp_path, monkeypatch):
    monkeypatch.setattr(disc, "REPORTS_DIR", tmp_path)
    with FakePrometheus(noisy_responder(), labels={"__name__": NAMES}) as fp, FakeFlagUI() as ui:
        assert probes_cli.main(["--prom-url", fp.url, "--flag-url", ui.url, "discover", "--wait", "0"]) == 0
        assert len(ui.write_bodies) == 1  # exactly one no-op write
    report = json.loads(next(tmp_path.glob("discovery-*.json")).read_text())
    assert report["noise_check"]["services_with_excess_errors"] == []


def test_error_rate_treats_no_errors_as_zero():
    for variant, template in disc.CANDIDATES["error_rate"].items():
        assert template.startswith("(sum(rate(") and "or vector(0))" in template, variant


def test_breakdown_records_query_errors():
    def responder(expr, at):
        raise ValueError("bad metric")
    with FakePrometheus(responder) as fp:
        out = disc.breakdown(PromClient(fp.url))
    assert set(out) == set(disc.BREAKDOWN_QUERIES) and all("error" in v for v in out.values())
