
"""runbooks_kb (wraps Person 1's index) and tickets (incident record, gated publish_status)."""
from __future__ import annotations

import json

from eval.schemas import payload_hash

from mcp_servers.common.approval import make_token
from mcp_servers.runbooks_kb.server import Runbooks
from mcp_servers.tickets.server import Tickets

SECRET = "test-secret"
T0 = 1_800_000_000.0     # the fixed clock the Tickets under test runs on


def code(result):
    return result["error"]["code"]


# ---------------------------------------------------------------- runbooks_kb

def test_search_returns_cited_passages():
    kb = Runbooks()
    r = kb.search("payment errors checkout failing", service="payment", k=3)
    assert r["ok"] and 1 <= r["result"]["count"] <= 3
    first = r["result"]["passages"][0]
    assert first["passage_id"] and first["doc_id"] and len(first["sha256"]) == 64
    assert "reference data" in r["result"]["notice"]


def test_search_validation():
    kb = Runbooks()
    assert code(kb.search("")) == "bad_argument"
    assert code(kb.search("x", k=0)) == "bad_argument"
    assert code(kb.search("x", k=99)) == "bad_argument"
    assert code(kb.search("x", service="not-a-service")) == "bad_argument"


def test_get_returns_a_whole_runbook_and_unknown_is_an_error():
    kb = Runbooks()
    doc_id = kb.search("payment errors", k=1)["result"]["passages"][0]["doc_id"]
    r = kb.get(doc_id)
    assert r["ok"] and r["result"]["runbook_id"] == doc_id and r["result"]["passages"]
    assert code(kb.get("RB-999")) == "not_found"
    assert code(kb.get("")) == "bad_argument"


# ---------------------------------------------------------------- tickets

def make():
    return Tickets(":memory:", secret=SECRET, clock=lambda: T0)


def test_internal_writes_need_no_approval_and_build_a_timeline():
    t = make()
    iid = t.create_incident(alert_name="CheckoutErrorRate", service="checkout", severity="SEV2")["result"]["incident_id"]
    assert iid == "INC-0001"
    assert t.append_timeline(iid, "triage: payment suspected", source="triage")["ok"]
    got = t.get_incident(iid)["result"]
    assert got["severity"] == "SEV2" and got["timeline"][0]["event"] == "triage: payment suspected"
    assert t.save_postmortem(iid, "Root cause: paymentFailure flag.")["ok"]


def test_ticket_validation_and_redaction():
    t = make()
    assert code(t.create_incident(incident_id="bad id!")) == "bad_argument"
    assert code(t.create_incident(alert_name="alert", service="nope")) == "unknown_service"
    assert code(t.create_incident(alert_name="alert", severity="P1")) == "bad_argument"
    assert code(t.append_timeline("INC-0099", "x")) == "not_found"
    iid = t.create_incident(alert_name="alert")["result"]["incident_id"]
    t.append_timeline(iid, "db password=hunter2 seen in log")
    assert "hunter2" not in t.get_incident(iid)["result"]["timeline"][0]["event"]


def test_draft_status_gives_the_exact_payload_and_hash_to_approve():
    t = make()
    iid = t.create_incident(alert_name="CheckoutErrorRate", service="checkout", severity="SEV-2")["result"]["incident_id"]
    t.append_timeline(iid, "flag switched off", source="remediation")
    d = t.draft_status(iid)["result"]
    assert iid in d["text"] and "flag switched off" in d["text"]
    assert d["payload_hash"] == payload_hash("publish_status", d["payload"])


def test_publish_status_needs_a_token_for_the_exact_text():
    t = make()
    iid = t.create_incident(alert_name="CheckoutErrorRate", service="checkout")["result"]["incident_id"]
    d = t.draft_status(iid)["result"]
    assert code(t.publish_status(iid, d["text"], "")) == "approval_required"
    token = make_token(d["payload_hash"], secret=SECRET, now=T0)
    # the text was edited after approval: the old token must not work
    assert code(t.publish_status(iid, d["text"] + " All fixed!", token)) == "approval_hash_mismatch"
    ok = t.publish_status(iid, d["text"], token)
    assert ok["ok"] and ok["result"]["published"]
    assert code(t.publish_status(iid, d["text"], token)) == "approval_already_used"


