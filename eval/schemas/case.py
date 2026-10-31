"""Golden-set case specification.

One YAML file per case. The expected outcome is written before the agent ever
runs on the case. Held-out cases use the same format but live outside the repo.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .catalogue import (
    EXCLUDED_FAULT_TYPES,
    FAULT_TYPES,
    FLAG_VARIANTS,
    SERVICES,
    accepted_services,
    check_action_args,
    normalize_severity,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- actions

class Action(_Strict):
    """A shop-changing action, e.g. set_flag(paymentFailure, off)."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _known_tool_and_args(self) -> "Action":
        check_action_args(self.tool, self.args, complete=True)
        return self


class ActionPattern(_Strict):
    """Matches any action with this tool whose args include every arg given here.

    ``{tool: restart_service}`` matches every restart; ``{tool: restart_service, args:
    {service: payment}}`` matches only restarting payment.
    """

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _known_tool_and_args(self) -> "ActionPattern":
        check_action_args(self.tool, self.args, complete=False)
        return self


# ---------------------------------------------------------------- faults and load

class FaultSpec(_Strict):
    flag: str
    variant: str
    start_after_s: int = Field(0, ge=0)
    mode: Literal["steady", "intermittent"] = "steady"
    on_s: Optional[int] = Field(None, gt=0)
    off_s: Optional[int] = Field(None, gt=0)

    @model_validator(mode="after")
    def _check(self) -> "FaultSpec":
        if self.flag not in FAULT_TYPES:
            raise ValueError(f"{self.flag!r} is not one of the 12 fault types")
        if self.flag in EXCLUDED_FAULT_TYPES:
            raise ValueError(f"{self.flag} is excluded from the golden set: {EXCLUDED_FAULT_TYPES[self.flag]}")
        if self.variant not in FLAG_VARIANTS[self.flag]:
            raise ValueError(
                f"variant {self.variant!r} not valid for {self.flag}; "
                f"choose from {FLAG_VARIANTS[self.flag]}"
            )
        if self.mode == "intermittent" and not (self.on_s and self.off_s):
            raise ValueError("intermittent faults need on_s and off_s")
        if self.mode == "steady" and (self.on_s or self.off_s):
            raise ValueError("on_s/off_s only apply to intermittent faults")
        return self


class LoadSpec(_Strict):
    """normal = default Locust traffic; flood = loadGeneratorFloodHomepage on."""

    profile: Literal["normal", "flood"] = "normal"
    flood_start_after_s: int = Field(0, ge=0)
    flood_duration_s: Optional[int] = Field(None, gt=0)  # None = until the case is cleared

    @model_validator(mode="after")
    def _check(self) -> "LoadSpec":
        if self.profile == "normal" and (self.flood_start_after_s or self.flood_duration_s):
            raise ValueError("flood_* settings only apply when profile is 'flood'")
        return self


class AlertSpec(_Strict):
    """The alert expected to open the incident. Names must match the Prometheus rules."""

    name: str
    service: str
    provisional: bool = True  # true until Person 5's alert rules exist

    @field_validator("service")
    @classmethod
    def _svc(cls, v: str) -> str:
        if v not in SERVICES:
            raise ValueError(f"unknown service {v!r}")
        return v


# ---------------------------------------------------------------- expectations

class RootCause(_Strict):
    service: str
    fault_type: str

    @model_validator(mode="after")
    def _check(self) -> "RootCause":
        if self.fault_type not in FAULT_TYPES:
            raise ValueError(f"unknown fault_type {self.fault_type!r}")
        if self.service not in accepted_services(self.fault_type):
            raise ValueError(
                f"service {self.service!r} is not the root cause of {self.fault_type} "
                f"(expected one of {sorted(accepted_services(self.fault_type))})"
            )
        return self


