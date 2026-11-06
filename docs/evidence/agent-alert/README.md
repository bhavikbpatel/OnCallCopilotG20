# Evidence: an alert on the agent itself fires (Person 5)

Sprint Backlog, Person 5: "Set up one working alert on the agent itself ... and show it firing."

**Run (6 Nov 2026, new laptop, full stack on the 3.1.0 images).**

```
PYTHONUTF8=1 COPILOT_METRICS_PORT=9464 python -m eval.runner --cases eval/cases/dev --only D-001 \
    --agent copilot --max-tool-calls 5 --approval reject --config-id P5-ALERT
```

The 5-call budget is E4's smallest arm. `--approval reject` means the agent cannot change the shop; the runner
clears the fault after the case. The runner process was kept alive after the case so Prometheus could scrape the
final metrics (the runner records a case when it ends and exits right after).

**What happened.**

| Time (IST) | Event |
| --- | --- |
| 17:31 | Runner starts; Prometheus scrapes `host.docker.internal:9464` |
| 17:31-17:45 | The runner sets `paymentFailure` to 50%, waits for `CheckoutErrorRateHigh`, and hands the incident to the copilot |
| 17:45:14 | Case ends `budget_exhausted`, final status `escalated`: 5 model-driven calls ran, 2 more (parallel diagnosis branches) were blocked by the hooks with `budget_exceeded`; 16 code-driven calls are not budgeted by design. Cost $0.046 |
| 17:46:01 | `CopilotLoopOrBudget` fires (`component="copilot"`, `severity="ticket"`) |
| 17:46:01 | Alertmanager routes it to the `team` receiver, not the incident endpoint |

**Screenshots.** `prometheus-firing.jpg` (the rule firing), `alertmanager-team.jpg` (routed to `team`),
`grafana-agent-dashboard.jpg` (the agent dashboard over the last hour: tool calls per run, cost per run, calls
blocked by hooks).

**Fixes this needed** (`copilot/metrics.py`, `infra/prometheus/rules/agent.yml`):

1. Prometheus's `increase()` ignores a counter's first sample, so a block counter created at 1 on the first block
   never fired the alert. The alert's block reasons are now created at 0 when the metrics start.
2. The alert left out `agent_budget_exceeded`, which `copilot/run.py` counts as a budget block.
3. Running out of budget usually shows up as no blocked call at all: the graph checks the budget before planning
   more and escalates. Two earlier runs of the same case ended this way and the alert stayed silent. The new
   counter `copilot_budget_exhausted_total` counts those runs, and the alert fires on it or on blocked calls.

**Known gap.** Dashboard panels that use `increase()` on labelled counters ("Runs by final status", "Task
success") miss the first run of each label combination for the same reason as fix 1, so they read 0 here.
