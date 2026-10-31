"""Knowledge retrieval, corpus, severity and reviewed-memory contracts."""
from __future__ import annotations

import json
import pytest

from eval.knowledge.evaluate import ROOT, evaluate
from eval.knowledge.memory import MemoryStore
from eval.knowledge.retrieval import KnowledgeIndex, load_passages
from eval.knowledge.severity import classify


def test_corpus_has_required_runbooks_and_seed_postmortems():
    passages = load_passages(ROOT)
    docs = {passage.doc_id for passage in passages}
    assert len([item for item in docs if item.startswith("RB-")]) == 20
    assert len([item for item in docs if item.startswith("PM-")]) == 10
    assert all(passage.fix is None or passage.fix["tool"] == "set_flag" for passage in passages)
    assert all(passage.fix is None for passage in passages if passage.doc_id == "RB-014")


def test_retrieval_honors_service_filter_and_rejects_unknown_service():
    index = KnowledgeIndex(load_passages(ROOT))
    results = index.search("checkout payment failures and recent flag change",
                           service="payment", k=5)
    assert results and all("payment" in passage.services for passage in results)
    with pytest.raises(ValueError, match="unknown service"):
        index.search("payment", service="all")


def test_benchmark_is_development_only_and_compares_baseline_to_change():
    result = evaluate()
    assert result["queries"] >= 30
    assert result["baseline_bm25"]["total"] == result["change_hybrid_reranker"]["total"]
    assert 0 <= result["baseline_bm25"]["recall_at_k"] <= 1
    assert 0 <= result["change_hybrid_reranker"]["recall_at_k"] <= 1


def test_seed_lessons_are_pending_until_review_and_preconditions_must_match():
    store = MemoryStore()
    try:
        assert store.load_proposals(ROOT / "lessons.yaml") == 3
        assert store.search("payment", "feature_flag", {
            "recent_changes": {"paymentFailure": 2},
            "signals": {"payment.error_rate": 0.1},
        }) == []
        store.review("L-007", "on-call reviewer", "approve")
        context = {
            "recent_changes": {"paymentFailure": 2},
            "signals": {"payment.error_rate": 0.1},
        }
        decision = store.search("payment", "feature_flag", context)
        assert len(decision) == 1 and decision[0].applicable
        misleading = store.search("payment", "feature_flag", {
            "recent_changes": {"paymentFailure": 60},
            "signals": {"payment.error_rate": 0.12},
        })
        assert len(misleading) == 1 and not misleading[0].applicable
        assert "recent change age" in misleading[0].reason
        assert store.search("payment", "feature_flag", context, enabled=False) == []
    finally:
        store.close()


def test_missing_or_unsupported_preconditions_fail_closed():
    store = MemoryStore()
    try:
        store.propose("L-x", "test", {"service": "payment", "fault_family": "feature_flag"},
                      [{"check": "signal", "name": "payment.error_rate", "operator": "gt", "threshold": 0.01}],
                      "D-001")
        store.review("L-x", "reviewer", "approve")
        result = store.search("payment", "feature_flag", {"signals": {}})
        assert len(result) == 1 and not result[0].applicable
        assert "unavailable" in result[0].reason
    finally:
        store.close()


def test_rejected_and_withdrawn_lessons_are_not_returned():
    store = MemoryStore()
    try:
        args = ("L-x", "test", {"service": "payment", "fault_family": "feature_flag"},
                [{"check": "service_is", "value": "payment"}], "D-001")
        store.propose(*args)
        store.review("L-x", "reviewer", "reject")
        assert store.search("payment", "feature_flag", {"service": "payment"}) == []
        store.propose("L-y", "test", {"service": "payment", "fault_family": "feature_flag"},
                      [{"check": "service_is", "value": "payment"}], "D-001")
        store.review("L-y", "reviewer", "approve")
        store.withdraw("L-y", "reviewer")
        assert store.search("payment", "feature_flag", {"service": "payment"}) == []
    finally:
        store.close()


def test_edit_review_requires_updated_text_and_preconditions():
    store = MemoryStore()
    try:
        store.propose("L-x", "old", {"service": "payment", "fault_family": "feature_flag"},
                      [{"check": "service_is", "value": "payment"}], "D-001")
        with pytest.raises(ValueError, match="revised text, scope, and preconditions"):
            store.review("L-x", "reviewer", "edit")
        store.review("L-x", "reviewer", "edit", text="revised",
                     applies_to={"service": "payment", "fault_family": "feature_flag"},
                     preconditions=[{"check": "service_is", "value": "payment"}])
        assert store.search("payment", "feature_flag", {"service": "payment"})[0].lesson.text == "revised"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("inputs", "level", "rule"),
    [
        ({"user_impact": "broad", "error_rate": 0.08, "duration_minutes": 8,
          "evidence_count": 2, "critical_path": True}, "SEV1", "S1.1"),
        ({"user_impact": "partial", "error_rate": 0.02, "duration_minutes": 6,
          "evidence_count": 2, "critical_path": False}, "SEV2", "S2.1"),
        ({"user_impact": "localized", "error_rate": 0.005, "duration_minutes": 2,
          "evidence_count": 2, "critical_path": False}, "SEV3", "S3.1"),
        ({"user_impact": "none", "error_rate": 0, "duration_minutes": 10,
          "evidence_count": 2, "critical_path": False}, "NONE", "S0.1"),
        ({"user_impact": "none", "error_rate": 0, "duration_minutes": 10,
          "evidence_count": 1, "critical_path": False}, "SEV3", "S3.0"),
        ({"user_impact": "broad", "error_rate": 0.2, "duration_minutes": 20,
          "evidence_count": 1, "critical_path": True}, "SEV3", "S3.0"),
        ({"user_impact": "unknown", "error_rate": 0.2, "duration_minutes": 20,
          "evidence_count": 3, "critical_path": True}, "SEV3", "S3.0"),
        ({"user_impact": "broad", "error_rate": None, "duration_minutes": 20,
          "evidence_count": 3, "critical_path": True}, "SEV3", "S3.0"),
        ({"user_impact": "partial", "error_rate": 0.005, "duration_minutes": 10,
          "evidence_count": 3, "critical_path": False, "latency_p95_ms": 900}, "SEV3", "S3.1"),
        ({"user_impact": "partial", "error_rate": 0.005, "duration_minutes": 10,
          "evidence_count": 3, "critical_path": True, "latency_p95_ms": 900}, "SEV2", "S2.1"),
        ({"user_impact": "partial", "error_rate": None, "duration_minutes": 10,
          "evidence_count": 3, "critical_path": True, "latency_p95_ms": 900}, "SEV2", "S2.1"),
    ],
)
def test_severity_policy(inputs, level, rule):
    result = classify(**inputs)
    assert (result.level, result.rule) == (level, rule)


