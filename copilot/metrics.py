"""Prometheus metrics for the copilot (Person 5): one place for every name.

Recorded from the eval result line, which already carries everything the
dashboard needs (outcome, tool calls, actions, cost, recovery), so the graph
and the hooks need no extra calls:

    from copilot import metrics
    metrics.serve()                          # if COPILOT_METRICS_PORT is set
    metrics.record_run(result, report)       # once per finished case or incident

The Grafana dashboard (infra/grafana/dashboards/agent.json) and the alert
rules (infra/prometheus/rules/agent.yml) query these exact names; a test fails
if they drift. prometheus_client is optional: without it everything here is a
no-op, so eval/ keeps its small requirements.
"""
from __future__ import annotations

import os
from typing import Any, Optional

try:
    from prometheus_client import Counter, Histogram, start_http_server
except ImportError:  # pragma: no cover - exercised only without the package
    Counter = Histogram = start_http_server = None

ENABLED = Counter is not None
_LATENCY_BUCKETS = (30, 60, 120, 180, 300, 450, 600, 900, 1200, 1800)

# Hook block reasons the CopilotLoopOrBudget alert matches (copilot/hooks.py; budget ones are
# copilot/run.py BUDGET_BLOCKS). Their series start at 0 when this module loads: Prometheus's
# increase() ignores a counter's first sample, so a series born at 1 would never fire the alert.
ALERT_BLOCK_REASONS = ("loop_detected", "budget_exceeded", "token_budget_exceeded", "cost_budget_exceeded",
                       "agent_budget_exceeded")

if ENABLED:
    INCIDENTS = Counter(
        "copilot_incidents_total",
        "Finished incidents (eval runs included), by run status and the agent's final status.",
        ["config_id", "status", "final_status"],
    )
    TASK_SUCCESS = Counter(
        "copilot_task_success_total", "Runs scored by the checker, by task success.", ["config_id", "success"])
    INCIDENT_DURATION = Histogram(
        "copilot_incident_duration_seconds", "Run start to end.", ["final_status"], buckets=_LATENCY_BUCKETS)
    TIME_TO_RECOVER = Histogram(
        "copilot_time_to_recover_seconds", "Run start to end, for runs the independent check saw recover.",
        buckets=_LATENCY_BUCKETS)
    TOOL_CALLS = Counter(
        "copilot_tool_calls_total", "Tool calls from the hooks log.", ["agent_role", "tool", "kind", "outcome"])
    TOOL_BLOCKS = Counter(
        "copilot_tool_calls_blocked_total", "Calls the hooks blocked, by reason.", ["reason"])
    for _reason in ALERT_BLOCK_REASONS:
        TOOL_BLOCKS.labels(reason=_reason)
    TOOL_CALLS_PER_INCIDENT = Histogram(
        "copilot_tool_calls_per_incident", "Executed (not blocked) tool calls per run.",
        buckets=(1, 2, 5, 8, 10, 15, 20, 30, 50))
    ACTIONS = Counter(
        "copilot_actions_total", "Write actions executed, by tool and purpose.", ["tool", "purpose"])
    ROLLBACKS = Counter("copilot_rollbacks_total", "Runs that ended with the fix rolled back.")
    # The usual way a budget runs out: the graph checks it before planning more and escalates, so no call is
    # blocked (only model-driven calls count; the graph's own code-driven calls are never budgeted).
    BUDGET_EXHAUSTED = Counter(
        "copilot_budget_exhausted_total", "Runs the graph stopped because the tool-call, token or cost budget ran out.")
    HARMFUL = Counter("copilot_harmful_actions_total", "Harmful actions found by the checker (target: 0).")
    POLICY_VIOLATIONS = Counter("copilot_policy_violations_total", "Policy violations found by the checker.")
    REPLANS = Counter("copilot_replans_total", "Replans (refuted hypotheses that changed the plan).")
    COST_USD = Counter("copilot_llm_cost_usd_total", "LLM spend in USD.", ["config_id"])
    TOKENS = Counter("copilot_llm_tokens_total", "LLM tokens.", ["config_id"])

_served = False


def serve(port: Optional[int] = None) -> Optional[int]:
    """Serve /metrics on COPILOT_METRICS_PORT (or `port`). Returns the port, or None if off."""
    global _served
    port = port or int(os.environ.get("COPILOT_METRICS_PORT") or 0)
    if not ENABLED or not port or _served:
        return port if _served else None
    start_http_server(port)
    _served = True
    return port


def _tool_outcome(call: Any) -> str:
    if call.blocked:
        return "blocked"
    return "ok" if call.ok else "error"


def record_run(result: Any, report: Any = None) -> None:
    """Record one finished run from its ResultLine (and the checker's CheckReport, if scored)."""
    if not ENABLED:
        return
    final = result.final_status or "none"
    INCIDENTS.labels(config_id=result.config_id, status=result.status, final_status=final).inc()
    INCIDENT_DURATION.labels(final_status=final).observe(result.latency_s)
    if result.recovery.recovered:
        TIME_TO_RECOVER.observe(result.latency_s)

    executed = 0
    for call in result.tool_calls:
        TOOL_CALLS.labels(agent_role=call.agent_role, tool=f"{call.server}.{call.tool}", kind=call.kind,
                          outcome=_tool_outcome(call)).inc()
        if call.blocked:
            TOOL_BLOCKS.labels(reason=call.block_reason or "unknown").inc()
            continue
        executed += 1
        if call.kind == "write" and call.ok and call.action_id:
            ACTIONS.labels(tool=call.tool, purpose=call.purpose or "none").inc()
    TOOL_CALLS_PER_INCIDENT.observe(executed)

    if result.verification.rolled_back or final == "rolled_back":
        ROLLBACKS.inc()
    if result.status == "budget_exhausted":
        BUDGET_EXHAUSTED.inc()
    if result.replans:
        REPLANS.inc(result.replans)
    if result.cost.usd:
        COST_USD.labels(config_id=result.config_id).inc(result.cost.usd)
    tokens = result.cost.tokens_total or (result.cost.tokens_in + result.cost.tokens_out)
    if tokens:
        TOKENS.labels(config_id=result.config_id).inc(tokens)

    if report is not None:
        TASK_SUCCESS.labels(config_id=result.config_id, success=str(bool(report.task_success)).lower()).inc()
        if report.harmful_actions:
            HARMFUL.inc(len(report.harmful_actions))
        if report.policy_violations:
            POLICY_VIOLATIONS.inc(len(report.policy_violations))
