"""How long the runner waits for the agent on one case.

Person 2's graph waits for recovery after every fix (``verify``: up to ``verify_window_s``, or the
signal's own window in ``verify_window_by_signal``). A memory fault may get a follow-up step with a
second wait (``follow_up_signals``). A fix that does not verify is rolled back, and the commander may
re-plan (``max_replans``) and try another fix. A fixed 600 s cut those runs off as "timeout" (review of
#29), which would score a slow but correct agent as a failure.

So the default deadline is the worst case the graph's own settings (config/app.yaml) allow for the
signal the case's alert watches::

    attempts  = 1 + max_replans
    verifies  = 2 if the signal can get a follow-up step, else 1
    timeout   = attempts * (step_allowance_s + verifies * (window + verify_poll_s))

``step_allowance_s`` (run config, default 300 s) is everything else in one attempt: diagnosis, runbook,
dry run, approval, rollback. ``--agent-timeout`` replaces the whole calculation with one number.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

APP_YAML = Path(__file__).resolve().parents[2] / "config" / "app.yaml"


@dataclasses.dataclass(frozen=True)
class GraphTiming:
    """The graph settings that decide how long one incident can take (defaults: copilot/config.py)."""

    verify_window_s: int = 300
    verify_window_by_signal: Mapping[str, int] = dataclasses.field(default_factory=dict)
    verify_poll_s: int = 30
    follow_up_signals: tuple[str, ...] = ()
    max_replans: int = 2

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "GraphTiming":
        d = cls()
        return cls(verify_window_s=int(raw.get("verify_window_s", d.verify_window_s)),
                   verify_window_by_signal={k: int(v) for k, v in (raw.get("verify_window_by_signal") or {}).items()},
                   verify_poll_s=int(raw.get("verify_poll_s", d.verify_poll_s)),
                   follow_up_signals=tuple(raw.get("follow_up_signals") or ()),
                   max_replans=int(raw.get("max_replans", d.max_replans)))

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "GraphTiming":
        """Read config/app.yaml; a missing file gives the graph's code defaults."""
        p = Path(path) if path is not None else APP_YAML
        if not p.is_file():
            return cls()
        return cls.from_mapping(yaml.safe_load(p.read_text()) or {})


def agent_timeout_for(signal: str, timing: GraphTiming, step_allowance_s: float) -> float:
    """Worst-case seconds the graph may take on an alert watching ``signal`` (see the module docstring)."""
    window = timing.verify_window_by_signal.get(signal, timing.verify_window_s)
    verifies = 2 if signal in timing.follow_up_signals else 1
    attempts = 1 + max(0, timing.max_replans)
    return float(attempts * (step_allowance_s + verifies * (window + timing.verify_poll_s)))
