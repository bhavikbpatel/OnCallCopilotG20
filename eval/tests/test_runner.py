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
    ("slow_requests", 0.0, 0.2, 1.0),             # one request slower than 1 s
])
def test_alert_threshold_is_the_probe_movement_rule(signal, base, rps, want):
    assert threshold_for(signal, base, rps) == (pytest.approx(want), None)


@pytest.mark.parametrize("signal,base,rps", [("latency_p95_ms", 50.0, 0.05), ("error_count", 3.0, 1.0),
                                             ("slow_requests", 2.0, 0.2),
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

def test_auto_approver_names_the_payload_and_issues_no_token():
    """#37: Person 2's gate mints the token and refuses an approval whose hash is not the proposal's."""
    a = AutoApprover("approve")
    d = a({"tool": "set_flag", "flag": "paymentFailure", "variant": "off"})
    digest = payload_hash("set_flag", {"flag": "paymentFailure", "variant": "off"})
    assert d["decision"] == "approve" and d["payload_hash"] == digest and d["token"] is None
    assert a.approvals == [{"payload_hash": digest, "decision": "approve", "decided_by": "auto",
                            "has_token": None}]                     # unknown to the approver, never "False"
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
    assert result.approvals[0].decision == "approve" and result.approvals[0].has_token is None
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


def test_approver_calls_back_only_on_approval():
    seen = []
    a = AutoApprover("approve", on_approved=lambda tool, args: seen.append((tool, args)))
    a({"tool": "set_flag", "flag": "paymentFailure", "variant": "off"})
    assert seen == [("set_flag", {"flag": "paymentFailure", "variant": "off"})]
    a.close()
    a({"tool": "set_flag", "flag": "adFailure", "variant": "off"})
    r = AutoApprover("reject", on_approved=lambda tool, args: seen.append((tool, args)))
    r({"tool": "set_flag", "flag": "cartFailure", "variant": "off"})
    assert len(seen) == 1


def test_approved_fix_releases_the_flag_from_the_plan(tmp_path):
    store, deps = make(tmp_path)
    result, report = run_case(load_case(DEV / "D-001.yaml"), FixingAgent(store), cfg(), deps)
    gt = json.loads(next((tmp_path / "ground_truth").glob("*.json")).read_text())
    assert [r["flag"] for r in gt["released"]] == ["paymentFailure"] and report.task_success


def test_rejected_fix_does_not_release_the_flag(tmp_path):
    store, deps = make(tmp_path)
    run_case(load_case(DEV / "D-001.yaml"), FixingAgent(store), cfg(approval="reject"), deps)
    gt = json.loads(next((tmp_path / "ground_truth").glob("*.json")).read_text())
    assert gt["released"] == []


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


# ---------------------------------------------------------------- after #37: gate tokens, secret, timeouts

class GateAgent(FixingAgent):
    """Fixes like FixingAgent and returns the gate's approval record, as Person 2's final state does. TEST ONLY."""

    name = "test-gate"
    needs_approval_secret = True

    def __init__(self, store, token):
        super().__init__(store)
        self.token = token

    def run(self, ctx):
        res = super().run(ctx)
        h = payload_hash("set_flag", {"flag": "paymentFailure", "variant": "off"})
        res.state["approval"] = {"payload_hash": h, "decision": "approve", "token": self.token,
                                 "decided_by": "auto", "decided_at": "2026-11-02T00:00:00Z"}
        return res


def test_has_token_comes_from_the_gate_record(tmp_path):
    store, deps = make(tmp_path)
    result, report = run_case(load_case(DEV / "D-001.yaml"), GateAgent(store, "v1.minted"), cfg(), deps)
    assert result.approvals[0].has_token is True and not report.policy_violations


def test_write_on_an_approval_the_gate_gave_no_token_is_a_violation(tmp_path):
    store, deps = make(tmp_path)
    result, report = run_case(load_case(DEV / "D-001.yaml"), GateAgent(store, None), cfg(), deps)
    assert result.approvals[0].has_token is False
    assert any("no token" in v for v in report.policy_violations)


def test_with_gate_tokens_fills_only_the_matching_latest_approval():
    from eval.runner.approvals import with_gate_tokens
    recs = [{"payload_hash": "a", "decision": "approve", "decided_by": "auto", "has_token": None},
            {"payload_hash": "b", "decision": "approve", "decided_by": "auto", "has_token": None},
            {"payload_hash": "b", "decision": "approve", "decided_by": "auto", "has_token": None}]
    out = with_gate_tokens(recs, {"approval": {"payload_hash": "b", "decision": "approve", "token": "t"}})
    assert [r["has_token"] for r in out] == [None, None, True]
    assert recs[2]["has_token"] is None                                     # input left alone
    assert [r["has_token"] for r in with_gate_tokens(recs, {})] == [None, None, None]
    rej = {"approval": {"payload_hash": "b", "decision": "reject", "token": None}}
    assert [r["has_token"] for r in with_gate_tokens(recs, rej)] == [None, None, None]


def test_agent_needing_the_approval_secret_does_not_start_without_it(tmp_path):
    from eval.runner.agents import not_ready
    store, deps = make(tmp_path)
    agent = GateAgent(store, "t")
    missing = tmp_path / "no.env"
    assert "APPROVAL_SECRET" in not_ready(agent, env={}, env_file=missing)
    assert not_ready(agent, env={"APPROVAL_SECRET": "s"}, env_file=missing) is None
    envf = tmp_path / ".env"
    envf.write_text("APPROVAL_SECRET=from-file\n")
    assert not_ready(agent, env={}, env_file=envf) is None
    assert not_ready(DoNothingAgent(), env={}, env_file=missing) is None    # never reaches the gate


def test_batch_and_cli_refuse_an_agent_without_the_secret(tmp_path, monkeypatch, capsys):
    import eval.runner.agents as agents_mod
    monkeypatch.delenv("APPROVAL_SECRET", raising=False)
    monkeypatch.setattr(agents_mod, "ENV_FILE", tmp_path / "absent.env")
    store, deps = make(tmp_path)
    with pytest.raises(RuntimeError, match="APPROVAL_SECRET"):
        run_batch([load_case(DEV / "D-001.yaml")], GateAgent(store, "t"), cfg(), deps)
    assert not any(tmp_path.glob("TEST-*"))                                 # nothing written, nothing injected
    assert all_at_rest(store)
    monkeypatch.setitem(agents_mod.AGENTS, "do-nothing", lambda: GateAgent(store, "t"))
    assert main(["--cases", str(DEV), "--only", "D-001", "--config-id", "X"], deps=deps) == 2
    assert "APPROVAL_SECRET" in capsys.readouterr().err


def test_default_agent_timeout_covers_the_graph_verify_windows():
    from eval.runner.timeouts import APP_YAML, GraphTiming, agent_timeout_for
    t = GraphTiming.load(APP_YAML)
    assert t.verify_window_by_signal.get("memory_mb", 0) >= t.verify_window_s
    for signal in ("error_rate", "error_count", "latency_p95_ms", "consumer_lag", "memory_mb"):
        window = t.verify_window_by_signal.get(signal, t.verify_window_s)
        verifies = 2 if signal in t.follow_up_signals else 1
        timeout = agent_timeout_for(signal, t, 300)
        assert timeout >= (1 + t.max_replans) * verifies * window > 600     # the old fixed 600 s was too short
    toy = GraphTiming(verify_window_s=100, verify_window_by_signal={"memory_mb": 200}, verify_poll_s=10,
                      follow_up_signals=("memory_mb",), max_replans=1)
    assert agent_timeout_for("error_rate", toy, 50) == 2 * (50 + 110)
    assert agent_timeout_for("memory_mb", toy, 50) == 2 * (50 + 2 * 210)
    assert GraphTiming.load(Path("/nonexistent/app.yaml")) == GraphTiming()


def test_runner_uses_the_worked_out_timeout_unless_one_is_given(tmp_path):
    from eval.runner.timeouts import GraphTiming, agent_timeout_for

    class TimedAgent(FixingAgent):
        def run(self, ctx):
            self.left = ctx.deadline - ctx.clock()          # time given, read when the agent starts
            return super().run(ctx)

    store, deps = make(tmp_path)
    deps.timing = GraphTiming(verify_window_s=100, verify_poll_s=10, max_replans=0)
    agent = TimedAgent(store)
    run_case(load_case(DEV / "D-001.yaml"), agent, cfg(), deps)
    assert agent.left == agent_timeout_for("error_rate", deps.timing, 300) == 410
    store, deps = make(tmp_path)
    agent = TimedAgent(store)
    run_case(load_case(DEV / "D-001.yaml"), agent, cfg(agent_timeout_s=42), deps)
    assert agent.left == 42


def test_preflight_asks_the_agent_whether_it_can_run_the_options():
    from eval.runner.agents import AGENTS, not_ready as preflight

    class NeedsDb:
        name, version = "needs-db", "1"
        options_supported = frozenset({"memory"})

        def options_problem(self, options):
            return "no database" if options.get("memory") == "playbook" else None

        def run(self, ctx):  # pragma: no cover - never run
            raise AssertionError

    assert "no database" in preflight(NeedsDb(), options={"memory": "playbook"})
    assert preflight(NeedsDb(), options={"memory": "off"}) is None
    assert callable(AGENTS["copilot"])          # registered, imported only when chosen (no LangGraph needed here)
