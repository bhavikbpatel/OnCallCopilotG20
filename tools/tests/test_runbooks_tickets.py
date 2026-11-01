"""runbooks-kb (wraps Person 1's index) and tickets (incident record, gated publish_status)."""
from __future__ import annotations

from eval.schemas import payload_hash

from tools.approval import make_token
from tools.runbooks_kb.server import Runbooks
from tools.tickets.server import Tickets

SECRET = "test-secret"
T0 = 1_800_000_000.0     # the fixed clock the Tickets under test runs on


def code(result):
    return result["error"]["code"]


# ---------------------------------------------------------------- runbooks-kb

def test_search_returns_cited_passages():
    kb = Runbooks()
    r = kb.search("payment errors checkout failing", service="payment", k=3)
    assert r["ok"] and 1 <= r["data"]["count"] <= 3
    first = r["data"]["passages"][0]
    assert first["passage_id"] and first["doc_id"] and len(first["sha256"]) == 64
    assert "reference data" in r["data"]["notice"]


def test_search_validation():
    kb = Runbooks()
    assert code(kb.search("")) == "bad_argument"
    assert code(kb.search("x", k=0)) == "bad_argument"
    assert code(kb.search("x", k=99)) == "bad_argument"
    assert code(kb.search("x", service="not-a-service")) == "bad_argument"


def test_get_returns_a_whole_runbook_and_unknown_is_an_error():
    kb = Runbooks()
    doc_id = kb.search("payment errors", k=1)["data"]["passages"][0]["doc_id"]
    r = kb.get(doc_id)
    assert r["ok"] and r["data"]["runbook_id"] == doc_id and r["data"]["passages"]
    assert code(kb.get("RB-999")) == "not_found"
    assert code(kb.get("")) == "bad_argument"


# ---------------------------------------------------------------- tickets

def make():
    return Tickets(":memory:", secret=SECRET, clock=lambda: T0)


def test_internal_writes_need_no_approval_and_build_a_timeline():
    t = make()
    iid = t.create_incident("CheckoutErrorRate", "checkout", "SEV2")["data"]["incident_id"]
    assert iid == "INC-0001"
    assert t.append_timeline(iid, "triage: payment suspected", "triage")["ok"]
    got = t.get_incident(iid)["data"]
    assert got["severity"] == "SEV-2" and got["timeline"][0]["event"] == "triage: payment suspected"
    assert t.save_postmortem(iid, "Root cause: paymentFailure flag.")["ok"]


def test_ticket_validation_and_redaction():
    t = make()
    assert code(t.create_incident("")) == "bad_argument"
    assert code(t.create_incident("alert", service="nope")) == "unknown_service"
    assert code(t.create_incident("alert", severity="P1")) == "bad_argument"
    assert code(t.append_timeline("INC-0099", "x")) == "not_found"
    iid = t.create_incident("alert")["data"]["incident_id"]
    t.append_timeline(iid, "db password=hunter2 seen in log")
    assert "hunter2" not in t.get_incident(iid)["data"]["timeline"][0]["event"]


def test_draft_status_gives_the_exact_payload_and_hash_to_approve():
    t = make()
    iid = t.create_incident("CheckoutErrorRate", "checkout", "SEV-2")["data"]["incident_id"]
    t.append_timeline(iid, "flag switched off", "remediation")
    d = t.draft_status(iid)["data"]
    assert iid in d["text"] and "flag switched off" in d["text"]
    assert d["payload_hash"] == payload_hash("publish_status", d["payload"])


def test_publish_status_needs_a_token_for_the_exact_text():
    t = make()
    iid = t.create_incident("CheckoutErrorRate", "checkout")["data"]["incident_id"]
    d = t.draft_status(iid)["data"]
    assert code(t.publish_status(iid, d["text"], "")) == "approval_required"
    token = make_token(d["payload_hash"], secret=SECRET, now=T0)
    # the text was edited after approval: the old token must not work
    assert code(t.publish_status(iid, d["text"] + " All fixed!", token)) == "approval_hash_mismatch"
    ok = t.publish_status(iid, d["text"], token)
    assert ok["ok"] and ok["data"]["published"]
    assert code(t.publish_status(iid, d["text"], token)) == "approval_already_used"


def test_publish_status_refuses_secrets_in_the_note():
    t = make()
    iid = t.create_incident("alert")["data"]["incident_id"]
    text = "Cause found. api_key=sk-abc123 was in the log."
    token = make_token(payload_hash("publish_status", {"incident_id": iid, "text": text}), secret=SECRET, now=T0)
    assert code(t.publish_status(iid, text, token)) == "secret_in_text"
