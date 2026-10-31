"""Live check of the recovery checker against the running shop. Skipped unless EVAL_LIVE=1.

Read-only (no flags changed). On a healthy shop both forms must say "recovered".
Takes about 2 minutes: the window form waits for two distinct Prometheus points.
"""
from __future__ import annotations

import os
import time

import pytest

from eval.recovery import check_recovery
from eval.schemas.case import RecoveryCheck
from eval.telemetry import PromClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("EVAL_LIVE") != "1", reason="set EVAL_LIVE=1 to run against the real shop"),
]


def test_live_since_fix_on_healthy_frontend():
    check = RecoveryCheck(signal="error_rate", service="frontend", measure="since_fix",
                          threshold=0.05, within_s=300, settle_s=90)
    fixed_at = time.time() - 400        # a "fix" in the past: the whole range has already arrived
    obs = check_recovery(PromClient(), check, fixed_at)
    assert obs.recovered is True, obs
    assert obs.requests and obs.requests > 100


def test_live_window_on_healthy_frontend_latency():
    check = RecoveryCheck(signal="latency_p95_ms", service="frontend", threshold=5000.0,
                          within_s=300, settle_s=0)
    obs = check_recovery(PromClient(), check, time.time() - 60)
    assert obs.recovered is True, obs
    assert len(obs.points) == 2
