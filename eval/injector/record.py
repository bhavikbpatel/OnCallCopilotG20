"""Ground-truth record of what the injector actually did during one run.

Written under eval/results/ (git-ignored). Never passed to the agent.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path
from typing import Optional

from ..schemas.case import Case

RECORDS_DIR = Path(__file__).resolve().parents[1] / "results" / "ground_truth"


@dataclasses.dataclass
class InjectionRecord:
    run_id: str
    case_id: str
    horizon_s: float
    planned_flags: list[str]
    plan: list[dict]
    started_at: dt.datetime
    applied: list[dict] = dataclasses.field(default_factory=list)
    drift: list[dict] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)
    cleared: bool = False
    ended_at: Optional[dt.datetime] = None

    @classmethod
    def new(cls, run_id: str, case: Case, horizon_s: float, plan, planned_flags, now) -> "InjectionRecord":
        return cls(
            run_id=run_id,
            case_id=case.case_id,
            horizon_s=horizon_s,
            planned_flags=list(planned_flags),
            plan=[dataclasses.asdict(e) for e in plan],
            started_at=now,
        )

    @property
    def contaminated(self) -> bool:
        return bool(self.drift)

    @property
    def ok(self) -> bool:
        return self.cleared and not self.errors and not self.contaminated

    @property
    def first_fault_at(self) -> Optional[dt.datetime]:
        return self.applied[0]["applied_at"] if self.applied else None

    def add_applied(self, at_s: float, when: dt.datetime, changes: dict[str, str]) -> None:
        self.applied.append({"at_s": at_s, "applied_at": when, "changes": dict(changes)})

    def add_drift(self, when: dt.datetime, flag: str, seen: str, expected: str) -> None:
        # One entry per (flag, seen value): repeated checks do not flood the record.
        if not any(d["flag"] == flag and d["seen"] == seen for d in self.drift):
            self.drift.append({"seen_at": when, "flag": flag, "seen": seen, "expected": expected})

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["contaminated"] = self.contaminated
        d["ok"] = self.ok
        return d

    def save(self, directory: Optional[Path] = None) -> Path:
        directory = Path(directory) if directory is not None else RECORDS_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.run_id}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2, default=lambda o: o.isoformat()) + "\n")
        return path
