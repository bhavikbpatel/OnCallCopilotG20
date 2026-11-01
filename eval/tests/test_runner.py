"""Runner end to end against a fake shop: flags in memory, Prometheus answering from those flags.

The real Injector is used (run synchronously); a fake clock makes every wait instant.
The agents defined here are TEST-ONLY: the fixing agent proves that a perfect run
scores as a success, and is never shipped or used to score anything.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import pytest

from eval.injector.flagstore import InMemoryFlagStore, apply, variants_of
from eval.injector.injector import Injector
from eval.runner import AgentResult, DoNothingAgent, RunConfig, RunDeps, meets_threshold, run_batch, run_case
from eval.runner.__main__ import main
from eval.runner.agents import AgentContext
from eval.runner.alerts import signal_for, threshold_for, wait_for_alert
from eval.runner.approvals import AutoApprover
from eval.schemas.case import AlertSpec, load_case
from eval.schemas.result import payload_hash
from eval.telemetry import signals
from eval.tests.fakes import real_flags

DEV = Path(__file__).resolve().parents[1] / "cases" / "dev"
T0 = 1_000_000.0
PERSON2_ALERT_FIELDS = {"alert_name", "service", "signal", "summary", "query", "threshold", "value",
                        "fired_at", "fingerprint"}


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        self.t += seconds


class SyncInjector(Injector):
    """The real injector, run synchronously with its clock jumped JUMP_S ahead, so every change the
    plan makes in its first JUMP_S seconds (e.g. D-001's fault at +60 s) is applied before start()
    returns. Used only with cases whose plan has no later "off" events in that span."""

    JUMP_S = 150.0

    def __init__(self, store):
        self._ticks = iter(())
        super().__init__(store, waiter=lambda ev, s: True, clock=lambda: next(self._ticks, self.JUMP_S))

    def start(self, case, horizon_s, run_id, background=True, force_clear=False):
        self._ticks = iter([0.0])                     # t0 = 0, every later reading = JUMP_S
        return super().start(case, horizon_s, run_id, background=False, force_clear=force_clear)


class ShopProm:
    """Answers the harness's queries from which flags are on (a tiny model of the 3.1.0 shop)."""

    def __init__(self, store, broken: bool = False):
        self.store, self.broken = store, broken

    def on(self) -> set[str]:
        return {k for k, v in variants_of(self.store.read()).items() if v != "off"}

    def query_value(self, expr, at=None):
        on = self.on()
        pay = "paymentFailure" in on and not self.broken
        if expr == signals.promql("error_rate", "checkout"):
            return 0.5 if pay else 0.0
        if expr == signals.promql("throughput_rps", "checkout"):
            return 0.05
        if expr == signals.promql("throughput_rps", "frontend"):
            return 34.0 if "loadGeneratorFloodHomepage" in on else 12.0
        if "increase(" in expr and 'service_name="payment"' in expr:
            return (10.0 if pay else 0.0) if signals.ERROR in expr else 20.0
        return math.nan


def make(tmp_path, *, broken=False, dirty=None):
    store = InMemoryFlagStore(real_flags())
    if dirty:
        store.poke(*dirty)
    clock = Clock()
    deps = RunDeps(prom=ShopProm(store, broken), injector=SyncInjector(store), results_dir=tmp_path,
                   clock=clock, sleep=clock.sleep)
    return store, deps


def cfg(**kw):
    kw.setdefault("config_id", "TEST")
    kw.setdefault("alert_timeout_s", 300)
    return RunConfig(**kw)


def all_at_rest(store) -> bool:
    return all(v == "off" for v in variants_of(store.read()).values())


# ---------------------------------------------------------------- test-only agents

class FixingAgent:
    """Turns paymentFailure off with approval, like a perfect copilot would. TEST ONLY."""

    name, version = "test-fixer", "1"

    def __init__(self, store):
        self.store = store
        self.seen: list[AgentContext] = []

    def run(self, ctx):
        self.seen.append(ctx)
        args = {"flag": "paymentFailure", "variant": "off"}
        h = payload_hash("set_flag", args)
        decision = ctx.approve({"tool": "set_flag", **args})
        calls = [{"seq": 0, "agent": "remediation", "agent_role": "remediation", "server": "ops_actions",
                  "tool": "dry_run", "kind": "read", "args": {"tool": "set_flag", **args}, "payload_hash": h,
                  "ok": True}]
        state = {"hypotheses": [{"id": "h1", "service": "payment", "fault_type": "paymentFailure",
                                 "status": "supported", "evidence": []}], "severity": "SEV-2"}
        if decision["decision"] != "approve":
            return AgentResult(state=state | {"final_status": "escalated"}, tool_calls=calls)
        apply(self.store, {"paymentFailure": "off"})
        calls.append({"seq": 1, "agent": "remediation", "agent_role": "remediation", "server": "ops_actions",
                      "tool": "set_flag", "kind": "write", "args": args, "payload_hash": h, "ok": True,
                      "purpose": "fix"})
        return AgentResult(state=state | {"final_status": "recovered"}, tool_calls=calls, fixed_at=ctx.clock())


class PokingAgent:
    """Someone else changes a flag mid-run (scheduler / UI). TEST ONLY."""

    name, version = "test-poker", "1"

    def __init__(self, store):
        self.store = store

    def run(self, ctx):
        self.store.poke("adFailure", "on")
        return AgentResult(tool_calls=[])


class RaisingAgent:
    name, version = "test-raiser", "1"

    def __init__(self, exc):
        self.exc = exc

    def run(self, ctx):
        raise self.exc


class SlowAgent:
    """Overruns its deadline, then tries to act anyway. TEST ONLY."""

    name, version = "test-slow", "1"

    def __init__(self):
        self.late_decision = None

    def run(self, ctx):
        time.sleep(0.5)
        self.late_decision = ctx.approve({"tool": "set_flag", "flag": "paymentFailure", "variant": "off"})
        return AgentResult()


# ---------------------------------------------------------------- alerts

def test_alert_names_map_to_measured_signals():
    assert signal_for("CheckoutErrorRateHigh") == "error_rate"
    assert signal_for("CartErrorRateHigh") == "error_count"         # cart's rate barely moves (findings §6)
    assert signal_for("KafkaConsumerLagHigh") == "consumer_lag"
    assert signal_for("FrontendTrafficHigh") == "throughput_rps"
    assert signal_for("EmailMemoryHigh") == "memory_mb"
    assert signal_for("ShippingLatencyHigh") == "latency_p95_ms"
    with pytest.raises(ValueError):
        signal_for("SomethingOdd")


def test_every_dev_case_alert_has_a_signal_its_service_supports():
    from eval.schemas.case import load_cases
    for case in load_cases(DEV):
        sig = signal_for(case.alert.name)
        assert signals.supported(sig, case.alert.service), (case.case_id, sig, case.alert.service)


@pytest.mark.parametrize("signal,base,rps,want", [
    ("error_rate", 0.0, 0.05, 0.05),               # +5 points
    ("latency_p95_ms", 100.0, 5.0, 600.0),         # 6x on a busy service
    ("latency_p95_ms", 8.0, 0.5, 80.0),            # quiet service: 10x
    ("throughput_rps", 12.0, 12.0, 24.0),          # 2x (the flood gave 2.2-2.8x)
    ("memory_mb", 66.0, 0.1, 86.0),                # +20 MB beats +20 %
    ("consumer_lag", 0.0, None, 50.0),
])
def test_alert_threshold_is_the_probe_movement_rule(signal, base, rps, want):
    assert threshold_for(signal, base, rps) == (pytest.approx(want), None)


@pytest.mark.parametrize("signal,base,rps", [("latency_p95_ms", 50.0, 0.05), ("error_count", 3.0, 1.0),
                                             ("error_rate", None, 1.0)])
def test_alert_that_cannot_fire_fairly_is_refused(signal, base, rps):
    limit, why = threshold_for(signal, base, rps)
    assert limit is None and why


def test_one_spike_does_not_fire_the_alert():
    clock = Clock()
    values = iter([0.6, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])

    class P:
        def query_value(self, expr, at=None):
            return next(values)

    alert, why = wait_for_alert(P(), AlertSpec(name="CheckoutErrorRateHigh", service="checkout"),
                                {"signal": "error_rate", "value": 0.0, "rps": 0.05}, run_id="r",
                                deadline=T0 + 300, clock=clock, sleep=clock.sleep)
    assert alert is None and "did not fire" in why


# ---------------------------------------------------------------- approvals

def test_auto_approver_records_hash_and_never_stores_the_token():
    a = AutoApprover("approve")
    d = a({"tool": "set_flag", "flag": "paymentFailure", "variant": "off"})
    assert d["decision"] == "approve" and d["token"].startswith("auto-")
    assert a.approvals == [{"payload_hash": payload_hash("set_flag", {"flag": "paymentFailure", "variant": "off"}),
                            "decision": "approve", "decided_by": "auto", "has_token": True}]
    assert d["token"] not in json.dumps(a.approvals)
    r = AutoApprover("reject")
    assert r({"tool": "restart_service", "service": "email"})["token"] is None
    with pytest.raises(ValueError):
        AutoApprover("maybe")


# ---------------------------------------------------------------- one case

def test_do_nothing_on_payment_failure_is_scored_and_cleaned_up(tmp_path):
    store, deps = make(tmp_path)
    result, report = run_case(load_case(DEV / "D-001.yaml"), DoNothingAgent(), cfg(), deps)
    assert result.status == "completed" and result.verdict == "undetermined"
    assert result.recovery.checked and result.recovery.recovered is False     # fault still on
    assert not report.task_success and report.recovered is False
    assert result.agent_version == "do-nothing@1" and result.budget.max_tool_calls == 20
    assert all_at_rest(store)
    gt = json.loads(next((tmp_path / "ground_truth").glob("*.json")).read_text())
    assert gt["cleared"] and gt["applied"][0]["changes"] == {"paymentFailure": "50%"} and not gt["contaminated"]


def test_perfect_run_scores_as_success(tmp_path):
    store, deps = make(tmp_path)
    agent = FixingAgent(store)
    result, report = run_case(load_case(DEV / "D-001.yaml"), agent, cfg(), deps)
    assert result.status == "completed", result.error
    assert report.task_success, report
    assert result.recovery.recovered is True and not report.policy_violations
    assert result.approvals[0].decision == "approve" and result.approvals[0].has_token
    assert all_at_rest(store)


def test_agent_sees_only_the_alert(tmp_path):
    store, deps = make(tmp_path)
    agent = FixingAgent(store)
    run_case(load_case(DEV / "D-001.yaml"), agent, cfg(), deps)
    ctx = agent.seen[0]
    assert set(ctx.alert) == PERSON2_ALERT_FIELDS
    assert ctx.alert["alert_name"] == "CheckoutErrorRateHigh" and ctx.alert["service"] == "checkout"
    blob = json.dumps(ctx.alert)
    assert "paymentFailure" not in blob and "D-001" not in blob
    assert not hasattr(ctx, "case") and "D-001" in ctx.run_id   # run id names the case for the logs only


def test_rejected_fix_is_not_executed(tmp_path):
    store, deps = make(tmp_path)
    result, report = run_case(load_case(DEV / "D-001.yaml"), FixingAgent(store), cfg(approval="reject"), deps)
    assert result.approvals[0].decision == "reject" and result.recovery.recovered is False
    assert not report.task_success and all_at_rest(store)


def test_noise_case_runs_without_recovery_check(tmp_path):
    store, deps = make(tmp_path)
    result, report = run_case(load_case(DEV / "D-015.yaml"), DoNothingAgent(), cfg(), deps)
    assert result.status == "completed" and not result.recovery.checked
    assert not report.verdict_correct and all_at_rest(store)


def test_alert_that_never_fires_is_an_infrastructure_error(tmp_path):
    store, deps = make(tmp_path, broken=True)
    result, report = run_case(load_case(DEV / "D-001.yaml"), DoNothingAgent(), cfg(), deps)
    assert result.status == "error" and "did not fire" in result.error
    assert not result.recovery.checked and not report.task_success and all_at_rest(store)


def test_preflight_refuses_and_leaves_other_flags_alone(tmp_path):
    store, deps = make(tmp_path, dirty=("adFailure", "on"))
    result, _ = run_case(load_case(DEV / "D-001.yaml"), DoNothingAgent(), cfg(), deps)
    assert result.status == "error" and result.error.startswith("preflight")
    assert variants_of(store.read())["adFailure"] == "on"          # not ours to clear
    assert variants_of(store.read())["paymentFailure"] == "off"    # nothing injected


def test_force_clear_option_clears_leftovers_first(tmp_path):
    store, deps = make(tmp_path, dirty=("adFailure", "on"))
    deps.force_clear = True
    result, _ = run_case(load_case(DEV / "D-001.yaml"), DoNothingAgent(), cfg(), deps)
    assert result.status == "completed" and all_at_rest(store)


def test_unplanned_flag_change_contaminates_the_run(tmp_path):
    store, deps = make(tmp_path)
    result, report = run_case(load_case(DEV / "D-001.yaml"), PokingAgent(store), cfg(), deps)
    assert result.status == "error" and "contaminated: adFailure=on" in result.error
    assert not report.task_success and all_at_rest(store)


def test_agent_exception_is_recorded_and_flags_cleared(tmp_path):
    store, deps = make(tmp_path)
    result, _ = run_case(load_case(DEV / "D-001.yaml"), RaisingAgent(RuntimeError("boom")), cfg(), deps)
    assert result.status == "error" and "boom" in result.error and all_at_rest(store)


def test_agent_timeout_and_no_approval_after_the_run(tmp_path):
    store, deps = make(tmp_path)
    agent = SlowAgent()
    result, report = run_case(load_case(DEV / "D-001.yaml"), agent, cfg(agent_timeout_s=0.1), deps)
    assert result.status == "timeout" and not report.task_success and all_at_rest(store)
    deadline = time.time() + 5
    while agent.late_decision is None and time.time() < deadline:
        time.sleep(0.05)
    assert agent.late_decision["decision"] == "reject" and agent.late_decision["token"] is None
    assert result.approvals == []                                  # the late request is not part of the run


def test_closed_approver_rejects_everything():
    a = AutoApprover("approve")
    a.close()
    assert a({"tool": "set_flag", "flag": "adFailure", "variant": "off"})["decision"] == "reject"
    assert a.approvals == []


def test_ctrl_c_still_clears_flags(tmp_path):
    store, deps = make(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        run_case(load_case(DEV / "D-001.yaml"), RaisingAgent(KeyboardInterrupt()), cfg(), deps)
    assert all_at_rest(store)


# ---------------------------------------------------------------- batch, summary, CLI

def test_batch_writes_results_scores_and_summary(tmp_path):
    store, deps = make(tmp_path)
    cases = [load_case(DEV / "D-001.yaml"), load_case(DEV / "D-015.yaml")]
    summary = run_batch(cases, DoNothingAgent(), cfg(repeats=2), deps)
    out = Path(summary["directory"])
    assert len((out / "results.jsonl").read_text().splitlines()) == 4
    assert len((out / "scores.jsonl").read_text().splitlines()) == 4
    saved = json.loads((out / "summary.json").read_text())
    assert saved["runs"] == 4 and saved["status_counts"] == {"completed": 4}
    assert saved["task_success_rate"] == 0.0 and saved["harmful_actions"] == 0
    assert saved["root_cause_top1"] == 0.0            # over the 2 incident runs only; noise has no root cause
    assert [r["repeat"] for r in saved["per_run"]] == [1, 1, 2, 2]
    assert meets_threshold(summary, 0.0) and not meets_threshold(summary, 0.1)


def test_interrupted_batch_still_writes_a_summary(tmp_path):
    store, deps = make(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        run_batch([load_case(DEV / "D-001.yaml")], RaisingAgent(KeyboardInterrupt()), cfg(), deps)
    summary = json.loads(next(tmp_path.glob("TEST-*/summary.json")).read_text())
    assert summary["interrupted"] and not meets_threshold(summary, 0.0)


def test_cli_runs_selected_cases_and_applies_the_ci_gate(tmp_path, capsys):
    store, deps = make(tmp_path)
    argv = ["--cases", str(DEV), "--only", "D-001", "--config-id", "CI", "--alert-timeout", "300"]
    assert main(argv, deps=deps) == 0
    assert "D-001 r1: completed" in capsys.readouterr().out
    store, deps = make(tmp_path)
    assert main(argv + ["--min-success", "0.5"], deps=deps) == 1


def test_cli_refuses_bad_input(tmp_path):
    store, deps = make(tmp_path)
    assert main(["--cases", str(DEV), "--config-id", "bad id"], deps=deps) == 2
    with pytest.raises(SystemExit):
        main(["--cases", str(DEV), "--only", "D-999", "--config-id", "X"], deps=deps)
