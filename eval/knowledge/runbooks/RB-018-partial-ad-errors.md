---
doc_id: RB-018
title: Partial ad request failures with healthy shopping flows
kind: runbook
service: [ad, frontend]
symptom: [intermittent ad 5xx, partial ad failures, frontend ad errors]
fault_family: partial_feature_failure
fix: {tool: set_flag, args: {flag: adFailure, value: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
A subset of ad requests fails while frontend browse, cart and checkout operations remain successful. An ad-specific alert does not imply a store-wide outage.
## Checks
Measure ad errors over total ad requests and compare with frontend core-operation errors. Inspect ad traces and recent `adFailure` flag changes; distinguish intermittent failures from low-volume statistical noise.
## Preconditions
Require a sustained, reproducible ad error signal and a matching active flag. Scope severity to observed user impact; do not infer checkout impact from the ad error percentage.
## Action
Propose `set_flag(flag="adFailure", value="off")` after dry-run and human approval bound to the exact command. Do not disable alerting or restart frontend.
## Expected effect
Ad request errors return toward baseline without changing core frontend availability.
## Verification
Check new ad traces and core frontend success for five minutes. Ensure error rates are computed with a non-zero, representative request denominator.
## Rollback
If ad errors persist or the core path degrades, restore the captured previous flag state through a newly approved action and escalate.
