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
| 1 Nov | Person 4 (Bhavik) | #17 | 300 s re-probe decisions: adHighCpu excluded, traffic bar 2x, longer recovery windows for image and email cases, probe --window option, real re-probe data as test f[...]
| 1 Nov | Person 4 (Bhavik) | #18 | New `slow_requests` signal (requests over 1 s) for faults that slow only some requests; 5-minute-window finding for intlShippingSlowdown |
| 1 Nov | Person 4 (Bhavik) | #19 | 42 held-out cases written (private, outside the repo); split checker that prints counts only; injector stops re-applying a flag once the agent's fix is approved |
| 1 Nov | Person 4 (Bhavik) | #20 | intlShippingSlowdown excluded (no effect in three runs); dev case D-013 replaced |
| 1 Nov | Person 3 (Asiya)  | #26 | Tools and control: four MCP servers (`mcp_servers/`: telemetry, runbooks_kb, tickets, ops_actions), approval tokens with a durable single-use store, hooks and guard[...]
| 1 Nov | Person 3 (Asiya)  | #26 | Review fixes for the MCP servers PR: `tickets.create_incident` reads the graph's `alert_name` and refuses an alert with no name (was stored as `unknown_alert`); unr[...]
| 1 Nov | Person 4 (Bhavik) | #21 | Dev case severities aligned with Person 1's severity policy, with rule IDs; schema checks rule IDs against the policy |
| 1 Nov | Person 4 (Bhavik) | #23 | Red team for E2: attack format, HTTP delivery, success / influence / false-block checks, runner support; 34 attacks written (private) |
| 1 Nov | Person 4 (Bhavik) | #24 | Support Person 2's follow-up steps and code-driven calls (budget); public signals.clean; probe reports each vector, incl. JSON body |
| 1 Nov | Person 4 (Bhavik) | #30 | LLM judge (status note, evidence support) with anchored rubric and fallback model; postmortem-timeline code check; blind labelling sheet; Cohen's kappa (quadratic-w[...]
| 1 Nov | Person 4 (Bhavik) | #31 | Preliminary judge calibration: `export --allow-demo` accepts Person 2's demo-graph runs (`DEMO-`), kappa report marks them PRELIMINARY; held-out still refused |
| 1 Nov | Person 4 (Bhavik) | #33 | Judge rubric v4: evidence support scores diagnostic claims only (recovery is checked by code), checks each claim against the item cited for it, and counts an item a[...]
| 1 Nov | Person 4 (Bhavik) | #34 | Follow-ups from the #29 review: verify polls logged as code calls outside the agent's tool-call budget (E4); plain-words Status line in status notes (raw reading st[...]
| 1 Nov | Person 4 (Bhavik) | #35 | Telemetry client: a null reading from the telemetry server (NaN, no traffic) is no data instead of a float(None) crash in service_health; found in the live-shop int[...]
| 1 Nov | Person 4 (Bhavik) | #36 | Runbook search: a metric-ID symptom (error_rate) matches the corpus's plain words (error rate), and a search that matches nothing returns no passages instead of a m[...]
| 2 Nov | Person 4 (Bhavik) | #38 | Follow-ups from the review of #37: approval bound to the exact payload the approver saw (`ApprovalDecision.payload_hash`, refused if missing or stale); evidence ids[...]
| 2 Nov | Person 4 (Bhavik) | #39 | Runner after #37: `has_token` comes from the agent's own approval record; a gate-using agent does not start without `APPROVAL_SECRET`; agent timeout worked out per [...]
| 2 Nov | Person 4 (Bhavik) | #40 | Experiments E1–E4 as config files (arms, fair comparisons, shared arms, cases, repeats); runner runs them arm by arm with the agent options and writes an experime[...]
| 2 Nov | Person 4 (Bhavik) | #41 | Gaps from the live D-001 run: triage severity from Person 1's classifier with its rule ID (impact-scope mapping proposed for Person 1); US-dollar cost of every mode[...]
| 2 Nov | Person 4 (Bhavik) | #42 | Flag UI client retries a brief 502/503/504 or dropped connection (1, 2, 4 s) instead of ending the injection; a single 503 after a shop restart had voided a live D-[...]
| 2 Nov | Person 3 (Asiya)  | #43 | Hooks on the graph's tool calls: `HookedToolbox` and async `Hooks.acall`, role tags on the triage, runbook, dry_run, execute, verify and comms nodes, switched on wi[...]
| 3 Nov | Person 5 (Uppili) | #44 | Deployment and monitoring: `docker compose up` (full) and `compose.lite.yaml` (8 GB laptops) start the pinned shop submodule, the four MCP servers and Alertmanager;[...]
| 4 Nov | Person 4 (Bhavik) | #47 | Runner accepts every registered agent (`--agent copilot` was refused by the run config after #45); found in the first live copilot run |
| 4 Nov | Person 4 (Bhavik) | #48 | Repaired `copilot/hooks.py` after merging #43 into #45/#46: a second copy of `call`/`acall` without `driver` and with an empty `_finish` overrode the working ones, [...]
| 4 Nov | Person 4 (Bhavik) | #49 | Report tables (`eval/report`): every figure the guide asks for per configuration and per fault type and service, time to diagnose and recover, spread across repeats[...]
| 4 Nov | Person 4 (Bhavik) | #50 | `CopilotAgent` runs every case of a batch on one event loop (a new loop per case broke the second case: "Event loop is closed" from langchain-openai's shared client[...]
| 4 Nov | Person 4 (Bhavik) | #51 | E2 runs with memory off in every arm (measures the defences alone; needs no playbook, no held-out cases, so it can run now); experiment log page (`eval.report --log[...]
| 4 Nov | Person 4 (Bhavik) | #52 | Evaluation report draft (`docs/eval-report.md`: golden set, fault injection, code checks, judge, red team, experiments, limits; results filled from `eval.report`); [...]
| 4 Nov | Person 4 (Bhavik) | #53 | Dev rehearsal of E1–E4 (`--rehearse`: every arm once on five dev cases, playbook replaced by an empty store, named `-DEV`, logged as rehearsal and left out of the[...]
| 5 Nov | Person 4 (Bhavik) | #54 | C1 single agent: structured response without `oneOf` (OpenAI refused the schema, so every C1 run failed in the E1 rehearsal), and its model cost counted like the te[...]
| 5 Nov | Person 4 (Bhavik) | #55 | Runner: 3-minute settle after the previous case's clean-up and a baseline that still shows an earlier fault re-measured or refused ("never reached 1.05"); alert wai[...]
| 5 Nov | Person 3 (Asiya)  | #56 | `search_logs` filters on the service's own field (`resource.service.name`, `LOG_SERVICE_FIELD` to change) instead of a free-text match; `scripts/try_servers.py` and[...]
| 6 Nov | Person 4 (Bhavik) | #58 | Follow-ups from the review of #57: the cost of writing each lesson kept with its candidate and totalled in the frozen playbook (E3); the playbook's hash in the expe[...]
| 6 Nov | Person 4 (Bhavik) | #59 | Run cut to fit the shop time (606 runs, about 141 h, to 320, about 75 h): arms can run on a sample of cases picked by code (spread over fault types, fixed seed); C2[...]
| 6 Nov | Person 4 (Bhavik) | #60 | Evaluation report: golden-set counts (dev and held-out, counts only), judge agreement on 24 human labels and what the results rely on, red-team coverage, the shop's[...]
| 6 Nov | Person 5 (Uppili) | #61 | The alert on the agent itself shown firing: `CopilotLoopOrBudget` fired on dev case D-001 with a 5-call budget and Alertmanager routed it to the team (`docs/evidenc[...]
| 6 Nov | Person 5 (Uppili) | #63 | Runner fixes found running dev case D-001 with the real copilot (#62): a correct fix is no longer marked "contaminated" when the injector's drift check runs between[...]
| 7 Nov | Person 3 (Asiya)  | #64 | The actuator (`mcp_servers/actuator`): the process `ACTUATOR_URL` points at, so restart, scale and rollback work in Docker. Four fixed endpoints plus `/health`; che[...]
| 7 Nov | Person 3 (Asiya)  | #65 | Tool-description pins are enforced when the toolbox connects (they were only a library and tests): a tool whose server, name or description is not pinned is refused[...]
| 7 Nov | Person 3 (Asiya)  | #66 | Removed the old opt-in hook path: `HookedToolbox`, `with_role` and the `COPILOT_HOOKS` switch (the routing in `copilot/toolctx.py` replaced them); threat model brou[...]
| 7 Nov | Person 3 (Asiya)  | #67 | `recent_changes` times a flag from when it changed, not from the agent's first question: the telemetry server now reads the flags in the background every 5 s (`FLAG[...]
| 7 Nov | Person 5 (Uppili) | #68 | Tracing with LangSmith (`copilot/tracing.py`): off unless `LANGSMITH_TRACING=true` and a key are set; credentials dropped and every other string redacted before it [...]
| 7 Nov | Person 3 (Asiya)  | #69 | Took over Person 1's open items: read the runbook and postmortem text behind the pins (no changes, pins unchanged), checked the severity policy, classifier and `cop[...]

