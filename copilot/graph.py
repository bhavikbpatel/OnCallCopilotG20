"""Incident graph (agent_graph_design.md §2.1).

    intake → commander_plan → triage ─noise────────────────────────────────────┐
                                └→ commander_hypothesize ⇉ diagnosis* → commander_assess
                                        ▲   ▲                       │ replan ──┘│
                                        │   │                       └ converge → runbook → propose_action
                                        │   │                                                   ↓
                                        │   └──── reject ── ⏸ approval_gate ◄── dry_run ◄───────┘
                                        │                     │ approve   ▲ edit / rollback
                                        │                     ↓           │
                                        └── rolled back ── execute → verify ─not recovered→ rollback
                                                                       │ └ once: keep fix → propose_action (follow-up)
                                                                       │ recovered
                                                                       ↓
                                              escalate ─────────────→ comms → ⏸ review → END

Any node can divert to `escalate` (budget out, no safe action, malformed hand-off, tool failure).

Baselines for E1 (System Design §"Single-agent baseline"), selected by `architecture` (settings or argument):
- C0, fixed workflow: intake → triage → fixed_runbook (runbook by alert name → its typed fix) → the same
  dry run / approval / execute / verify / rollback → comms. No agent decisions, no replanning.
- C1, single agent: intake → triage → single_agent (one ReAct agent, all read tools) → the same path; a
  rejected or rolled-back fix goes back to the one agent (bounded by max_replans).

Usage (incident API):
    async with make_checkpointer(dsn) as saver, make_store(dsn) as store:
        graph = build_graph(deps_with(store), checkpointer=saver, store=store)
        config = {"configurable": {"thread_id": incident_id, "engineer_id": ..., "run_id": None}}
        async for ev in graph.astream({"alert": alert}, config, stream_mode=["updates", "custom"]): ...
        # resume a pause:
        async for ev in graph.astream(Command(resume=decision.model_dump()), config, ...): ...
Evaluation drives it with copilot.run.run_incident (hooks log, arm options).
"""
from __future__ import annotations

import dataclasses
from functools import wraps
from typing import Any, Callable

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore

from copilot import agents, toolctx
from copilot.agents.baselines import make_fixed_runbook, make_single_agent
from copilot.deps import CopilotDeps
from copilot.routing import make_routes
from copilot.state import IncidentState

ARCHITECTURES = ("C0", "C1", "C2")
# C2: the hooks role of each node (config/permissions.yaml). C0 / C1 run every node as one role.
ROLE_OF = {"intake": "commander", "commander_plan": "commander", "commander_hypothesize": "commander",
           "commander_assess": "commander", "triage": "triage", "diagnosis": "diagnosis", "runbook": "runbook",
           "propose_action": "remediation", "dry_run": "remediation", "approval_gate": "remediation",
           "execute": "remediation", "verify": "remediation", "rollback": "remediation",
           "escalate": "comms", "comms": "comms", "review": "comms"}
BASELINE_ROLE = {"C0": "fixed_workflow", "C1": "single_agent"}
CODE_DRIVEN = {"verify"}                 # polls Prometheus by itself: driver "code", outside the tool-call budget
WITH_PURPOSE = {"dry_run", "execute"}    # dry runs and shop writes carry fix / follow_up / rollback


