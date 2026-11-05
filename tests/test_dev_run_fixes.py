"""Fixes from the first scored dev batch (DEV-C2, 4 Nov): which supported cause wins, closing as noise, what remediation
sees, and postmortem lessons gated like any other lesson."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from copilot.agents import commander, comms, remediation, runbook, triage
from copilot.agents.commander import choose, decide
from copilot.agents.runbook import gate_postmortems, postmortem_applies
from copilot.clients import Passage
from copilot.config import load_settings
from copilot.memory.views import remediation_view
from copilot.routing import make_routes
from copilot.state import BudgetLimits, EvidenceItem, EvidenceRef, Hypothesis, HypothesisResult
from tests.fake_shop import ScriptedChat, Shop, alert, fake_structured_call

EV = [EvidenceRef(evidence_id="promql:x", summary="up")]


def hyp(service, fault, n_evidence=1, status="supported", hid=None):
    return Hypothesis(id=hid or f"h-{service}-{fault}", service=service, fault_type=fault, status=status,
                      evidence=EV * n_evidence)


# ---------- which supported hypothesis wins (D-001, D-010, D-012) ----------

def test_the_service_that_owns_the_fault_beats_a_caller_showing_symptoms():
    caller, owner = hyp("checkout", "paymentFailure"), hyp("payment", "paymentFailure")
    assert choose([caller, owner]) is owner                              # D-001 named checkout before
    assert choose([hyp("frontend-proxy", "imageSlowLoad"), hyp("image-provider", "imageSlowLoad")]).service == "image-provider"


def test_a_real_fault_beats_noise_and_more_evidence_breaks_ties():
    assert str(choose([hyp("frontend", "noise", 3), hyp("product-catalog", "productCatalogFailure")]).fault_type) \
        == "productCatalogFailure"                                       # D-013 / D-014 / D-019
    a, b = hyp("payment", "paymentFailure", 1, hid="a"), hyp("payment", "paymentFailure", 2, hid="b")
    assert choose([a, b]) is b and choose([a, hyp("payment", "paymentFailure", 1, hid="c")]) is a   # ties keep order


def test_only_noise_supported_closes_as_noise():
    st = {"hypotheses": [hyp("frontend", "noise")], "limits": BudgetLimits()}
    assert decide(st, 2)[0] == "noise"                                   # D-015
    assert make_routes(load_settings())["after_assess"]({"final_status": "noise"}) == "comms"


def test_assess_keeps_one_root_cause_supported_and_ranks_the_rest_open(monkeypatch):
    monkeypatch.setattr(commander, "structured_call", fake_structured_call)
    deps = SimpleNamespace(settings=load_settings())
    st = {"hypotheses": [hyp("checkout", "paymentFailure"), hyp("payment", "paymentFailure")], "limits": BudgetLimits(),
          "alert": None}
    out = asyncio.run(commander.make_commander_assess(deps)(st))
    assert [(str(h.service), h.status) for h in out["hypotheses"]] == [("checkout", "open")]
    assert "ranked below payment/paymentFailure" in out["hypotheses"][0].rationale
    assert "converge: payment/paymentFailure" in out["timeline"][0].text


# ---------- remediation sees the cause and its evidence (D-005, D-013, D-014) ----------

def test_remediation_view_carries_alert_hypothesis_and_evidence():
    h = hyp("product-catalog", "productCatalogFailure")
    st = {"alert": SimpleNamespace(model_dump=lambda mode: {"alert_name": "A"}), "hypotheses": [h, hyp("cart", "cartFailure", status="refuted")],
          "evidence": [EvidenceItem(evidence_id="promql:pc", hypothesis_id=h.id, summary="errors 30%",
                                    at=datetime.now(timezone.utc)),
                       EvidenceItem(evidence_id="promql:cart", hypothesis_id="h-cart-cartFailure", summary="flat",
                                    at=datetime.now(timezone.utc))],
          "evidence_summary": "catalog errors", "runbook_advice": None}
    view = remediation_view(st)
    assert view["alert"] == {"alert_name": "A"} and [x["service"] for x in view["supported_hypotheses"]] == ["product-catalog"]
    assert view["evidence"] == [{"evidence_id": "promql:pc", "summary": "errors 30%"}]      # only the supported cause's


# ---------- postmortems are lessons too (D-011, E4) ----------

PM_FLAG = Passage(passage_id="PM-001#fix", doc_id="PM-001", text="t", score=1, kind="postmortem",
                  fix={"tool": "set_flag", "args": {"flag": "paymentFailure", "variant": "off"}})
PM_RESTART = Passage(passage_id="PM-007#fix", doc_id="PM-007", text="t", score=1, kind="postmortem",
                     fix={"tool": "restart_service", "args": {"service": "email"}})
RB = Passage(passage_id="RB-009#action", doc_id="RB-009", text="t", score=1, kind="runbook", fix=None)


def test_a_postmortem_applies_only_when_its_target_is_in_play():
    assert postmortem_applies(PM_FLAG, {"paymentFailure"}, set())
    assert not postmortem_applies(PM_FLAG, {"paymentUnreachable"}, {"checkout"})      # D-011: the misleading one
    assert postmortem_applies(PM_RESTART, set(), {"email"}) and not postmortem_applies(PM_RESTART, set(), {"cart"})


def gate(store, flags, services=()):
    async def changes(since):
        return [{"flag": f, "variant": "on"} for f in flags]
    deps = SimpleNamespace(store=store, telemetry=SimpleNamespace(recent_changes=changes))
    passages = {p.passage_id: p for p in (RB, PM_FLAG, PM_RESTART)}
    return asyncio.run(gate_postmortems(passages, deps, list(services)))


def test_misleading_postmortem_is_dropped_and_reported():
    kept, rejected = gate(object(), ["paymentUnreachable"], ["checkout"])
    assert list(kept) == ["RB-009#action"] and rejected == ["PM-001", "PM-007"]
    kept, rejected = gate(object(), ["paymentFailure"], ["payment"])
    assert set(kept) == {"RB-009#action", "PM-001#fix"} and rejected == ["PM-007"]


def test_memory_off_sees_no_postmortems():
    kept, rejected = gate(None, ["paymentFailure"], ["payment"])         # E4 "without postmortem memory"
    assert list(kept) == ["RB-009#action"] and rejected == []


# ---------- end to end: a noise alert closes as noise with no action (D-015) ----------

def test_noise_alert_closes_without_any_action(monkeypatch):
    async def instant(_):
        return None

    async def noise_calls(deps, agent, prompt, schema, payload):
        if schema.__name__ == "HypothesesDraft":
            return schema(hypotheses=[{"service": "frontend", "fault_type": "noise", "rationale": "traffic only"}],
                          assumptions=[]), None
        return await fake_structured_call(deps, agent, prompt, schema, payload)
    monkeypatch.setattr(remediation.asyncio, "sleep", instant)
    for mod in (triage, comms, commander, runbook, remediation):
        monkeypatch.setattr(mod, "structured_call", noise_calls)
    monkeypatch.setenv("APPROVAL_SECRET", "test-secret-0123456789abcdef")
    from copilot import run as run_mod
    monkeypatch.setattr(run_mod, "load_dotenv", lambda *a, **k: None)      # never the developer's .env
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    llm = ScriptedChat(
        script=[AIMessage(content="", tool_calls=[{"name": "query_promql", "args": {"query": "frontend_errors"}, "id": "n"}])],
        structured=HypothesisResult(hypothesis_id="h", verdict="supported", confidence_basis="errors flat, traffic up",
                                    evidence=[EvidenceRef(evidence_id="promql:frontend_errors", summary="flat")]))
    from copilot.run import run_incident
    from eval.runner.approvals import AutoApprover
    shop = Shop()
    res = asyncio.run(run_incident(alert(alert_name="FrontendTrafficHigh", service="frontend"), AutoApprover("approve"),
                                   run_id="D-015-r1", limits=BudgetLimits(tool_calls=20), settings=load_settings(),
                                   options={"architecture": "C2", "memory": "off", "review_enabled": False},
                                   toolbox=shop.toolbox(), llm_factory=lambda a: llm))
    st = res.final_state
    assert st["final_status"] == "noise" and shop.writes == [] and st.get("proposed_action") is None
    assert "judged noise" in st["status_note"].text
