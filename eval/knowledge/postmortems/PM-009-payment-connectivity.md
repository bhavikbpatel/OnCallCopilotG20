---
doc_id: PM-009
title: Synthetic exercise — checkout cannot reach payment
kind: postmortem
service: [checkout, payment]
symptom: [payment unreachable, checkout connection failure]
fault_family: dependency_connectivity
fix: {tool: set_flag, args: {flag: paymentUnreachable, value: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Summary
Synthetic development scenario D-011 deliberately tests a misleading payment-error lesson.
## Timeline
- T+0: checkout-to-payment reachability fault enabled; payment service remained healthy.
- T+5m: checkout client spans showed connection errors; payment server errors stayed flat.
- T+7m: approved matching test-flag action; connectivity was verified.
## Root cause
Injected paymentUnreachable affected the caller path, not payment processor health.
## Fix
The exact flag was disabled after dry-run/approval; no generic paymentFailure action was used.
## Lessons
- Reject advice to disable paymentFailure unless payment itself has errors and its flag changed recently. For this lesson, require checkout client connection errors, healthy payment server spans and paymentUnreachable evidence.
