---
doc_id: RB-012
title: Ad service request errors
kind: runbook
service: [ad, frontend]
symptom: [ad 5xx, ad request failures, frontend ad errors]
fault_family: feature_flag
fix: {tool: set_flag, args: {flag: adFailure, value: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
Ad requests fail; core shopping flows may remain healthy because ads are a non-critical path.
## Checks
Inspect ad server error rate and traces; compare frontend core-operation success and recent `adFailure` flag changes. Measure the affected request share rather than assuming site-wide impact.
## Preconditions
Require ad errors correlated with the active flag. Raise severity only for observed user impact; do not treat every ad failure as checkout failure.
## Action
Propose `set_flag(flag="adFailure", value="off")` after dry-run and exact approval. Do not restart frontend or disable monitoring.
## Expected effect
Ad error rate returns to baseline and core store traffic stays healthy.
## Verification
Check ad errors and frontend core-operation success for five minutes.
## Rollback
If errors remain, restore the captured previous flag with approval and escalate.
