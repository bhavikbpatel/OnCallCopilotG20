"""Run configuration: everything that differs between experiment arms lives here, not in code."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")   # e.g. "E1-C2"; used in file names
    agent: Literal["do-nothing"] = "do-nothing"
    approval: Literal["approve", "reject"] = "approve"
    max_tool_calls: int = Field(20, gt=0)
    agent_timeout_s: float = Field(600, gt=0)
    alert_timeout_s: float = Field(420, gt=0)     # kafka lag needs ~5 min to build
    baseline_readings: int = Field(3, ge=1)
    poll_s: float = Field(30, gt=0)
    repeats: int = Field(1, ge=1)
