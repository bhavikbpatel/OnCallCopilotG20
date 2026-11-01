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
| 1 Nov | Person 3 (Asiya) | this PR | Tools and control: four MCP servers (`mcp_servers/`: telemetry, runbooks_kb, tickets, ops_actions), approval tokens with a durable single-use store, hooks and guardrails (`copilot/`), permission roles (`config/permissions.yaml`), threat model; laid out as in the System Design after Person 2's review |
| 1 Nov | Person 4 (Bhavik) | #21 | Dev case severities aligned with Person 1's severity policy, with rule IDs; schema checks rule IDs against the policy |
| 1 Nov | Person 4 (Bhavik) | #23 | Red team for E2: attack format, HTTP delivery, success / influence / false-block checks, runner support; 34 attacks written (private) |
| 1 Nov | Person 4 (Bhavik) | #24 | Support Person 2's follow-up steps and code-driven calls (budget); public signals.clean; probe reports each vector, incl. JSON body |
| 1 Nov | Person 4 (Bhavik) | #30 | LLM judge (status note, evidence support) with anchored rubric and fallback model; postmortem-timeline code check; blind labelling sheet; Cohen's kappa (quadratic-weighted and unweighted); `explanation` field in the result line |
| 1 Nov | Person 4 (Bhavik) | #31 | Preliminary judge calibration: `export --allow-demo` accepts Person 2's demo-graph runs (`DEMO-`), kappa report marks them PRELIMINARY; held-out still refused |
| 1 Nov | Person 4 (Bhavik) | #33 | Judge rubric v4: evidence support scores diagnostic claims only (recovery is checked by code), checks each claim against the item cited for it, and counts an item as backing a claim whatever its hypothesis's status; after evidence kappa -0.18 in the preliminary calibration |
| 1 Nov | Person 4 (Bhavik) | #34 | Follow-ups from the #29 review: verify polls logged as code calls outside the agent's tool-call budget (E4); plain-words Status line in status notes (raw reading stays in the timeline) |
| 1 Nov | Person 4 (Bhavik) | #35 | Telemetry client: a null reading from the telemetry server (NaN, no traffic) is no data instead of a float(None) crash in service_health; found in the live-shop integration run |
| 1 Nov | Person 4 (Bhavik) | this PR | Runbook search: a metric-ID symptom (error_rate) matches the corpus's plain words (error rate), and a search that matches nothing returns no passages instead of a min() crash; found in the live-shop integration run |
