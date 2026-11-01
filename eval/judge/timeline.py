"""Code check: does the postmortem timeline match the run's own record?

Person 2's comms agent copies the timeline from state ("Timeline comes from state, never from the
model", copilot/agents/comms.py), so this is a plain comparison, not a judge question:

``matches`` (the timeline matches the run) needs both:

- every postmortem event appears in the run's timeline (same time, node, kind and text): nothing invented;
- the postmortem leaves out no ``action``, ``approval`` or ``rollback`` event that the run recorded
  before the postmortem was written, i.e. before the run's first ``comms`` event (those are what a
  reader of a postmortem must not miss).

``ordered`` is reported separately and does not affect ``matches``: Person 2's graph runs diagnosis
branches in parallel, so its own timeline can list a later event before an earlier one (seen in the
judge preview on 1 Nov). That is a presentation issue for the graph, not a mismatch with the run.

``checked`` is False when the run wrote no postmortem; ``matches`` and ``ordered`` are then None, never True.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from ..schemas.result import Explanation, ExplanationEvent

MUST_KEEP = {"action", "approval", "rollback"}


class TimelineCheck(BaseModel):
    checked: bool = False
    matches: Optional[bool] = None
    invented: int = 0                 # postmortem events not in the run's timeline
    missing_key_events: int = 0       # action/approval/rollback events left out
    ordered: Optional[bool] = None    # postmortem events in time order (reported; not part of matches)
    reason: Optional[str] = None


def _key(e: ExplanationEvent) -> tuple:
    return (e.at, e.node, e.kind, e.text.strip())


def check_timeline(ex: Optional[Explanation]) -> TimelineCheck:
    if ex is None or not ex.postmortem_timeline:
        return TimelineCheck(reason="no postmortem timeline")
    run = {_key(e) for e in ex.timeline}
    pm = ex.postmortem_timeline
    invented = sum(1 for e in pm if _key(e) not in run)
    times = [e.at for e in pm if e.at is not None]
    out_of_order = any(b < a for a, b in zip(times, times[1:]))
    # Events recorded before the postmortem was written: up to the run's first comms event
    # (Person 2's comms node), else up to the postmortem's own last event.
    comms = [e.at for e in ex.timeline if e.kind == "comms" and e.at is not None]
    cutoff = min(comms) if comms else (max(times) if times else None)
    pm_keys = {_key(e) for e in pm}
    missing = sum(1 for e in ex.timeline
                  if e.kind in MUST_KEEP and (cutoff is None or e.at is None or e.at <= cutoff)
                  and _key(e) not in pm_keys)
    problems = [r for r, bad in ((f"{invented} event(s) not in the run's timeline", invented),
                                 (f"{missing} action/approval/rollback event(s) left out", missing)) if bad]
    notes = problems + (["events not in time order (reported; not a mismatch)"] if out_of_order else [])
    return TimelineCheck(checked=True, matches=not problems, invented=invented, missing_key_events=missing,
                         ordered=not out_of_order, reason="; ".join(notes) or None)
