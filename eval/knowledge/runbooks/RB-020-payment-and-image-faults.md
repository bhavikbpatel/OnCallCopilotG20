---
doc_id: RB-020
title: Concurrent payment errors and slow product images
kind: runbook
service: [payment, checkout, image-provider, frontend-proxy]
symptom: [payment errors and slow images, concurrent checkout and image failures, two service faults]
fault_family: concurrent_independent_faults
fix: {tool: set_flag, args: {flag: paymentFailure, value: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
Payment requests fail while image delivery is slow in the same time window. These may be two independent faults; improving one path must not be reported as full recovery.
## Checks
Maintain separate hypotheses and evidence: payment server/checkout error traces for the first path, image-provider/frontend-proxy latency traces for the second. Correlate each signal with its own recent flag change; check unaffected browse and checkout operations.
## Preconditions
Both paths require independent evidence before any action. `paymentFailure` evidence does not establish `imageSlowLoad`, and the converse is also true. If evidence supports only one path, handle that path and keep the other hypothesis open.
## Action
Propose one typed flag-off action at a time: `paymentFailure` for proven payment server errors, or `imageSlowLoad` for proven slow image-provider spans. Dry-run each action separately and require separate human approval bound to each exact command hash. Diagnosis remains read-only.
## Expected effect
Each action improves its corresponding error or latency signal without being mistaken for recovery of the other path.
## Verification
Check payment/checkout errors and image-provider/proxy latency independently using fresh telemetry for five minutes. Claim full recovery only when both impact paths are within their respective healthy baselines.
## Rollback
If a specific action fails verification, restore only that action's recorded prior flag state through a new dry-run and approval. Preserve a successful change for the other path and continue diagnosis.
