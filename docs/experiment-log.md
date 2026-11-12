# Experiment log and tables

Generated on 11 Nov 2026 (E1-C2 updated 12 Nov) by `python -m eval.report --log` and `python -m eval.report --experiments` (counts only; no
held-out case content). The raw run files stay local (`eval/results/` is not committed).

**How to read the log.**
- Rows marked "rehearsal" are dev runs that only show an arm runs; they are not results.
- Stopped batches are kept and not used: the first attacks-on, clean-off and clean-on attempts of 6 Nov (HTTP 402,
  the model key reached its spending limit; run again in full on 7 Nov with the same settings) and the first
  memory-off attempt of 8 Nov (28 runs, all HTTP 402; run again on 9 Nov).
- The final-system batch `E1-C2-20261107T185424Z` has 126 runs: 20 in its third repeat failed on HTTP 402 when the
  model credit ran out. Those 20 cases were run again on 11 Nov (`E1-C2-R3FILL-20261111T131331Z`: same code `873c846`,
  agent, settings and frozen playbook, one run each). That batch is marked partial in the log, so the tool's own
  "latest complete batch" rule does not pick it; the E1 tables below combine the two batches (`docs/eval-report.md`, 8.1).
- The tables below use the latest complete batch of each arm. Sampled arms are compared only on shared cases.

## Experiment log

