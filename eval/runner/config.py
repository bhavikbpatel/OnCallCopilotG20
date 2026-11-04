"""Run configuration: everything that differs between experiment arms lives here, not in code."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")   # e.g. "E1-C2"; used in file names
    agent: str = "do-nothing"                     # a name in eval/runner/agents.py AGENTS (e.g. "copilot")
    approval: Literal["approve", "reject"] = "approve"
    max_tool_calls: int = Field(20, gt=0)
    # None: worked out per case from the graph's verify windows (eval/runner/timeouts.py); a number fixes it.
    agent_timeout_s: Optional[float] = Field(None, gt=0)
    step_allowance_s: float = Field(300, gt=0)    # per attempt, besides waiting for recovery
    alert_timeout_s: float = Field(420, gt=0)     # kafka lag needs ~5 min to build
    baseline_readings: int = Field(3, ge=1)
    poll_s: float = Field(30, gt=0)
    repeats: int = Field(1, ge=1)
    # Experiment arm (eval/experiments/*.yaml): the agent options it must honour (architecture, memory,
    # defences, review_enabled), which arm this is, and the hash of the arm's settings for the result lines.
    options: dict[str, Any] = Field(default_factory=dict)
    experiment: Optional[str] = None
    arm: Optional[str] = None
    config_hash: Optional[str] = None

    @field_validator("agent")
    @classmethod
    def _known_agent(cls, v: str) -> str:
        from .agents import AGENTS                # imported here: agents.py must not depend on this module
        if v not in AGENTS:
            raise ValueError(f"unknown agent {v!r}; expected one of {sorted(AGENTS)}")
        return v
