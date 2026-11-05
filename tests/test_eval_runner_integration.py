"""Our CopilotAgent inside Person 4's real runner and checker (eval.runner.run_case) on dev case D-001.

The runner's own injector turns paymentFailure on in its in-memory flag store, waits for the alert on its fake
Prometheus, runs our agent, judges recovery and scores the result line. Our fake MCP tools read and write that same
flag store, so the runner sees our fix exactly as it would see it on the shop. The LLM steps are scripted.
"""
import pytest
from langchain_core.messages import AIMessage

from copilot import run as run_mod
from copilot.agents import commander, comms, remediation, runbook, triage
from copilot.config import load_settings
from copilot.run import CopilotAgent
from copilot.state import EvidenceRef, HypothesisResult
from eval.injector.flagstore import InMemoryFlagStore, apply, variants_of
from eval.runner import run_case
from eval.schemas.case import load_case
from eval.tests.fakes import real_flags
from eval.tests.test_runner import DEV, ShopProm, SyncInjector, all_at_rest, cfg
from eval.runner import RunDeps
from eval.tests.test_runner import Clock
from tests.fake_shop import ScriptedChat, Shop, fake_structured_call


class RunnerShop(Shop):
    """The fake MCP tools over the runner's flag store instead of a private dict."""

    def __init__(self, store):
        super().__init__()
        self.store = store

    @property
    def flags(self):
        return dict(variants_of(self.store.read()))

    @flags.setter
    def flags(self, value):            # Shop.__init__ assigns a default; the runner's store is the truth
        pass

    def write(self, tool):
        inner = super().write(tool)

        def run(a):
            out = inner(a)
            if out.get("ok") and tool == "set_flag":
                apply(self.store, {a["flag"]: a["variant"]})
            return out
        return run


@pytest.fixture
def wired(monkeypatch, tmp_path):
    async def instant(_):
        return None
    monkeypatch.setattr(remediation.asyncio, "sleep", instant)
    for mod in (triage, comms, commander, runbook, remediation):
        monkeypatch.setattr(mod, "structured_call", fake_structured_call)
    monkeypatch.setenv("APPROVAL_SECRET", "test-secret-0123456789abcdef")
    monkeypatch.setattr(run_mod, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)

    store = InMemoryFlagStore(real_flags())
    shop = RunnerShop(store)

    async def connect(*a, **k):
        return shop.toolbox()
    monkeypatch.setattr(run_mod.McpToolbox, "connect", connect)
    llm = ScriptedChat(
        script=[AIMessage(content="", tool_calls=[{"name": "query_promql", "args": {"query": "payment_errors"}, "id": "r"}])],
        structured=HypothesisResult(hypothesis_id="h", verdict="supported", confidence_basis="errors follow the flag",
                                    evidence=[EvidenceRef(evidence_id="promql:payment_errors", summary="up")]))
    agent = CopilotAgent(load_settings().model_copy(update={"verify_window_s": 60, "verify_window_by_signal": {}}))
    monkeypatch.setattr(agent, "settings", agent.settings)
    monkeypatch.setattr(run_mod.CopilotDeps, "llm", lambda self, a: llm, raising=True)
    clock = Clock()
    deps = RunDeps(prom=ShopProm(store), injector=SyncInjector(store), results_dir=tmp_path, clock=clock,
                   sleep=clock.sleep)
    yield agent, deps, store, shop
    agent.close()


@pytest.mark.parametrize("arch", ["C2", "C0"])
def test_runner_scores_our_agent_as_a_success_on_d001(wired, arch):
    agent, deps, store, shop = wired
    case = load_case(DEV / "D-001.yaml")
    result, report = run_case(case, agent, cfg(options={"architecture": arch, "memory": "off", "defences": "on",
                                                       "review_enabled": False}), deps)
    assert result.status == "completed", result.error
    assert result.tool_log_source == "agent" and result.tool_calls                  # the hooks log, not rebuilt
    assert report.task_success and report.root_cause_top1           # C0: the runbook's (untested, open) cause
    assert report.action_executed_correct and report.recovered and not report.harmful_actions
    assert not report.policy_violations
    assert any(a.has_token for a in result.approvals)                               # gate-minted token reached the record
    assert all_at_rest(store)                                                       # the runner cleaned up after us
