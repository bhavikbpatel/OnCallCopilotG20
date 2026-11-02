"""Run configuration: everything that differs between experiment arms lives here, not in code."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")   # e.g. "E1-C2"; used in file names
    agent: Literal["do-nothing"] = "do-nothing"
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
