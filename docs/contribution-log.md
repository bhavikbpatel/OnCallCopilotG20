# Contribution log

**Why:** the Sprint Backlog asks everyone to keep a contribution log (definition of done, "Everyone"),
and the PR-review checklist (`pr-review/SKILL.md` §4.1) checks that each PR updates it.

**How:** add one row per merged PR (or per piece of work that is not a PR), newest at the bottom,
in the same PR as the work. Say what changed in plain words; link the PR number. Each person writes
their own rows only.

| Date | Person | PR | What |
|---|---|---|---|
| 25 Oct | Person 4 (Bhavik) | #1 | System design document for the On-Call Copilot |
| 31 Oct | Person 4 (Bhavik) | #2 | Evaluation harness: case and result schemas, code checkers with tests, 20 dev cases |
| 31 Oct | Person 4 (Bhavik) | #3 | Fault injector: flag UI client, timed plans, contamination detection, guaranteed clean-up |
| 31 Oct | Person 4 (Bhavik) | #4 | Discovery probe and Prometheus client, verified on the live shop |
| 31 Oct | Person 4 (Bhavik) | #5 | Course-allowed OpenRouter models recorded; judge models not on the list refused |
| 31 Oct | Person 4 (Bhavik) | #7 | Signal map for recovery checks; shop telemetry findings |
| 31 Oct | Person 4 (Bhavik) | #9, #10, #11 | Fault-probe analysis, error-count signal and measured recovery checks for the dev cases (#9 merged, reverted by mistake in #10, re-applied in #11) |
| 31 Oct | Person 4 (Bhavik) | #12 | Evaluation contract aligned with Person 2's incident graph; adapter from incident state to result line |
| 31 Oct | Person 4 (Bhavik) | #13 | PR-review skill: one checklist for every pull request |
| 31 Oct | Person 4 (Bhavik) | #14 | Python 3.9 restriction dropped; recommendationCacheFailure marked unused; .DS_Store no longer committed |
| 31 Oct | Person 4 (Bhavik) | #15 | Independent recovery checker from Prometheus |
| 1 Nov | Person 4 (Bhavik) | #16 | Evaluation runner with synthetic alerts, auto-approval, do-nothing agent, JSONL results and CI gate; this log |
| 1 Nov | Person 4 (Bhavik) | #17 | 300 s re-probe decisions: adHighCpu excluded, traffic bar 2x, longer recovery windows for image and email cases, probe --window option, real re-probe data as test fixture |
| 1 Nov | Person 4 (Bhavik) | #18 | New `slow_requests` signal (requests over 1 s) for faults that slow only some requests; 5-minute-window finding for intlShippingSlowdown |
| 1 Nov | Person 4 (Bhavik) | #19 | 42 held-out cases written (private, outside the repo); split checker that prints counts only; injector stops re-applying a flag once the agent's fix is approved |
| 1 Nov | Person 4 (Bhavik) | #20 | intlShippingSlowdown excluded (no effect in three runs); dev case D-013 replaced |
| 1 Nov | Person 3 (Asiya) | #26 | Tools and control: four MCP servers (`mcp_servers/`: telemetry, runbooks_kb, tickets, ops_actions), approval tokens with a durable single-use store, hooks and guardrails (`copilot/`), permission roles (`config/permissions.yaml`), threat model; laid out as in the System Design after Person 2's review |
| 1 Nov | Person 3 (Asiya) | #26 | Review fixes for the MCP servers PR: `tickets.create_incident` reads the graph's `alert_name` and refuses an alert with no name (was stored as `unknown_alert`); unreadable timestamps refused, unknown alert service opens the incident with a warning; `memory.search_incidents` removed from the permissions (no memory server); failing log sinks counted in `Hooks.costs()`; tickets tests use the real `Alert` shape |
| 1 Nov | Person 4 (Bhavik) | #21 | Dev case severities aligned with Person 1's severity policy, with rule IDs; schema checks rule IDs against the policy |
| 1 Nov | Person 4 (Bhavik) | #23 | Red team for E2: attack format, HTTP delivery, success / influence / false-block checks, runner support; 34 attacks written (private) |
| 1 Nov | Person 4 (Bhavik) | #24 | Support Person 2's follow-up steps and code-driven calls (budget); public signals.clean; probe reports each vector, incl. JSON body |
| 1 Nov | Person 4 (Bhavik) | #30 | LLM judge (status note, evidence support) with anchored rubric and fallback model; postmortem-timeline code check; blind labelling sheet; Cohen's kappa (quadratic-weighted and unweighted); `explanation` field in the result line |
| 1 Nov | Person 4 (Bhavik) | #31 | Preliminary judge calibration: `export --allow-demo` accepts Person 2's demo-graph runs (`DEMO-`), kappa report marks them PRELIMINARY; held-out still refused |
| 1 Nov | Person 4 (Bhavik) | #33 | Judge rubric v4: evidence support scores diagnostic claims only (recovery is checked by code), checks each claim against the item cited for it, and counts an item as backing a claim whatever its hypothesis's status; after evidence kappa -0.18 in the preliminary calibration |
| 1 Nov | Person 4 (Bhavik) | #34 | Follow-ups from the #29 review: verify polls logged as code calls outside the agent's tool-call budget (E4); plain-words Status line in status notes (raw reading stays in the timeline) |
| 1 Nov | Person 4 (Bhavik) | #35 | Telemetry client: a null reading from the telemetry server (NaN, no traffic) is no data instead of a float(None) crash in service_health; found in the live-shop integration run |
| 1 Nov | Person 4 (Bhavik) | #36 | Runbook search: a metric-ID symptom (error_rate) matches the corpus's plain words (error rate), and a search that matches nothing returns no passages instead of a min() crash; found in the live-shop integration run |
| 2 Nov | Person 4 (Bhavik) | #38 | Follow-ups from the review of #37: approval bound to the exact payload the approver saw (`ApprovalDecision.payload_hash`, refused if missing or stale); evidence ids must equal a tool value exactly; the runner's approver names the payload and issues no token; note on when a flag was turned on |
| 2 Nov | Person 4 (Bhavik) | #39 | Runner after #37: `has_token` comes from the agent's own approval record; a gate-using agent does not start without `APPROVAL_SECRET`; agent timeout worked out per case from the graph's verify windows (600 s was too short, #29 review) |
| 2 Nov | Person 4 (Bhavik) | #40 | Experiments E1–E4 as config files (arms, fair comparisons, shared arms, cases, repeats); runner runs them arm by arm with the agent options and writes an experiment log; p95 latency and cost per run in the summary |
| 2 Nov | Person 4 (Bhavik) | #41 | Gaps from the live D-001 run: triage severity from Person 1's classifier with its rule ID (impact-scope mapping proposed for Person 1); US-dollar cost of every model call in the budget ledger and result line; OpenSearch published on 127.0.0.1:9200 so search_logs works |
| 2 Nov | Person 4 (Bhavik) | #42 | Flag UI client retries a brief 502/503/504 or dropped connection (1, 2, 4 s) instead of ending the injection; a single 503 after a shop restart had voided a live D-001 run |
| 2 Nov | Person 3 (Asiya) | #43 | Hooks on the graph's tool calls: `HookedToolbox` and async `Hooks.acall`, role tags on the triage, runbook, dry_run, execute, verify and comms nodes, switched on with `COPILOT_HOOKS=1` (off by default); runbook hash pins (`config/runbook_hashes.yaml`) with a test that fails when the runbooks change without new pins; viva demo scripts for approvals and attacks. Open: a live run with the hooks on, the real actuator, the Diagnosis agent's own tools |
| 4 Nov | Person 4 (Bhavik) | #47 | Runner accepts every registered agent (`--agent copilot` was refused by the run config after #45); found in the first live copilot run |
| 4 Nov | Person 4 (Bhavik) | #48 | Repaired `copilot/hooks.py` after merging #43 into #45/#46: a second copy of `call`/`acall` without `driver` and with an empty `_finish` overrode the working ones, so every hooked call raised TypeError; contribution-log table rejoined |
| 4 Nov | Person 4 (Bhavik) | #49 | Report tables (`eval/report`): every figure the guide asks for per configuration and per fault type and service, time to diagnose and recover, spread across repeats, E1–E4 comparisons from the experiment log |
| 4 Nov | Person 4 (Bhavik) | #50 | `CopilotAgent` runs every case of a batch on one event loop (a new loop per case broke the second case: "Event loop is closed" from langchain-openai's shared client) and stops a case at its deadline; found in the first live dev batch |
| 4 Nov | Person 4 (Bhavik) | this PR | E2 runs with memory off in every arm (measures the defences alone; needs no playbook, no held-out cases, so it can run now); experiment log page (`eval.report --log`) |