| 7 Nov | Person 5 (Uppili) | #70 | The console's per-incident hooks checked live after #66: one D-001 incident through `ui.incident.IncidentSession` with the MCP servers rebuilt from `main`; the appr[...]
| 8 Nov | Person 4 (Bhavik) | #72 | E2 results in the evaluation report (section 7) from `eval.report --experiments`: defences off / on on the red-team suite and 10 dev cases, the attacks by goal, the[...]
| 8 Nov | Person 3 (Asiya)  | #73 | Threat model brought up to date, docs only: hard-limit numbering fixed, T3 tests column, T11 metric count, the hooks and flag-timing gaps reworded. No code change |
| 9 Nov | Person 3 (Asiya)  | #76 | Docs only: the actuator needs `COMPOSE_PROJECT_NAME` set to the running stack's project name (found with `docker compose ls`), otherwise it looks in a different pro[...]
| 9 Nov | Person 4 (Bhavik) | this PR | Evaluation report with the results that exist, docs only: a "what was run" table (182 of 320 planned runs), the final system on the 42 held-out cases (106 runs [...]
| 11 Nov | Person 4 (Bhavik) | this PR | Final evaluation results, docs only: E1 (fixed workflow, single agent, team), E3, E4 (memory, budgets, misleading lesson) added to the report with the final-sy[...]
| 12 Nov | Person 4 (Bhavik) | this PR | Re-ran the 20 final-system runs of repeat 3 that failed on HTTP 402 (same code, agent, settings and playbook); report and experiment log updated to 126 of 126 [...]
| 12 Nov | Person 6 (Manasi) | this PR | Added this contribution log entry for Manasi and recorded the update to the project contribution log. |
