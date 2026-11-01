---
doc_id: RB-001
title: Payment processor errors after a flag change
kind: runbook
service: [payment, checkout]
symptom: [payment 5xx, checkout error rate, payment feature flag]
fault_family: feature_flag
fix: {tool: set_flag, args: {flag: paymentFailure, variant: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
Checkout errors rise while payment spans carry error status. Treat this as a payment-path incident only when checkout failures and payment errors overlap in time.
## Checks
Compare service-scoped `traces_span_metrics_calls_total` error rates and request volume for payment and checkout; inspect error traces and recent flag changes. Check whether payment errors began within 15 minutes of `paymentFailure` changing. Compare traffic with baseline so a traffic drop is not mistaken for recovery. A flat payment error rate or no matching change argues against this fix.
## Preconditions
Evidence must show payment errors above baseline, a recent `paymentFailure` change, and checkout user impact. Diagnosis is read-only. Do not infer causation from a flag value alone.
## Action
Propose only `set_flag(flag="paymentFailure", variant="off")`. Run `dry_run`; show current value and expected effect; require human approval bound to the exact action hash before execution.
## Expected effect
Payment and checkout error rates return to their healthy baseline within five minutes; successful traffic continues.
## Verification
Check fresh Prometheus error-rate samples and a new successful checkout trace. Report recovery only after both signals agree.
## Rollback
If error rate does not improve or worsens, stop retries and use the approval gate to restore the prior recorded flag value. Escalate if the prior value is unknown.
