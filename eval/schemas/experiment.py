"""Experiments E1–E4: which arms to run, on which cases, how many times (capstone guide pp. 5–6).

One YAML file per experiment in ``eval/experiments/``. Each arm is one run configuration of the copilot:
its architecture (C0, C1, C2), memory, defences and tool-call budget, the case set and the repeats.
Fairness is checked when the files load: each ``compare`` group lists the arms that are compared with
each other and the one setting (or few) that ``varies`` between them; every other setting (cases,
repeats, budget, memory...) must be the same in all arms of the group. The model is config/app.yaml for
every arm.

An arm can ``reuse`` an identical arm of another experiment instead of running again (E1's C2 is also
E3's "after" and E4's "memory on, budget 20"). The loader checks the two really are identical.

**Sampling** (the shop time does not fit the full plan): an arm may run on ``sample`` cases instead of the
whole set. ``sample_cases`` picks them by code, spread over fault types (noise and two-fault cases are groups
of their own), in an order fixed by the experiment's ``sample_seed`` — the same set, case pool, size and seed
always give the same cases, chosen before anything is run, never by hand. Arms in one comparison may differ in
``sample`` and ``repeats``: the report then compares them on the cases they have in common (all repeats), and
says so. Everything else must still be equal.

Every experiment arm runs with ``review_enabled: false``: measuring never writes lessons to memory.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

CaseSet = Literal["dev", "heldout", "attacks"]   # attacks: red-team folder, each with its dev base case
Varied = Literal["architecture", "memory", "defences", "max_tool_calls"]
SETTINGS = ("architecture", "memory", "defences", "max_tool_calls", "cases", "only", "repeats", "sample")
# May differ between compared arms: the report compares them on their common cases (see the module docstring).
SCOPE_SETTINGS = ("repeats", "sample")
EXPERIMENTS_DIR = Path(__file__).resolve().parents[1] / "experiments"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentOptions(_Strict):
    """What the copilot agent must honour for this arm. An agent that cannot is refused by the runner."""

    architecture: Literal["C0", "C1", "C2"] = "C2"   # C0 fixed workflow, C1 one agent, C2 our team
    memory: Literal["playbook", "off"] = "playbook"  # playbook: frozen lessons, read-only; off: no memory store
    defences: Literal["on", "off"] = "on"            # COPILOT_DEFENCES for the hooks and guardrails


class Arm(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    description: str = ""
    options: AgentOptions = Field(default_factory=AgentOptions)
    max_tool_calls: int = Field(20, gt=0)
    cases: Optional[CaseSet] = None       # default: the experiment's
    only: list[str] = Field(default_factory=list)   # case (or attack) IDs; empty = the whole set
    repeats: Optional[int] = Field(None, ge=1)      # default: the experiment's
    sample: Optional[int] = Field(None, gt=0)       # run on this many cases, picked by sample_cases; None = all
    reuse: Optional[str] = Field(None, pattern=r"^E[1-4]/[A-Za-z0-9][A-Za-z0-9_.-]*$")   # "E1/C2"


class Comparison(_Strict):
    arms: list[str] = Field(min_length=2)
    varies: list[Varied] = Field(min_length=1)


class Experiment(_Strict):
    experiment: Literal["E1", "E2", "E3", "E4"]
    question: str
    guide: str = ""
    cases: CaseSet
    repeats: int = Field(1, ge=1)
    sample_seed: int = 0                             # fixes which cases a sampled arm runs on
    arms: list[Arm] = Field(min_length=2)
    compare: list[Comparison] = Field(min_length=1)
    notes: str = ""

    def settings(self, arm: Arm) -> dict[str, Any]:
        """The arm's full settings, defaults filled in. Two arms with equal settings measure the same thing."""
        return {"architecture": arm.options.architecture, "memory": arm.options.memory,
                "defences": arm.options.defences, "max_tool_calls": arm.max_tool_calls,
                "cases": arm.cases or self.cases, "only": sorted(arm.only),
                "repeats": arm.repeats or self.repeats,
                "sample": None if arm.sample is None else {"n": arm.sample, "seed": self.sample_seed}}

    def arm(self, arm_id: str) -> Arm:
        for a in self.arms:
            if a.id == arm_id:
                return a
        raise KeyError(f"{self.experiment} has no arm {arm_id!r}; arms: {[a.id for a in self.arms]}")

    @model_validator(mode="after")
    def _fair(self) -> "Experiment":
        ids = [a.id for a in self.arms]
        if len(set(ids)) != len(ids):
            raise ValueError(f"{self.experiment}: arm IDs must be unique, got {ids}")
        compared = set()
        for c in self.compare:
            unknown = [i for i in c.arms if i not in ids]
            if unknown:
                raise ValueError(f"{self.experiment}: compare names unknown arms {unknown}")
            fixed = [k for k in SETTINGS if k not in c.varies and k not in SCOPE_SETTINGS]
            first = self.settings(self.arm(c.arms[0]))
            for i in c.arms[1:]:
                s = self.settings(self.arm(i))
                diff = [k for k in fixed if s[k] != first[k]]
                if diff:
                    raise ValueError(f"{self.experiment}: arm {i} differs from {c.arms[0]} in {diff}, but the "
                                     f"comparison varies only {c.varies}; that would not be a fair comparison")
                if all(s[k] == first[k] for k in c.varies):
                    raise ValueError(f"{self.experiment}: arms {c.arms[0]} and {i} have the same {c.varies}; "
                                     f"the comparison would measure nothing")
            compared.update(c.arms)
        for a in self.arms:
            if a.sample is not None and (a.cases or self.cases) == "attacks":
                raise ValueError(f"{self.experiment}/{a.id}: sample works on cases, not on attacks")
        idle = sorted(set(ids) - compared)
        if idle:
            raise ValueError(f"{self.experiment}: arms {idle} are in no comparison")
        return self


