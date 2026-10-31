---
doc_id: RB-014
title: Intermittent feature-flag failures
kind: runbook
service: [payment, cart, product-catalog, ad]
symptom: [intermittent 5xx, periodic failures, alternating healthy and failing windows]
fault_family: intermittent_fault
fix: null
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
Errors alternate with healthy periods; one short query can miss the failure or mistake normal variance for recovery.
## Checks
Use aligned time windows for error rate, traces and flag-change history. Compare each failing window with a healthy window; identify the exact service and flag rather than reusing the example payment flag.
## Preconditions
Require repeated correlated failure windows and an identified service, flag and fault family. This generic runbook intentionally has no fixed action: retrieve the matching service-specific runbook and validate its preconditions before proposing any write.
## Action
Do not execute an action from this generic runbook. Once the fault is identified, use its service-specific runbook, dry-run its exact typed action, capture current state and obtain human approval bound to its hash. Never toggle repeatedly to chase an intermittent symptom.
## Expected effect
No new failure window occurs for at least one full observed interval; error-rate baseline is stable.
## Verification
Observe a full failing/healthy cycle length using fresh metrics and traces; record the window and signal freshness.
## Rollback
If the failure persists or another service degrades, restore prior state with a newly approved action and escalate.