class RecoveryCheck(_Strict):
    """Checked independently by the harness, never taken from the agent.

    ``signal`` is a named signal from eval/telemetry/signals.py. Two ways to judge:

    - ``measure: window`` (default): the signal's value ``within_s`` after the fix,
      compared either with a fixed ``threshold`` or with ``baseline_factor`` times
      the value the runner measured just before injecting. Use the baseline form
      for latency and memory: healthy levels differ a lot between sessions
      (ad p95 was 7.6 ms in one run and 88 ms in another).
    - ``measure: since_fix``: errors/rates counted from ``settle_s`` after the fix
      until ``within_s`` after it. Use this on low-traffic paths (checkout sees
      ~2.5 requests/min) and for rare operations (cartFailure breaks only EmptyCart).
      ``settle_s`` skips errors that happened before the fix but arrive late
      (metrics reach Prometheus 60-120 s behind).
    """

    signal: Literal["error_rate", "error_count", "latency_p95_ms", "latency_p50_ms", "throughput_rps", "memory_mb",
                    "cpu_percent", "consumer_lag"]
    service: str
    measure: Literal["window", "since_fix"] = "window"
    op: Literal["lt", "gt"] = "lt"
    threshold: Optional[float] = None
    baseline_factor: Optional[float] = Field(None, gt=0)
    within_s: int = Field(300, gt=0)
    settle_s: int = Field(90, ge=0)

    @field_validator("service")
    @classmethod
    def _svc(cls, v: str) -> str:
        if v not in SERVICES:
            raise ValueError(f"unknown service {v!r}")
        return v

    @model_validator(mode="after")
    def _check(self) -> "RecoveryCheck":
        if (self.threshold is None) == (self.baseline_factor is None):
            raise ValueError("give exactly one of threshold or baseline_factor")
        if self.measure == "since_fix":
            if self.signal not in ("error_rate", "error_count"):
                raise ValueError("since_fix only applies to error_rate and error_count")
            if self.baseline_factor is not None:
                raise ValueError("since_fix uses a fixed threshold, not baseline_factor")
            if self.within_s - self.settle_s < 120:
                raise ValueError("since_fix needs at least 120 s between settle_s and within_s "
                                 "(metrics arrive about once a minute)")
        if self.signal == "cpu_percent":
            raise ValueError("cpu_percent behaves like a slow running average in this shop and "
                             "cannot show recovery; use latency instead")
        return self


class SeverityExpectation(_Strict):
    level: Literal["SEV-1", "SEV-2", "SEV-3", "SEV-4", "NONE"]
    rule: Optional[str] = None  # rule ID from Person 1's severity policy
    provisional: bool = True  # true until the policy is agreed

    @field_validator("level", mode="before")
    @classmethod
    def _norm(cls, v: Any) -> Any:
        return normalize_severity(v) if isinstance(v, str) else v


class Expected(_Strict):
    verdict: Literal["incident", "noise"]
    root_causes: list[RootCause] = Field(default_factory=list)
    # Alternatives: the run is correct if ANY one option is fully satisfied.
    correct_actions: list[list[Action]] = Field(default_factory=list)
    # Not required, but not counted as unnecessary either.
    acceptable_actions: list[Action] = Field(default_factory=list)
    # Executing any of these counts as harmful.
    forbidden_actions: list[ActionPattern] = Field(default_factory=list)
    recovery: Optional[RecoveryCheck] = None
    severity: SeverityExpectation

    @property
    def should_act(self) -> bool:
        return self.verdict == "incident"


class Case(_Strict):
    case_id: str = Field(pattern=r"^[DH]-\d{3}$")
    title: str
    scenario_group: str
    split: Literal["dev", "heldout"]
    written_on: dt.date
    written_by: str = "Person 4"
    faults: list[FaultSpec] = Field(default_factory=list)
    load: LoadSpec = Field(default_factory=LoadSpec)
    alert: AlertSpec
    expected: Expected
    notes: str = ""
    needs_verification: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> "Case":
        prefix = {"dev": "D-", "heldout": "H-"}[self.split]
        if not self.case_id.startswith(prefix):
            raise ValueError(f"{self.split} case IDs must start with {prefix}")

        exp = self.expected
        if exp.verdict == "noise":
            if self.faults:
                raise ValueError("noise cases inject no faults")
            if self.load.profile != "flood":
                raise ValueError("noise cases need a load spike (load.profile: flood)")
            if exp.root_causes or exp.correct_actions:
                raise ValueError("noise cases have no root cause and no correct action")
            if exp.severity.level != "NONE":
                raise ValueError("noise cases have severity NONE")
        else:
            if not self.faults:
                raise ValueError("incident cases need at least one fault")
            if not exp.correct_actions:
                raise ValueError("incident cases need at least one correct action option")
            if exp.recovery is None:
                raise ValueError("incident cases need a recovery check")
            injected = sorted(f.flag for f in self.faults)
            expected = sorted(rc.fault_type for rc in exp.root_causes)
            if injected != expected:
                raise ValueError(
                    f"root_causes {expected} must match the injected faults {injected}"
                )
        return self


def load_case(path: str | Path) -> Case:
    path = Path(path)
    data = yaml.safe_load(path.read_text())
    case = Case.model_validate(data)
    if path.stem != case.case_id:
        raise ValueError(f"{path.name}: file name must be {case.case_id}.yaml")
    return case


def load_cases(directory: str | Path) -> list[Case]:
    cases = [load_case(p) for p in sorted(Path(directory).glob("*.yaml"))]
    ids = [c.case_id for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate case IDs: {sorted(dupes)}")
    return cases
