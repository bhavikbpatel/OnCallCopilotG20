---
doc_id: PM-001
title: Synthetic exercise — payment flag errors
kind: postmortem
service: [payment, checkout]
symptom: [payment 5xx, checkout error rate]
fault_family: feature_flag
fix: {tool: set_flag, args: {flag: paymentFailure, value: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Summary
Synthetic development scenario D-001: payment failures propagated to checkout. No production event is implied.
## Timeline
- T+0: paymentFailure test fault enabled.
- T+5m: checkout error alert opened; payment error spans corroborated impact.
- T+7m: approved flag-off action executed and recovery independently checked.
## Root cause
The injected paymentFailure flag caused payment errors. Evidence: matching test-fixture change and correlated payment/checkout error traces.
## Fix
The typed `set_flag(paymentFailure, off)` action passed dry-run, exact approval and post-action verification. The scenario recovered; rollback was not needed.
## Lessons
- Reuse this lesson only when payment errors and a paymentFailure change within 15 minutes are both observed; require payment error rate above baseline. Do not apply to checkout connectivity failures or healthy payment spans.
