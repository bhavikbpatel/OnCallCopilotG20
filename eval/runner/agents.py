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
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


@dataclasses.dataclass(frozen=True)
class AgentContext:
    run_id: str
    alert: dict                              # Person 2's Alert fields
    approve: Callable[[dict], dict]          # flat action -> ApprovalDecision (decision, payload_hash; no token: the gate mints it)
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

    # Optional: True for an agent whose approvals go through Person 2's gate, which mints tokens with
    # APPROVAL_SECRET. Agents without the attribute need nothing.
    # needs_approval_secret: bool


def not_ready(agent: Agent, env: Optional[Mapping[str, str]] = None, env_file: Optional[Path] = None) -> Optional[str]:
    """Why ``agent`` cannot run, or None. Without APPROVAL_SECRET the gate refuses every approval, so every
    case would end quietly as an escalation; the runner stops before the first case instead."""
    if not getattr(agent, "needs_approval_secret", False):
        return None
    env = os.environ if env is None else env
    if env.get("APPROVAL_SECRET"):
        return None
    path = ENV_FILE if env_file is None else env_file
    if path.is_file():
        try:
            from dotenv import dotenv_values
        except ImportError:
            dotenv_values = None
        if dotenv_values is not None and dotenv_values(path).get("APPROVAL_SECRET"):
            return None
    return (f"agent {agent.name} needs APPROVAL_SECRET (in the environment or {path.name}): without it the "
            f"approval gate refuses every approval and every case would end as an escalation")


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