def test_query_labels_do_not_reference_heldout_data():
    queries = json.loads((ROOT / "retrieval_queries.json").read_text(encoding="utf-8"))
    assert len(queries) >= 30
    assert all(query["id"].startswith("Q-") and all(not ref.startswith("H-")
               for ref in query["relevant"]) for query in queries)


def test_alert_query_is_decomposed_and_returns_diagnostic_passages():
    index = KnowledgeIndex(load_passages(ROOT))
    decomposed = index.decompose_query(
        "CheckoutPaymentErrors alert: checkout attempts fail after rollout",
        service="payment",
    )
    assert decomposed and any("error rate failed requests" in item for item in decomposed)
    results = index.search(
        "CheckoutPaymentErrors alert: checkout attempts fail after rollout",
        service="payment", k=5,
    )
    assert results and any(item.doc_id == "RB-001" for item in results)


def test_incident_memory_requires_accepted_postmortem_and_can_be_deleted():
    store = MemoryStore()
    try:
        with pytest.raises(ValueError, match="accepting reviewer"):
            store.save_incident("INC-1", "summary", {}, {}, "recovered", [], "")
        with pytest.raises(ValueError, match="allow-listed action"):
            store.save_incident("INC-1", "summary", {}, {"tool": "exec"}, "recovered", [], "reviewer")
        store.save_incident(
            "INC-1", "redacted summary", {"service": "payment"},
            {"tool": "set_flag", "args": {"flag": "paymentFailure", "value": "off"}},
            "recovered", [{"time": "T+1", "event": "verified"}], "on-call reviewer",
        )
        incident = store.get_incident("INC-1")
        assert incident is not None and incident.accepted_by == "on-call reviewer"
        assert incident.root_cause == {"service": "payment"}
        store.delete_incident("INC-1", "on-call reviewer")
        assert store.get_incident("INC-1") is None
    finally:
        store.close()


def test_memory_rejects_unverified_lesson_signals():
    store = MemoryStore()
    try:
        with pytest.raises(ValueError, match="unsupported signal"):
            store.propose(
                "L-unknown", "unsupported",
                {"service": "product-catalog", "fault_family": "test"},
                [{"check": "signal", "name": "astronomy-db.wait_ms",
                  "operator": "gt", "threshold": 100}], "D-012",
            )
    finally:
        store.close()


def test_lesson_can_be_deleted_by_named_reviewer():
    store = MemoryStore()
    try:
        store.propose(
            "L-delete", "test", {"service": "payment", "fault_family": "feature_flag"},
            [{"check": "service_is", "value": "payment"}], "D-001",
        )
        store.delete_lesson("L-delete", "reviewer")
        with pytest.raises(ValueError, match="not awaiting review"):
            store.review("L-delete", "reviewer", "approve")
    finally:
        store.close()


def test_signal_registry_uses_discovered_span_metric_names():
    from eval.telemetry.signals import lesson_signal_name, signal_query
    from eval.probes.discover import CANDIDATES

    assert lesson_signal_name("payment", "error_rate") == "payment.error_rate"
    assert signal_query("payment", "error_rate") == (
        CANDIDATES["error_rate"]["server_spans"].replace("{svc}", "payment")
    )
    assert signal_query("product-catalog", "latency_p95_ms") == (
        CANDIDATES["latency_p95_ms"]["server_spans"].replace("{svc}", "product-catalog")
    )
    with pytest.raises(ValueError, match="unverified"):
        signal_query("astronomy-db", "wait_ms")


@pytest.mark.parametrize(
    "fix",
    [
        {"tool": "exec", "args": {"command": "restart everything"}},
        {"tool": "set_flag", "args": {"flag": "paymentFailure", "value": "on"}},
        {"tool": "scale", "args": {"service": "payment", "replicas": 100}},
    ],
)
def test_ingestion_rejects_non_allowlisted_fixes(tmp_path, fix):
    source = f"""---
doc_id: RB-test
kind: runbook
service: [payment]
symptom: [test symptom]
fault_family: test
fix: {json.dumps(fix)}
author: test
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Checks
Evidence.
"""
    (tmp_path / "unsafe.md").write_text(source, encoding="utf-8")
    with pytest.raises(ValueError, match="typed action"):
        load_passages(tmp_path)