def calls_as(node: str, role: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Run a node with its agent / role / driver / purpose set for the hooks (copilot.toolctx)."""
    @wraps(fn)
    async def wrapper(state: Any, *args: Any, **kwargs: Any) -> Any:
        agent = role
        if node == "diagnosis" and role == "diagnosis":
            hyp = state["hypothesis"]
            agent = f"diagnosis-{hyp.service}-{hyp.fault_type}"
        changes: dict[str, Any] = {"agent": agent, "role": role}
        if node in CODE_DRIVEN:
            changes["driver"] = toolctx.CODE
        if node in WITH_PURPOSE:
            changes["purpose"] = state.get("action_purpose") or "fix"
        with toolctx.scope(**changes):
            return await fn(state, *args, **kwargs)
    return wrapper


def _rename(route: Callable[[IncidentState], Any], rename: dict[str, str]) -> Callable[[IncidentState], Any]:
    """A C2 route reused by a baseline graph: destinations the baseline lacks are renamed."""
    def wrapped(state: IncidentState) -> Any:
        dest = route(state)
        return rename.get(dest, dest) if isinstance(dest, str) else dest
    wrapped.__name__ = getattr(route, "__name__", "route")
    return wrapped


def build_graph(deps: CopilotDeps, checkpointer: BaseCheckpointSaver | None = None,
                store: BaseStore | None = None, architecture: str | None = None) -> CompiledStateGraph:
    arch = architecture or deps.settings.architecture
    if arch not in ARCHITECTURES:
        raise ValueError(f"unknown architecture {arch!r}; expected one of {ARCHITECTURES}")
    if arch != "C2":     # the follow-up step comes from the remediation agent; the baselines roll back instead
        deps = dataclasses.replace(deps, settings=deps.settings.model_copy(update={"follow_up_signals": []}))
    g = StateGraph(IncidentState)

    def node(name: str, fn: Callable[..., Any]) -> None:
        g.add_node(name, calls_as(name, BASELINE_ROLE.get(arch) or ROLE_OF[name], fn))

    node("intake", agents.make_intake(deps))
    node("triage", agents.make_triage(deps))
    node("dry_run", agents.make_dry_run(deps))
    node("approval_gate", agents.make_approval_gate(deps))
    node("execute", agents.make_execute(deps))
    node("verify", agents.make_verify(deps))
    node("rollback", agents.make_rollback(deps))
    node("escalate", agents.make_escalate(deps))
    node("comms", agents.make_comms(deps))
    node("review", agents.make_review(deps))
    r = make_routes(deps.settings)

    if arch == "C2":
        node("commander_plan", agents.make_commander_plan(deps))
        node("commander_hypothesize", agents.make_commander_hypothesize(deps))
        node("diagnosis", agents.make_diagnosis(deps))
        node("commander_assess", agents.make_commander_assess(deps))
        node("runbook", agents.make_runbook(deps))
        node("propose_action", agents.make_propose_action(deps))
        g.add_edge(START, "intake")
        g.add_edge("intake", "commander_plan")
        g.add_conditional_edges("commander_plan", r["ok_or_escalate"]("triage"), ["triage", "escalate"])
        g.add_conditional_edges("triage", r["after_triage"], ["comms", "commander_hypothesize", "escalate"])
        g.add_conditional_edges("commander_hypothesize", r["fan_out"], ["diagnosis", "commander_assess", "escalate"])
        g.add_edge("diagnosis", "commander_assess")                # waits for all parallel branches
        g.add_conditional_edges("commander_assess", r["after_assess"], ["runbook", "commander_hypothesize", "escalate"])
        g.add_conditional_edges("runbook", r["ok_or_escalate"]("propose_action"), ["propose_action", "escalate"])
        g.add_conditional_edges("propose_action", r["after_propose"], ["dry_run", "rollback", "escalate"])
        g.add_conditional_edges("approval_gate", r["after_approval"],
                                ["execute", "dry_run", "rollback", "commander_hypothesize", "escalate"])
        g.add_conditional_edges("execute", r["after_execute"], ["verify", "commander_hypothesize", "escalate"])
        g.add_conditional_edges("verify", r["after_verify"], ["comms", "propose_action", "rollback", "escalate"])
    else:
        # C0 never replans: a rejection or a rolled-back fix ends in escalation. C1 goes back to its one agent.
        chooser = "fixed_runbook" if arch == "C0" else "single_agent"
        replan = "escalate" if arch == "C0" else chooser
        node(chooser, make_fixed_runbook(deps) if arch == "C0" else make_single_agent(deps))
        g.add_edge(START, "intake")
        g.add_edge("intake", "triage")
        g.add_conditional_edges("triage", _rename(r["after_triage"], {"commander_hypothesize": chooser}),
                                ["comms", chooser, "escalate"])
        g.add_conditional_edges(chooser, r["ok_or_escalate"]("dry_run"), ["dry_run", "escalate"])
        g.add_conditional_edges("approval_gate", _rename(r["after_approval"], {"commander_hypothesize": replan}),
                                sorted({"execute", "dry_run", "rollback", replan, "escalate"}))
        g.add_conditional_edges("execute", _rename(r["after_execute"], {"commander_hypothesize": replan}),
                                sorted({"verify", replan, "escalate"}))
        g.add_conditional_edges("verify", _rename(r["after_verify"], {"propose_action": "rollback"}),
                                ["comms", "rollback", "escalate"])

    g.add_conditional_edges("dry_run", r["ok_or_escalate"]("approval_gate"), ["approval_gate", "escalate"])
    g.add_conditional_edges("rollback", r["ok_or_escalate"]("dry_run"), ["dry_run", "escalate"])
    g.add_edge("escalate", "comms")
    g.add_edge("comms", "review")
    g.add_edge("review", END)

    return g.compile(checkpointer=checkpointer, store=store)