def stratum(case: Any) -> str:
    """The group a case is sampled from: noise, two or more faults, or its fault type."""
    if case.expected.verdict == "noise":
        return "noise"
    faults = sorted({f.flag for f in case.faults})
    return faults[0] if len(faults) == 1 else "multi-fault"


def sample_cases(cases: list, n: int, seed: int) -> list:
    """``n`` of ``cases``, spread over strata in proportion to their size (largest remainder), every stratum
    getting at least one case when ``n`` allows. Within a stratum, cases are taken in the order of
    sha256("seed:case_id"): deterministic, unrelated to results, and the same for every arm with the same seed.
    Returned in the order of ``cases``."""
    if n >= len(cases):
        return list(cases)
    groups: dict[str, list] = defaultdict(list)
    for c in cases:
        groups[stratum(c)].append(c)
    for g in groups.values():
        g.sort(key=lambda c: hashlib.sha256(f"{seed}:{c.case_id}".encode()).hexdigest())
    total = len(cases)
    exact = {k: n * len(g) / total for k, g in groups.items()}
    quota = {k: int(v) for k, v in exact.items()}
    for k in sorted(groups, key=lambda k: (-(exact[k] - quota[k]), k))[:n - sum(quota.values())]:
        quota[k] += 1
    if n >= len(groups):                       # every stratum at least once, taken from the largest quotas
        for k in sorted(groups):
            if quota[k] == 0:
                donor = max((d for d in quota if quota[d] > 1), key=lambda d: (quota[d], d))
                quota[donor] -= 1
                quota[k] = 1
    chosen = {c.case_id for k, g in groups.items() for c in g[:quota[k]]}
    return [c for c in cases if c.case_id in chosen]


def config_hash(settings: dict[str, Any]) -> str:
    """Short, stable hash of an arm's settings (written into every result line of the arm)."""
    blob = json.dumps(settings, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def load_experiment(path: Path) -> Experiment:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    exp = Experiment.model_validate(raw)
    if Path(path).stem != exp.experiment:
        raise ValueError(f"{path}: file name must be {exp.experiment}.yaml")
    return exp


def load_experiments(folder: Path = EXPERIMENTS_DIR) -> dict[str, Experiment]:
    """Every E*.yaml in ``folder``, with ``reuse`` links checked across files."""
    exps = {e.experiment: e for e in (load_experiment(p) for p in sorted(Path(folder).glob("E*.yaml")))}
    for e in exps.values():
        for a in e.arms:
            if a.reuse is None:
                continue
            target_exp, _, target_arm = a.reuse.partition("/")
            if target_exp not in exps:
                raise ValueError(f"{e.experiment}/{a.id} reuses {a.reuse}, but {target_exp}.yaml is not loaded")
            t = exps[target_exp].arm(target_arm)
            if t.reuse is not None:
                raise ValueError(f"{e.experiment}/{a.id} reuses {a.reuse}, which itself reuses {t.reuse}; "
                                 f"point at the arm that runs")
            mine, theirs = e.settings(a), exps[target_exp].settings(t)
            diff = [k for k in mine if k != "repeats" and mine[k] != theirs[k]]
            if diff or theirs["repeats"] < mine["repeats"]:
                raise ValueError(f"{e.experiment}/{a.id} reuses {a.reuse} but their settings differ in "
                                 f"{diff or ['repeats']}; only identical runs may be shared")
    return exps


def resolve(name: str, folder: Path = EXPERIMENTS_DIR) -> Experiment:
    exps = load_experiments(folder)
    if name not in exps:
        raise ValueError(f"unknown experiment {name!r}; found {sorted(exps)} in {folder}")
    return exps[name]
