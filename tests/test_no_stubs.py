"""Values the graph acts on come from real tools: gate-minted tokens, tool-grounded evidence, windowed preconditions."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from copilot.agents import remediation
from copilot.agents.diagnosis import grounded, tool_strings
from copilot.memory.preconditions import evaluate_lesson
from copilot.memory.schemas import Lesson, Precondition
from copilot.state import Alert, DryRunResult, SetFlag, payload_hash
from mcp_servers.common.approval import check_token

FIX = SetFlag(flag="paymentFailure", variant="off")
H = payload_hash(FIX)                                      # what the approver is shown in the interrupt
ALERT = Alert(alert_name="CheckoutErrorRateHigh", service="checkout", signal="error_rate", summary="s",
              query="q", threshold=0.05, value=0.4, fired_at=datetime.now(timezone.utc), fingerprint="f")


def gate(monkeypatch, decision):
    monkeypatch.setattr(remediation, "interrupt", lambda payload: decision)
    state = {"proposed_action": FIX, "incident_id": "INC-1", "alert": ALERT, "action_purpose": "fix",
             "dry_run": DryRunResult(would_do="x", expected_effect="y", current_state={})}
    return asyncio.run(remediation.make_approval_gate(None)(state))


def test_gate_mints_the_token_and_ignores_a_stub_one(monkeypatch):
    monkeypatch.setenv("APPROVAL_SECRET", "test-secret-0123456789abcdef")
    out = gate(monkeypatch, {"decision": "approve", "token": "tok-fake", "decided_by": "runner", "payload_hash": H})
    token = out["approval"].token
    assert token != "tok-fake" and check_token(payload_hash(FIX), token) is None
    assert "escalation_reason" not in out


def test_gate_without_a_secret(monkeypatch):
    monkeypatch.delenv("APPROVAL_SECRET", raising=False)
    monkeypatch.delenv("APPROVAL_DEV_MODE", raising=False)
    out = gate(monkeypatch, {"decision": "approve", "decided_by": "runner", "payload_hash": H})
    assert out["approval"].token is None and "approval_not_configured" in out["escalation_reason"]
    out = gate(monkeypatch, {"decision": "approve", "token": "tok:1:abc", "decided_by": "runner", "payload_hash": H})
    assert out["approval"].token == "tok:1:abc"          # passed on; the ops server is the one that checks it


def test_reject_never_gets_a_token(monkeypatch):
    monkeypatch.setenv("APPROVAL_SECRET", "test-secret-0123456789abcdef")
    out = gate(monkeypatch, {"decision": "reject", "reason": "no", "decided_by": "x"})
    assert out["approval"].token is None and out["proposed_action"] is None


def test_evidence_must_come_from_a_tool_result():
    promql = 'promql:sum(rate(calls{service_name="payment"}[5m])):15m'
    result = json.dumps({"ok": True, "result": {"series": [], "evidence_id": promql}})
    msgs = [AIMessage(content=""), ToolMessage([{"type": "text", "text": result}], tool_call_id="1")]
    seen = tool_strings(msgs)
    assert grounded(promql, seen)                          # quotes survive the JSON decoding
    assert not grounded("trace:made-up", seen) and not grounded("  ", seen)


@pytest.mark.parametrize("age_min,passed", [(5, True), (120, False)])
def test_recent_change_precondition_uses_its_window(age_min, passed):
    now = datetime.now(timezone.utc)

    class Telemetry:                                       # the server lists every active flag, whatever `since` is
        async def recent_changes(self, since):
            return [{"flag": "paymentFailure", "variant": "50%", "at": now - timedelta(minutes=age_min)}]

    lesson = Lesson(lesson_id="L1", text="t", service="payment", fault_family="f", source_incident="I",
                    reviewed_by="r", preconditions=[Precondition(check="recent_change",
                                                                 args={"flag": "paymentFailure", "within": "30m"})])
    ok, _ = asyncio.run(evaluate_lesson(lesson, Telemetry(), now))
    assert ok is passed



# ---- approval bound to the exact payload the approver saw (review of #37) ----

@pytest.mark.parametrize("shown,why", [
    (None, "approval_without_payload_hash"),
    ("0" * 64, "stale_approval"),
    (payload_hash(SetFlag(flag="paymentFailure", variant="50%")), "stale_approval"),   # a different action's hash
])
def test_approval_for_another_or_no_payload_is_refused(monkeypatch, shown, why):
    monkeypatch.setenv("APPROVAL_SECRET", "test-secret-0123456789abcdef")
    decision = {"decision": "approve", "decided_by": "runner", "token": "tok:1:abc"}
    if shown is not None:
        decision["payload_hash"] = shown
    out = gate(monkeypatch, decision)
    assert out["approval"].token is None and why in out["escalation_reason"]
    assert "proposed_action" not in out                    # nothing changes hands; the graph escalates


def test_matching_approval_gets_a_token_bound_to_that_payload(monkeypatch):
    monkeypatch.setenv("APPROVAL_SECRET", "test-secret-0123456789abcdef")
    out = gate(monkeypatch, {"decision": "approve", "decided_by": "runner", "payload_hash": H})
    assert "escalation_reason" not in out and check_token(H, out["approval"].token) is None
    other = payload_hash(SetFlag(flag="paymentFailure", variant="50%"))
    assert check_token(other, out["approval"].token) is not None      # useless for any other action


def test_missing_hash_is_refused_even_without_a_secret(monkeypatch):
    monkeypatch.delenv("APPROVAL_SECRET", raising=False)
    monkeypatch.delenv("APPROVAL_DEV_MODE", raising=False)
    out = gate(monkeypatch, {"decision": "approve", "decided_by": "runner", "token": "tok:1:abc"})
    assert out["approval"].token is None and "approval_without_payload_hash" in out["escalation_reason"]


def test_mint_token_passes_the_approvers_hash_to_resolve(monkeypatch):
    """resolve() itself refuses a stale hash: the gate no longer hands it its own hash."""
    monkeypatch.setenv("APPROVAL_SECRET", "test-secret-0123456789abcdef")
    token, why = remediation.mint_token(FIX, H, "runner", "0" * 64)
    assert token is None and why == "stale_approval"
    token, why = remediation.mint_token(FIX, H, "runner", H)
    assert token and why == ""


# ---- evidence ids match exactly (review of #37) ----

def test_short_ids_inside_longer_values_are_not_grounded():
    seen = ['promql:sum(rate(calls{service_name="payment"}[5m])):15m', "payment error_rate rose to 0.31", "tr-9f1"]
    assert not grounded("payment", seen) and not grounded("error_rate", seen) and not grounded("tr-9f", seen)
    assert grounded("tr-9f1", seen) and grounded(" tr-9f1 ", seen)
    assert grounded('promql:sum(rate(calls{service_name="payment"}[5m])):15m', seen)
