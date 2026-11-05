---
doc_id: RB-015
title: Multiple independent service faults in one incident
kind: runbook
service: [ad, astronomy-db, cart, checkout, email, fraud-detection, frontend-proxy, image-provider, kafka, payment, product-catalog, valkey-cart]
symptom: [multiple service errors, independent fault signals, partial recovery]
fault_family: concurrent_faults
fix: null
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
Distinct services show independent errors or latency changes in overlapping windows; fixing one path may leave another alert active.
## Checks
Build a separate evidence chain per service using scoped errors, traces and recent changes. Check dependencies to avoid counting one downstream symptom as a second root cause. Track each hypothesis independently.
In this environment, flagd unknown flag lookups and about 35% failing requests are background noise; they are not a root cause without matching service/user-path evidence.
## Preconditions
This generic runbook intentionally has no fixed action. For each confirmed fault family, retrieve its family runbook and validate that runbook's preconditions; never reuse one fault's action for another.
## Action
For each independently confirmed fault, follow its family runbook and propose only its matching typed action (for example, `paymentFailure` in RB-001 or `imageSlowLoad` in RB-008 for D-017). Never disable a flag whose fault has not been independently confirmed. If both faults are confirmed, propose and execute one flag-off action at a time; each needs its own dry-run and human approval bound to the exact action hash. Diagnosis agents remain read-only. Do not restart or scale multiple services as a batch.
## Expected effect
Each action improves only its matching signal; unresolved paths remain open for investigation.
## Verification
Verify each service independently with fresh metrics/traces. Overall recovery requires all user-impacting paths to meet their thresholds.
## Rollback
Rollback only the specific action that failed verification, through the same approval gate; retain successful independent fixes and re-evaluate.
