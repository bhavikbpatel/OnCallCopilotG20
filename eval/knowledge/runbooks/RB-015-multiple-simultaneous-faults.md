---
doc_id: RB-015
title: Multiple independent service faults in one incident
kind: runbook
service: [payment, image-provider, cart, ad, checkout, frontend]
symptom: [multiple service errors, independent fault signals, partial recovery]
fault_family: concurrent_faults
fix: {tool: set_flag, args: {flag: paymentFailure, value: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
Distinct services show independent errors or latency changes in overlapping windows; fixing one path may leave another alert active.
## Checks
Build a separate evidence chain per service using scoped errors, traces and recent changes. Check dependencies to avoid counting one downstream symptom as a second root cause. Track each hypothesis independently.
## Preconditions
Do not act until every proposed fix names its own supported cause and preconditions. The metadata action is only an example for a proven payment flag failure, never a blanket multi-service fix.
## Action
Create one typed action per supported root cause; dry-run and obtain separate exact-command approval for each. Diagnosis agents remain read-only. Do not restart or scale multiple services as a batch.
## Expected effect
Each action improves only its matching signal; unresolved paths remain open for investigation.
## Verification
Verify each service independently with fresh metrics/traces. Overall recovery requires all user-impacting paths to meet their thresholds.
## Rollback
Rollback only the specific action that failed verification, through the same approval gate; retain successful independent fixes and re-evaluate.