def test_publish_status_refuses_secrets_in_the_note():
    t = make()
    iid = t.create_incident(alert_name="alert")["result"]["incident_id"]
    text = "Cause found. api_key=sk-abc123 was in the log."
    token = make_token(payload_hash("publish_status", {"incident_id": iid, "text": text}), secret=SECRET, now=T0)
    assert code(t.publish_status(iid, text, token)) == "secret_in_text"


GRAPH_ALERT = {   # copilot/state.py Alert, as McpTickets.create_incident sends it (model_dump(mode="json"))
    "alert_name": "CheckoutErrorRateHigh", "service": "checkout", "signal": "error_rate", "summary": "errors up",
    "query": "sum(rate(x[5m]))", "threshold": 0.05, "value": 0.4, "fired_at": "2026-11-01T10:00:00Z", "fingerprint": "f1"}


def test_tickets_accept_the_graphs_call_shapes():
    """copilot/tools.py McpTickets sends: create_incident{incident_id, alert, severity},
    append_timeline{incident_id, at, text}, save_postmortem{incident_id, postmortem}."""
    t = make()
    r = t.create_incident(incident_id="inc-20261101-1", alert=GRAPH_ALERT, severity="SEV2")
    assert r["ok"] and r["result"]["incident_id"] == "inc-20261101-1" and r["result"]["warnings"] == []
    again = t.create_incident(incident_id="inc-20261101-1", alert=GRAPH_ALERT, severity="SEV2")
    assert again["ok"] and again["result"]["warnings"] == ["incident_already_exists"]      # retry: no duplicate
    assert t.append_timeline("inc-20261101-1", text="alert received", at="2026-11-01T10:00:05+00:00")["ok"]
    got = t.get_incident("inc-20261101-1")["result"]
    assert got["alert_name"] == "CheckoutErrorRateHigh" and got["service"] == "checkout"   # the real name, never a placeholder
    assert got["severity"] == "SEV2"
    assert got["timeline"][0]["at"] == 1793527205.0 and got["timeline"][0]["event"] == "alert received"
    pm = {"timeline": ["a"], "root_cause": "paymentFailure flag"}
    assert t.save_postmortem("inc-20261101-1", postmortem=pm)["ok"]
    assert json.loads(t.get_incident("inc-20261101-1")["result"]["postmortem"])["root_cause"] == "paymentFailure flag"
    assert t.draft_status("inc-20261101-1")["ok"]


def test_an_alert_without_a_name_is_refused_not_renamed():
    t = make()
    for bad in (None, {}, {"service": "checkout"}, {"alert_name": "  "}):
        assert code(t.create_incident(incident_id="inc-x", alert=bad)) == "missing_alert_name"
    assert t.get_incident("inc-x")["error"]["code"] == "not_found"      # nothing was stored


def test_an_unrecognised_service_opens_the_incident_with_a_warning():
    t = make()
    r = t.create_incident(incident_id="inc-y", alert=dict(GRAPH_ALERT, service="not-a-service"))
    assert r["ok"] and r["result"]["warnings"] == ["service_not_recognised:not-a-service"]
    assert t.get_incident("inc-y")["result"]["service"] is None
    assert code(t.create_incident(incident_id="inc-z", alert=GRAPH_ALERT, service="nope")) == "unknown_service"


def test_prometheus_style_alert_names_still_work():
    t = make()
    assert t.create_incident(incident_id="inc-p", alert={"labels": {"alertname": "HighLatency", "service_name": "cart"}})["ok"]
    got = t.get_incident("inc-p")["result"]
    assert got["alert_name"] == "HighLatency" and got["service"] == "cart"


def test_an_unreadable_timestamp_is_refused_and_a_missing_one_means_now():
    t = make()
    t.create_incident(incident_id="inc-t", alert=GRAPH_ALERT)
    assert code(t.append_timeline("inc-t", text="x", at="yesterday-ish")) == "bad_argument"
    assert t.append_timeline("inc-t", text="no time given")["ok"]
    assert t.get_incident("inc-t")["result"]["timeline"][0]["at"] == T0
    assert code(t.create_incident(incident_id="inc-s", alert=GRAPH_ALERT, severity="SEV9")) == "bad_argument"
