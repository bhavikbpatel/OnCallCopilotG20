"""Builds infra/grafana/dashboards/agent.json. Edit here, then run:

    python infra/grafana/build_dashboard.py

Metric names come from copilot/metrics.py (tests/test_infra.py checks them).
Panels follow the backlog: time to diagnose/recover, tool calls per incident,
actions rolled back, errors and cost per incident, plus the shop's own signal.
"""

import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "webstore-metrics"}  # the shop's Prometheus datasource
OUT = Path(__file__).resolve().parent / "dashboards" / "agent.json"


def panel(pid, title, kind, x, y, w, h, targets, unit=None, description=""):
    p = {
        "id": pid, "title": title, "type": kind, "datasource": DS, "description": description,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"datasource": DS, "expr": e, "legendFormat": legend, "refId": chr(65 + i)}
                    for i, (e, legend) in enumerate(targets)],
        "fieldConfig": {"defaults": {"unit": unit} if unit else {}, "overrides": []},
        "options": {},
    }
    if kind == "stat":
        p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "none"}
    return p


def quantiles(metric, by=""):
    grouping = f"le{', ' + by if by else ''}"
    return [(f"histogram_quantile({q}, sum by ({grouping}) (increase({metric}_bucket[$__range])))", f"p{int(q * 100)}")
            for q in (0.5, 0.95)]


RANGE = "[$__range]"
panels = [
    panel(1, "Runs by final status", "stat", 0, 0, 8, 5,
          [(f"sum by (final_status) (increase(copilot_incidents_total{RANGE}))", "{{final_status}}")]),
    panel(2, "Task success", "stat", 8, 0, 4, 5,
          [(f'sum(increase(copilot_task_success_total{{success="true"}}{RANGE})) / '
            f"sum(increase(copilot_task_success_total{RANGE}))", "success")], unit="percentunit"),
    panel(3, "Rollbacks", "stat", 12, 0, 3, 5, [(f"sum(increase(copilot_rollbacks_total{RANGE}))", "rollbacks")]),
    panel(4, "Harmful actions", "stat", 15, 0, 3, 5,
          [(f"sum(increase(copilot_harmful_actions_total{RANGE}))", "harmful")],
          description="Target is zero"),
    panel(5, "Cost per run", "stat", 18, 0, 3, 5,
          [(f"sum(increase(copilot_llm_cost_usd_total{RANGE})) / "
            f"clamp_min(sum(increase(copilot_incidents_total{RANGE})), 1)", "USD")], unit="currencyUSD"),
    panel(6, "Checkout error rate (shop)", "stat", 21, 0, 3, 5,
          [('sum(rate(traces_span_metrics_calls_total{service_name="checkout",span_kind="SPAN_KIND_SERVER",'
            'status_code="STATUS_CODE_ERROR"}[5m])) / sum(rate(traces_span_metrics_calls_total'
            '{service_name="checkout",span_kind="SPAN_KIND_SERVER"}[5m]))', "checkout")], unit="percentunit"),
    panel(7, "Run duration (alert to end)", "timeseries", 0, 5, 12, 8,
          quantiles("copilot_incident_duration_seconds"), unit="s"),
    panel(8, "Time to recover", "timeseries", 12, 5, 12, 8,
          quantiles("copilot_time_to_recover_seconds"), unit="s",
          description="Runs the runner's independent check saw recover"),
    panel(9, "Tool calls per run", "timeseries", 0, 13, 8, 8, quantiles("copilot_tool_calls_per_incident")),
    panel(10, "Tool calls by role", "timeseries", 8, 13, 8, 8,
          [("sum by (agent_role, kind) (increase(copilot_tool_calls_total[5m]))", "{{agent_role}} {{kind}}")]),
    panel(11, "Calls blocked by hooks", "timeseries", 16, 13, 8, 8,
          [("sum by (reason) (increase(copilot_tool_calls_blocked_total[5m]))", "{{reason}}")]),
    panel(12, "Errors and timeouts", "timeseries", 0, 21, 8, 8,
          [('sum by (status) (increase(copilot_incidents_total{status=~"error|timeout|budget_exhausted"}[15m]))',
            "{{status}}"),
           ('sum by (tool) (increase(copilot_tool_calls_total{outcome="error"}[15m]))', "tool error {{tool}}")]),
    panel(13, "Actions executed", "timeseries", 8, 21, 8, 8,
          [("sum by (tool, purpose) (increase(copilot_actions_total[15m]))", "{{tool}} ({{purpose}})")]),
    panel(14, "LLM spend", "timeseries", 16, 21, 8, 8,
          [("sum by (config_id) (increase(copilot_llm_cost_usd_total[15m]))", "{{config_id}}")], unit="currencyUSD"),
]

dashboard = {
    "uid": "copilot-agent", "title": "On-Call Copilot: agent", "tags": ["copilot"],
    "schemaVersion": 39, "version": 1, "editable": True, "refresh": "15s",
    "time": {"from": "now-3h", "to": "now"}, "panels": panels,
}

if __name__ == "__main__":
    OUT.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {OUT.name} ({len(panels)} panels)")