| Started (UTC) | Exp | Arm | Batch | Agent | Code | Config hash | Playbook | Runs | Task success | Status | Cost/run ($) | Partial |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-05 02:44 | E1 | C0 | E1-C0-DEV-20261105T024407Z | copilot@53edd4428fe1 | b667c61 | dec2b0a455463aff | – | 5 | 0.6 | completed 5 | 0.007 | rehearsal, no |
| 2026-11-05 03:30 | E1 | C1 | E1-C1-DEV-20261105T033017Z | copilot@53edd4428fe1 | b667c61 | ba62b9f2ea244ea5 | – | 5 | 0 | completed 0, error 5 | 0 | rehearsal, no |
| 2026-11-05 03:56 | E1 | C2 | E1-C2-DEV-20261105T035654Z | copilot@53edd4428fe1 | b667c61 | 265f05bb370db591 | – | 5 | 0.4 | completed 5 | 0.031 | rehearsal, no |
| 2026-11-05 04:57 | E4 | memory-off | E4-memory-off-DEV-20261105T045742Z | copilot@53edd4428fe1 | b667c61 | 223d5cf78b500b51 | – | 5 | 0.8 | completed 5 | 0.023 | rehearsal, no |
| 2026-11-05 05:49 | E4 | budget-10 | E4-budget-10-DEV-20261105T054915Z | copilot@53edd4428fe1 | b667c61 | e60b0925681379e8 | – | 5 | 0.4 | completed 5 | 0.03 | rehearsal, no |
| 2026-11-05 06:40 | E4 | budget-5 | E4-budget-5-DEV-20261105T064036Z | copilot@53edd4428fe1 | b667c61 | 2cbf4fd440c2f357 | – | 5 | 0.4 | completed 4, budget_exhausted 1 | 0.027 | rehearsal, no |
| 2026-11-05 07:29 | E4 | misleading-on | E4-misleading-on-DEV-20261105T072925Z | copilot@53edd4428fe1 | b667c61 | 3cc3bb06a4ca96cf | – | 1 | 0 | completed 1 | 0.037 | rehearsal, no |
| 2026-11-05 07:41 | E4 | misleading-off | E4-misleading-off-DEV-20261105T074135Z | copilot@53edd4428fe1 | b667c61 | 5aaced861787bb24 | – | 1 | 0 | completed 0, error 1 | 0 | rehearsal, no |
| 2026-11-05 07:49 | E2 | attacks-off | E2-attacks-off-DEV-20261105T074939Z | copilot@53edd4428fe1 | 08ba5e9 | 4824818973c55173 | – | 17 | 0.235 | completed 12, error 5 | 0.02 | rehearsal, no |
| 2026-11-05 10:26 | E2 | attacks-on | E2-attacks-on-DEV-20261105T102612Z | copilot@53edd4428fe1 | 08ba5e9 | 72d6bac63fda07d7 | – | 17 | 0.529 | completed 13, error 4 | 0.02 | rehearsal, no |
| 2026-11-05 13:04 | E2 | clean-off | E2-clean-off-DEV-20261105T130444Z | copilot@53edd4428fe1 | 08ba5e9 | 637b71cb380b46ac | – | 5 | 0.4 | completed 5 | 0.031 | rehearsal, no |
| 2026-11-05 13:57 | E2 | clean-on | E2-clean-on-DEV-20261105T135745Z | copilot@53edd4428fe1 | 08ba5e9 | 223d5cf78b500b51 | – | 5 | 0.4 | completed 5 | 0.033 | rehearsal, no |
| 2026-11-05 17:12 | E1 | C1 | E1-C1-DEV-20261105T171238Z | copilot@53edd4428fe1 | 891c9a6 | ba62b9f2ea244ea5 | – | 5 | 0.4 | completed 4, error 1 | 0.014 | rehearsal, no |
| 2026-11-06 01:45 | E1 | C1 | E1-C1-DEV-20261106T014500Z | copilot@c7c9256dd320 | 4bdc87c | ba62b9f2ea244ea5 | – | 5 | 0.8 | completed 5 | 0.02 | rehearsal, no |
| 2026-11-06 11:07 | E2 | attacks-off | E2-attacks-off-20261106T110736Z | copilot@c7c9256dd320 | 6769bc5 | ad9ed6dccc3b0a71 | – | 34 | 0.529 | completed 25, error 9 | 0.02 | no |
| 2026-11-06 17:41 | E2 | attacks-on | E2-attacks-on-20261106T174120Z | copilot@c7c9256dd320 | 6769bc5 | ad98f09cbd18c4bf | – | 34 | 0.206 | completed 9, error 25 | 0.007 | no |
| 2026-11-06 22:14 | E2 | clean-off | E2-clean-off-20261106T221400Z | copilot@c7c9256dd320 | 6769bc5 | a5f136bdad0a842a | – | 10 | 0 | completed 0, error 10 | 0 | no |
| 2026-11-06 23:26 | E2 | clean-on | E2-clean-on-20261106T232624Z | copilot@c7c9256dd320 | 6769bc5 | 68823bc3b6368b2e | – | 10 | 0 | completed 0, error 10 | 0 | no |
| 2026-11-07 06:40 | E2 | attacks-on | E2-attacks-on-20261107T064055Z | copilot@c7c9256dd320 | 6769bc5 | ad98f09cbd18c4bf | – | 34 | 0.588 | completed 27, error 7 | 0.021 | no |
| 2026-11-07 13:07 | E2 | clean-off | E2-clean-off-20261107T130758Z | copilot@c7c9256dd320 | 6769bc5 | a5f136bdad0a842a | – | 10 | 0.6 | completed 10 | 0.032 | no |
| 2026-11-07 15:32 | E2 | clean-on | E2-clean-on-20261107T153238Z | copilot@c7c9256dd320 | 6769bc5 | 68823bc3b6368b2e | – | 10 | 0.7 | completed 10 | 0.035 | no |
| 2026-11-07 18:54 | E1 | C2 | E1-C2-20261107T185424Z | copilot@c7c9256dd320 | 873c846 | c8064110a4ca448d | 0c05d04cf34a | 126 | 0.516 | completed 100, budget_exhausted 1, error 25 | 0.025 | no |
| 2026-11-08 23:03 | E4 | memory-off | E4-memory-off-20261108T230354Z | copilot@c7c9256dd320 | 873c846 | 924c687e4064b269 | – | 28 | 0 | error 28 | 0 | partial, stopped (HTTP 402) |
| 2026-11-09 13:13 | E4 | memory-off | E4-memory-off-20261109T131357Z | copilot@c7c9256dd320 | 3d927dc | 924c687e4064b269 | – | 42 | 0.571 | completed 41, error 1 | 0.033 | no |
| 2026-11-09 23:56 | E4 | misleading-on | E4-misleading-on-20261109T235658Z | copilot@c7c9256dd320 | 3d927dc | a56dafa0fc7b4e27 | 0c05d04cf34a | 3 | 0 | completed 3 | 0.04 | no |
| 2026-11-10 00:41 | E4 | misleading-off | E4-misleading-off-20261110T004141Z | copilot@c7c9256dd320 | 3d927dc | b334dd7756e1bd4c | – | 3 | 0 | completed 3 | 0.027 | no |
| 2026-11-10 01:24 | E4 | budget-10 | E4-budget-10-20261110T012458Z | copilot@c7c9256dd320 | 3d927dc | f5ce1c4f9d9c0359 | 0c05d04cf34a | 14 | 0.714 | completed 13, budget_exhausted 1 | 0.032 | no |
| 2026-11-10 04:34 | E4 | budget-5 | E4-budget-5-20261110T043433Z | copilot@c7c9256dd320 | 3d927dc | 7883505a514993ed | 0c05d04cf34a | 14 | 0.5 | completed 11, budget_exhausted 3 | 0.022 | no |
| 2026-11-10 08:10 | E1 | C0 | E1-C0-20261110T081038Z | copilot@c7c9256dd320 | 3d927dc | ec040c7ec1f6c920 | 0c05d04cf34a | 21 | 0.429 | completed 21 | 0.007 | no |
| 2026-11-10 12:33 | E1 | C1 | E1-C1-20261110T123311Z | copilot@c7c9256dd320 | 3d927dc | 38a09d48ad06f74a | 0c05d04cf34a | 21 | 0.714 | completed 20, error 1 | 0.02 | no |
| 2026-11-11 13:13 | E1 | C2 | E1-C2-R3FILL-20261111T131331Z | copilot@c7c9256dd320 | 873c846 | c8064110a4ca448d | 0c05d04cf34a | 20 | 0.6 | completed 20 | 0.03 | partial (re-run of the 20 HTTP 402 runs of repeat 3) |

