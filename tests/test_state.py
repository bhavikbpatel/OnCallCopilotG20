from datetime import datetime, timezone

import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from copilot import state as st
from copilot.state import (
    AgentSpend, BudgetLedger, EvidenceItem, Hypothesis, RestartService, SetFlag,
    add_and_trim, keep_last_n_per_hypothesis, merge_by_id, payload_hash, sum_ledgers,
)

NOW = datetime(2026, 11, 3, 10, 15, tzinfo=timezone.utc)


def hyp(id, status="open"):
    return Hypothesis(id=id, service="payment", fault_type="paymentFailure", status=status)


def ev(h, e):
    return EvidenceItem(evidence_id=e, hypothesis_id=h, summary=e, at=NOW)


def test_merge_by_id_right_wins_and_keeps_order():
    out = merge_by_id([hyp("h1"), hyp("h2")], [hyp("h2", "refuted"), hyp("h3")])
    assert [h.id for h in out] == ["h1", "h2", "h3"]
    assert out[1].status == "refuted"


def test_merge_by_id_handles_none():
    assert merge_by_id(None, [hyp("h1")])[0].id == "h1"


def test_evidence_keeps_last_n_per_hypothesis():
    left = [ev("h1", f"e{i}") for i in range(st.EVIDENCE_PER_HYPOTHESIS)] + [ev("h2", "x")]
    out = keep_last_n_per_hypothesis(left, [ev("h1", "new")])
    h1 = [e.evidence_id for e in out if e.hypothesis_id == "h1"]
    assert len(h1) == st.EVIDENCE_PER_HYPOTHESIS
    assert h1[-1] == "new" and "e0" not in h1
    assert any(e.hypothesis_id == "h2" for e in out)


def test_evidence_dedupes_by_id():
    out = keep_last_n_per_hypothesis([ev("h1", "a")], [ev("h1", "a")])
    assert len(out) == 1


def test_sum_ledgers_adds_per_agent():
    a = BudgetLedger(by_agent={"triage": AgentSpend(tool_calls=2, usd=0.1)})
    b = BudgetLedger(by_agent={"triage": AgentSpend(tool_calls=1), "diag:h1": AgentSpend(tool_calls=3)})
    out = sum_ledgers(a, b)
    assert out.by_agent["triage"].tool_calls == 3
    assert out.total.tool_calls == 6


def test_messages_trimmed():
    msgs = [HumanMessage(content=str(i), id=str(i)) for i in range(st.MESSAGES_KEPT + 5)]
    out = add_and_trim([], msgs)
    assert len(out) == st.MESSAGES_KEPT and out[-1].content == str(st.MESSAGES_KEPT + 4)


def test_payload_hash_changes_with_args():
    a = payload_hash(RestartService(service="cart"))
    assert a == payload_hash(RestartService(service="cart"))
    assert a != payload_hash(RestartService(service="checkout"))


def test_action_enums_reject_free_text():
    with pytest.raises(ValidationError):
        RestartService(service="all")
    with pytest.raises(ValidationError):
        st.ScaleService(service="ad", replicas=100)
    SetFlag(flag="paymentFailure", variant="off")
