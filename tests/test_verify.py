"""Verify, follow-up gate and rollback routing — code decides, so no LLM is needed."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from copilot.agents import remediation
from copilot.clients import RecoveryCount
from copilot.config import AppSettings
from copilot.routing import make_routes
from copilot.state import (
    ActionOutcome, Alert, BudgetLimits, DryRunResult, RestartService, ScaleService, SetFlag, payload_hash,
)

FIXED_AT = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
FAST = {"verify_initial_delay_s": 0, "verify_poll_s": 1, "verify_window_s": 2, "verify_window_by_signal": {}}


class Telemetry:
    def __init__(self, value=None, count=None):
        self.value, self.count, self.since = value, count, None

    async def errors_since(self, service, since):
        self.since = since
        return self.count

    async def signal_value(self, signal, service, window="2m"):
        return self.value


def alert(signal="error_rate", service="cart", threshold=0.05):
    return Alert(alert_name="a", service=service, signal=signal, summary="s", query="q", threshold=threshold,
                 value=1.0, fired_at=FIXED_AT, fingerprint="f")


def state(fix, signal="error_rate", service="cart", threshold=0.05, **extra):
    return {"alert": alert(signal, service, threshold), "executed_at": FIXED_AT, "limits": BudgetLimits(),
            "outcome": ActionOutcome(action=fix, approved_hash=payload_hash(fix), verified=False, rolled_back=False),
            **extra}


def run_verify(st, telemetry, **settings):
    s = AppSettings().model_copy(update={**FAST, "verify_settle_s": 0, "follow_up_signals": ["memory_mb"], **settings})
    return asyncio.run(remediation.make_verify(SimpleNamespace(settings=s, telemetry=telemetry))(st)), s


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr(remediation.asyncio, "sleep", instant)


CART_OFF = SetFlag(flag="cartFailure", variant="off")
EMAIL_OFF = SetFlag(flag="emailMemoryLeak", variant="off")


def test_recovered():
    out, _ = run_verify(state(CART_OFF), Telemetry(count=RecoveryCount(errors=0, requests=20, error_rate=0.0)))
    assert out["outcome"].verified and out["final_status"] == "recovered"


def test_errors_are_counted_from_settle_time():
    t = Telemetry(count=RecoveryCount(errors=0, requests=20, error_rate=0.0))
    run_verify(state(CART_OFF), t, verify_settle_s=90, verify_initial_delay_s=0)
    assert t.since == FIXED_AT + timedelta(seconds=90)


def test_too_little_traffic_escalates_never_rolls_back():
    out, s = run_verify(state(CART_OFF), Telemetry(count=RecoveryCount(errors=0, requests=2, error_rate=0.0)))
    assert out["escalation_reason"] and not out["outcome"].verified
    assert make_routes(s)["after_verify"](state(CART_OFF) | out) == "escalate"


def test_nan_reading_is_not_judged():
    out, _ = run_verify(state(EMAIL_OFF, "memory_mb", "email", 300), Telemetry(value=None))
    assert out["escalation_reason"]


def test_error_rate_not_recovered_rolls_back_without_follow_up():
    """cartFailure fixed but still erroring: no restart invited (restart cart is a forbidden action)."""
    out, s = run_verify(state(CART_OFF), Telemetry(count=RecoveryCount(errors=5, requests=20, error_rate=0.25)))
    assert not out["outcome"].verified and "follow_up_of" not in out
    assert make_routes(s)["after_verify"](state(CART_OFF) | out) == "rollback"


def test_memory_after_flag_off_gets_one_follow_up():
    st = state(EMAIL_OFF, "memory_mb", "email", 300)
    out, s = run_verify(st, Telemetry(value=620.0))
    assert out["follow_up_of"] == EMAIL_OFF
    assert make_routes(s)["after_verify"](st | out) == "propose_action"


def test_memory_after_a_restart_does_not_get_a_follow_up():
    st = state(RestartService(service="email"), "memory_mb", "email", 300)
    out, s = run_verify(st, Telemetry(value=620.0))
    assert "follow_up_of" not in out and make_routes(s)["after_verify"](st | out) == "rollback"


def test_second_failure_after_follow_up_rolls_back():
    st = state(RestartService(service="email"), "memory_mb", "email", 300, follow_up_of=EMAIL_OFF)
    out, s = run_verify(st, Telemetry(value=620.0))
    assert make_routes(s)["after_verify"](st | out) == "rollback"


def test_throughput_recovers_above_threshold():
    st = state(CART_OFF, "throughput_rps", "frontend", 10)
    out, _ = run_verify(st, Telemetry(value=25.0))
    assert out["outcome"].verified


def test_window_per_signal():
    calls = []

    class Counting(Telemetry):
        async def signal_value(self, *a, **k):
            calls.append(1)
            return 620.0

    run_verify(state(RestartService(service="email"), "memory_mb", "email", 300), Counting(),
               verify_window_by_signal={"memory_mb": 4})
    assert len(calls) == 5          # window 4 s, poll 1 s → 5 readings instead of the default 3


@pytest.mark.parametrize("action,ok", [
    (RestartService(service="email"), True),
    (RestartService(service="cart"), False),          # not the alerting service
    (ScaleService(service="email", replicas=2), False),
    (SetFlag(flag="emailMemoryLeak", variant="off"), False),
])
def test_follow_up_must_restart_the_alerting_service(monkeypatch, action, ok):
    async def fake_call(deps, agent, prompt, schema, view):
        return schema(action=action, expected_effect="e", rationale="r"), {}

    monkeypatch.setattr(remediation, "structured_call", fake_call)
    st = state(EMAIL_OFF, "memory_mb", "email", 300, follow_up_of=EMAIL_OFF, action_purpose="follow_up",
               dry_run=DryRunResult(would_do="x", expected_effect="e"))
    out = asyncio.run(remediation.make_propose_action(SimpleNamespace())(st))
    assert ("proposed_action" in out and out["proposed_action"] == action) == ok
    if not ok:                                     # refused follow-up → rollback, never a half-done fix
        assert out["proposed_action"] is None and make_routes(AppSettings())["after_propose"](st | out) == "rollback"


def test_failed_fix_is_never_proposed_again(monkeypatch):
    async def fake_call(deps, agent, prompt, schema, view):
        return schema(action=CART_OFF, expected_effect="e", rationale="r"), {}

    monkeypatch.setattr(remediation, "structured_call", fake_call)
    st = {"alert": alert(), "failed_actions": [payload_hash(CART_OFF)]}
    out = asyncio.run(remediation.make_propose_action(SimpleNamespace())(st))
    assert "already failed" in out["escalation_reason"]


@pytest.mark.parametrize("variant,ok", [("off", True), ("50%", True), ("on", False)])
def test_set_flag_variant_must_exist(variant, ok):
    if ok:
        SetFlag(flag="paymentFailure", variant=variant)
    else:
        with pytest.raises(ValueError):
            SetFlag(flag="paymentFailure", variant=variant)


def test_verify_readings_do_not_use_the_agents_tool_call_budget():
    """E4 budget 5: a long memory verify (many polls) must not exhaust the agent's budget
    (docs/eval-result-contract.md: code-driven calls do not count)."""
    from copilot.policy import budget_exhausted
    from copilot.state import BudgetLedger, sum_ledgers
    out, _ = run_verify(state(EMAIL_OFF, "memory_mb", "email", 300), Telemetry(value=620.0),
                        verify_window_by_signal={"memory_mb": 30})          # 31 polls, never recovers
    spent = out["budget"].total
    assert spent.code_calls >= 6 and spent.tool_calls == 0
    agent_so_far = BudgetLedger(by_agent={"diagnosis:h1": {"tool_calls": 4}})
    assert not budget_exhausted(sum_ledgers(agent_so_far, out["budget"]), BudgetLimits(tool_calls=5))


def test_code_calls_add_up_and_are_kept_apart_from_tool_calls():
    from copilot.agents.base import spend
    from copilot.state import sum_ledgers
    total = sum_ledgers(spend("remediation", tool_calls=1), spend("remediation", code_calls=3)).total
    assert (total.tool_calls, total.code_calls) == (1, 3)