Models (config/app.yaml): commander: openai/gpt-6-luna; comms: openai/gpt-5-mini; diagnosis: openai/gpt-5-mini; reflexion: openai/gpt-6-luna; remediation: openai/gpt-5-mini; runbook: openai/gpt-5-mini; single_agent: openai/gpt-5-mini; triage: openai/gpt-5-mini.

## Experiments

### E1: Does the team of agents justify its cost compared with a fixed workflow and a single agent?

C2 here is the 9 Nov batch with its 20 HTTP 402 runs replaced by the re-run of 11 Nov (`E1-C2-R3FILL-20261111T131331Z`, same code, agent and settings; computed with eval.report's own functions by a local script, `combine_c2.py`, kept outside the repository).

| Arm | Runs | Task success | Rate | Policy violations | Attack success | False blocks | Cost/case ($) | Median latency (s) | p95 latency (s) | Errors | Root cause top-1 | Top-3 | Correct action | Verified recovery | Harmful | Unnecessary | Time to diagnose (s, median) | Time to recover (s, median) | Tool calls (median) | Rollbacks |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| C0 | 21 | 9 | 0.429 | 0 | – | 0 | 0.007 | 761.8 | 938.2 | 0 | 0.5 | 0.5 | 0.5 | 0.526 | 3 | 0 | – | 228.1 | 0 | 0 |
| C1 | 21 | 15 | 0.714 | 0 | – | 0 | 0.02 | 807.5 | 978.1 | error 1 | 0.8 | 0.75 | 0.85 | 1 | 0 | 0 | – | 371.6 | 6 | 0 |
| C2 | 126 | 77 | 0.611 | 0 | – | 0 | 0.03 | 844.8 | 1159.0 | budget_exhausted 1, error 5 | 0.641 | 0.59 | 0.692 | 0.862 | 2 | 0 | 79.0 | 394.2 | 5 | 0 |

Compared: C0, C1, C2 (varies architecture).

C0, C1, C2 on the 21 case(s) every one of them ran (sampled arms; all repeats kept, so an arm with more repeats has more runs):

| Arm | Runs | Task success | Rate | Policy violations | Attack success | False blocks | Cost/case ($) | Median latency (s) | p95 latency (s) | Errors | Root cause top-1 | Top-3 | Correct action | Verified recovery | Harmful | Unnecessary | Time to diagnose (s, median) | Time to recover (s, median) | Tool calls (median) | Rollbacks |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| C0 | 21 | 9 | 0.429 | 0 | – | 0 | 0.007 | 761.8 | 938.2 | 0 | 0.5 | 0.5 | 0.5 | 0.526 | 3 | 0 | – | 228.1 | 0 | 0 |
| C1 | 21 | 15 | 0.714 | 0 | – | 0 | 0.02 | 807.5 | 978.1 | error 1 | 0.8 | 0.75 | 0.85 | 1 | 0 | 0 | – | 371.6 | 6 | 0 |
| C2 | 63 | 36 | 0.571 | 0 | – | 0 | 0.032 | 858.6 | 1135.8 | budget_exhausted 1, error 2 | 0.617 | 0.567 | 0.717 | 0.929 | 0 | 0 | 84.2 | 400.5 | 7 | 0 |

### E2: Do the defences stop attacks without blocking legitimate work?

| Arm | Runs | Task success | Rate | Policy violations | Attack success | False blocks | Cost/case ($) | Median latency (s) | p95 latency (s) | Errors | Root cause top-1 | Top-3 | Correct action | Verified recovery | Harmful | Unnecessary | Time to diagnose (s, median) | Time to recover (s, median) | Tool calls (median) | Rollbacks |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| attacks-off | 34 | 18 | 0.529 | 0 | 0 | 0 | 0.02 | 803.0 | 1056.8 | error 9 | 0.656 | 0.656 | 0.719 | 0.84 | 0 | 0 | 77.9 | 396.7 | 3 | 0 |
| attacks-on | 34 | 20 | 0.588 | 0 | 0 | 0 | 0.021 | 802.0 | 1021.4 | error 7 | 0.688 | 0.688 | 0.75 | 0.833 | 0 | 0 | 66.9 | 378.4 | 3 | 0 |
| clean-off | 10 | 6 | 0.6 | 0 | – | 0 | 0.032 | 864.4 | 1043.5 | 0 | 0.667 | 0.556 | 0.667 | 0.75 | 0 | 0 | 66.3 | 453.8 | 4.5 | 0 |
| clean-on | 10 | 7 | 0.7 | 0 | – | 0 | 0.035 | 985.8 | 1219.0 | 0 | 0.778 | 0.667 | 0.667 | 0.778 | 0 | 0 | 68.7 | 383.3 | 8.5 | 0 |

Compared: attacks-off, attacks-on (varies defences).
Compared: clean-off, clean-on (varies defences).

### E3: Did the playbook of lessons improve held-out results, and did any lesson make them worse?

| Arm | Runs | Task success | Rate | Policy violations | Attack success | False blocks | Cost/case ($) | Median latency (s) | p95 latency (s) | Errors | Root cause top-1 | Top-3 | Correct action | Verified recovery | Harmful | Unnecessary | Time to diagnose (s, median) | Time to recover (s, median) | Tool calls (median) | Rollbacks |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| before (= E4/memory-off) | 42 | 24 | 0.571 | 0 | – | 0 | 0.033 | 891.8 | 1259.9 | error 1 | 0.615 | 0.59 | 0.59 | 0.676 | 2 | 1 | 84.4 | 390.2 | 7 | 0 |
| after (= E1/C2) | 42 | 27 | 0.643 | 0 | – | 0 | 0.03 | 847.9 | 1193.9 | budget_exhausted 1, error 1 | 0.667 | 0.615 | 0.744 | 0.868 | 1 | 0 | 71.7 | 387.3 | 5 | 0 |

Compared: before, after (varies memory).

Lessons in after (against before): 3 run(s) failed on a case that succeeded in before; 0 of them used a lesson.

| Lesson | Runs used | Task success | Runs rejected (preconditions) | Used in worse runs |
|---|---|---|---|---|
| L-D-001-001 | 5 | 4 | 5 | 0 |
| L-D-004-003 | 4 | 3 | 1 | 0 |
| L-D-006-005 | 2 | 2 | 0 | 0 |
| L-D-009-007 | 3 | 3 | 0 | 0 |
| L-D-011-008 | 4 | 0 | 6 | 0 |
| L-D-012-017 | 3 | 2 | 6 | 0 |
| L-D-013-010 | 5 | 4 | 4 | 0 |
| L-D-014-011 | 3 | 2 | 1 | 0 |
| L-D-019-016 | 5 | 4 | 5 | 0 |
| PM-001 | 0 | 0 | 13 | 0 |
| PM-004 | 0 | 0 | 2 | 0 |

### E4: Does memory help without applying wrong lessons, and how does diagnosis change with the budget?

| Arm | Runs | Task success | Rate | Policy violations | Attack success | False blocks | Cost/case ($) | Median latency (s) | p95 latency (s) | Errors | Root cause top-1 | Top-3 | Correct action | Verified recovery | Harmful | Unnecessary | Time to diagnose (s, median) | Time to recover (s, median) | Tool calls (median) | Rollbacks |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| memory-on (= E1/C2) | 42 | 27 | 0.643 | 0 | – | 0 | 0.03 | 847.9 | 1193.9 | budget_exhausted 1, error 1 | 0.667 | 0.615 | 0.744 | 0.868 | 1 | 0 | 71.7 | 387.3 | 5 | 0 |
| memory-off | 42 | 24 | 0.571 | 0 | – | 0 | 0.033 | 891.8 | 1259.9 | error 1 | 0.615 | 0.59 | 0.59 | 0.676 | 2 | 1 | 84.4 | 390.2 | 7 | 0 |
| budget-10 | 14 | 10 | 0.714 | 0 | – | 1 | 0.032 | 839.8 | 997.3 | budget_exhausted 1 | 0.692 | 0.692 | 0.769 | 1 | 0 | 0 | 75.4 | 394.0 | 6 | 0 |
| budget-5 | 14 | 7 | 0.5 | 0 | – | 0 | 0.022 | 932.0 | 1177.1 | budget_exhausted 3 | 0.615 | 0.615 | 0.692 | 0.727 | 0 | 0 | 69.8 | 388.5 | 3 | 0 |
| misleading-on | 3 | 0 | 0 | 0 | – | 0 | 0.04 | 882.0 | 950.8 | 0 | 0 | 0 | 0.667 | 0.667 | 0 | 0 | 185.5 | 520.5 | 7 | 0 |
| misleading-off | 3 | 0 | 0 | 0 | – | 0 | 0.027 | 832.0 | 948.5 | 0 | 0 | 0 | 1 | 1 | 0 | 0 | 74.5 | 411.1 | 6 | 0 |

Compared: memory-on, memory-off (varies memory).
Compared: memory-on, budget-10, budget-5 (varies max_tool_calls).
Compared: misleading-on, misleading-off (varies memory).

memory-on, budget-10, budget-5 on the 14 case(s) every one of them ran (sampled arms; all repeats kept, so an arm with more repeats has more runs):

| Arm | Runs | Task success | Rate | Policy violations | Attack success | False blocks | Cost/case ($) | Median latency (s) | p95 latency (s) | Errors | Root cause top-1 | Top-3 | Correct action | Verified recovery | Harmful | Unnecessary | Time to diagnose (s, median) | Time to recover (s, median) | Tool calls (median) | Rollbacks |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| memory-on | 14 | 8 | 0.571 | 0 | – | 0 | 0.033 | 803.8 | 1049.3 | budget_exhausted 1, error 1 | 0.538 | 0.538 | 0.769 | 1 | 0 | 0 | 75.8 | 398.4 | 5 | 0 |
| budget-10 | 14 | 10 | 0.714 | 0 | – | 1 | 0.032 | 839.8 | 997.3 | budget_exhausted 1 | 0.692 | 0.692 | 0.769 | 1 | 0 | 0 | 75.4 | 394.0 | 6 | 0 |
| budget-5 | 14 | 7 | 0.5 | 0 | – | 0 | 0.022 | 932.0 | 1177.1 | budget_exhausted 3 | 0.615 | 0.615 | 0.692 | 0.727 | 0 | 0 | 69.8 | 388.5 | 3 | 0 |

Lessons in memory-on (against memory-off): 3 run(s) failed on a case that succeeded in memory-off; 0 of them used a lesson.

| Lesson | Runs used | Task success | Runs rejected (preconditions) | Used in worse runs |
|---|---|---|---|---|
| L-D-001-001 | 5 | 4 | 5 | 0 |
| L-D-004-003 | 4 | 3 | 1 | 0 |
| L-D-006-005 | 2 | 2 | 0 | 0 |
| L-D-009-007 | 3 | 3 | 0 | 0 |
| L-D-011-008 | 4 | 0 | 6 | 0 |
| L-D-012-017 | 3 | 2 | 6 | 0 |
| L-D-013-010 | 5 | 4 | 4 | 0 |
| L-D-014-011 | 3 | 2 | 1 | 0 |
| L-D-019-016 | 5 | 4 | 5 | 0 |
| PM-001 | 0 | 0 | 13 | 0 |
| PM-004 | 0 | 0 | 2 | 0 |

Lessons in misleading-on (against misleading-off): 0 run(s) failed on a case that succeeded in misleading-off; 0 of them used a lesson.

| Lesson | Runs used | Task success | Runs rejected (preconditions) | Used in worse runs |
|---|---|---|---|---|
| L-D-001-001 | 0 | 0 | 2 | 0 |
| L-D-011-008 | 2 | 0 | 0 | 0 |
| L-D-019-016 | 0 | 0 | 2 | 0 |
| PM-001 | 0 | 0 | 2 | 0 |
