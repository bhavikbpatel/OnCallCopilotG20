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
                                                                       │ recovered
                                                                       ↓
                                              escalate ─────────────→ comms → ⏸ review → END

Any node can divert to `escalate` (budget out, no safe action, malformed hand-off, tool failure).

Usage (incident API):
    async with make_checkpointer(dsn) as saver, make_store(dsn) as store:
        graph = build_graph(deps_with(store), checkpointer=saver, store=store)
        config = {"configurable": {"thread_id": incident_id, "engineer_id": ..., "run_id": None}}
        async for ev in graph.astream({"alert": alert}, config, stream_mode=["updates", "custom"]): ...
        # resume a pause:
        async for ev in graph.astream(Command(resume=decision.model_dump()), config, ...): ...
"""
from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore

from copilot import agents
from copilot.deps import CopilotDeps
from copilot.routing import make_routes
from copilot.state import IncidentState


def build_graph(deps: CopilotDeps, checkpointer: BaseCheckpointSaver | None = None,
                store: BaseStore | None = None) -> CompiledStateGraph:
    g = StateGraph(IncidentState)

    g.add_node("intake", agents.make_intake(deps))
    g.add_node("commander_plan", agents.make_commander_plan(deps))
    g.add_node("triage", agents.make_triage(deps))
    g.add_node("commander_hypothesize", agents.make_commander_hypothesize(deps))
    g.add_node("diagnosis", agents.make_diagnosis(deps))
    g.add_node("commander_assess", agents.make_commander_assess(deps))
    g.add_node("runbook", agents.make_runbook(deps))
    g.add_node("propose_action", agents.make_propose_action(deps))
    g.add_node("dry_run", agents.make_dry_run(deps))
    g.add_node("approval_gate", agents.make_approval_gate(deps))
    g.add_node("execute", agents.make_execute(deps))
    g.add_node("verify", agents.make_verify(deps))
    g.add_node("rollback", agents.make_rollback(deps))
    g.add_node("escalate", agents.make_escalate(deps))
    g.add_node("comms", agents.make_comms(deps))
    g.add_node("review", agents.make_review(deps))

    r = make_routes(deps.settings)
    g.add_edge(START, "intake")
    g.add_edge("intake", "commander_plan")
    g.add_conditional_edges("commander_plan", r["ok_or_escalate"]("triage"), ["triage", "escalate"])
    g.add_conditional_edges("triage", r["after_triage"], ["comms", "commander_hypothesize", "escalate"])
    g.add_conditional_edges("commander_hypothesize", r["fan_out"], ["diagnosis", "commander_assess", "escalate"])
    g.add_edge("diagnosis", "commander_assess")                # waits for all parallel branches
    g.add_conditional_edges("commander_assess", r["after_assess"], ["runbook", "commander_hypothesize", "escalate"])
    g.add_conditional_edges("runbook", r["ok_or_escalate"]("propose_action"), ["propose_action", "escalate"])
    g.add_conditional_edges("propose_action", r["ok_or_escalate"]("dry_run"), ["dry_run", "escalate"])
    g.add_conditional_edges("dry_run", r["ok_or_escalate"]("approval_gate"), ["approval_gate", "escalate"])
    g.add_conditional_edges("approval_gate", r["after_approval"],
                            ["execute", "dry_run", "commander_hypothesize", "escalate"])
    g.add_conditional_edges("execute", r["after_execute"], ["verify", "commander_hypothesize", "escalate"])
    g.add_conditional_edges("verify", r["after_verify"], ["comms", "rollback", "escalate"])
    g.add_conditional_edges("rollback", r["ok_or_escalate"]("dry_run"), ["dry_run", "escalate"])
    g.add_edge("escalate", "comms")
    g.add_edge("comms", "review")
    g.add_edge("review", END)

    return g.compile(checkpointer=checkpointer, store=store)
