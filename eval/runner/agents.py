"""What the runner needs from an agent, and the agents that ship with the harness.

An agent gets only what a real on-call system would give it: the alert, a way to
ask for approval, a deadline and a tool-call budget. It never sees the case, the
injected flags or the expected answer (capstone guide pp. 5-6; Sprint Backlog,
evaluation integrity).

Person 2's graph will plug in as another ``Agent`` whose ``run`` streams the graph,
answers each approval interrupt with ``ctx.approve(action)`` and returns the final
``IncidentState`` (turned into a result line by eval/adapters/copilot_state.py).
"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable, Optional, Protocol


@dataclasses.dataclass(frozen=True)
class AgentContext:
    run_id: str
    alert: dict                              # Person 2's Alert fields
    approve: Callable[[dict], dict]          # flat action -> ApprovalDecision (decision, token, ...)
    deadline: float                          # unix time; the runner stops waiting after this
    max_tool_calls: int
    clock: Callable[[], float]


@dataclasses.dataclass
class AgentResult:
    state: Any = dataclasses.field(default_factory=dict)   # final IncidentState (pydantic or dict)
    tool_calls: Optional[list] = None        # full tool log if the agent has one (else rebuilt from state)
    fixed_at: Optional[float] = None         # unix time the fix was executed; recovery is judged from here
    budget_exhausted: bool = False


class Agent(Protocol):
    name: str
    version: str

    def run(self, ctx: AgentContext) -> AgentResult: ...


class DoNothingAgent:
    """Never investigates and never acts. Floor baseline and a check of the runner itself:
    the timeout, recovery, scoring and clean-up paths all run without any agent behaviour."""

    name = "do-nothing"
    version = "1"

    def run(self, ctx: AgentContext) -> AgentResult:
        return AgentResult(
            state={"run_id": ctx.run_id, "final_status": "escalated",
                   "escalation_reason": "do-nothing agent: takes no action (floor baseline)",
                   "limits": {"tool_calls": ctx.max_tool_calls}},
            tool_calls=[],
        )


AGENTS: dict[str, Callable[[], Agent]] = {"do-nothing": DoNothingAgent}
