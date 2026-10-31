---
doc_id: RB-016
title: Total payment failure during elevated checkout traffic
kind: runbook
service: [payment, checkout]
symptom: [payment 100 percent errors, checkout outage, payment failure under load]
fault_family: payment_failure_high_impact
fix: {tool: set_flag, args: {flag: paymentFailure, variant: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
Nearly all payment attempts fail and checkout errors rise during high traffic. Distinguish a payment processor failure from checkout-to-payment connectivity problems before acting.
## Checks
Compare fresh payment server-span errors with checkout request errors and throughput. Inspect representative failed traces and the test flag-change history. Verify the total error rate and duration across at least two observation windows; compare healthy dependency spans and unaffected browse traffic.
## Preconditions
Use this procedure only when payment server spans themselves fail and the active `paymentFailure` change matches the incident window. If payment server spans are healthy, follow the connectivity investigation in RB-009 instead. Elevated traffic alone is not evidence that the payment flag caused the outage.
## Action
Propose only `set_flag(flag="paymentFailure", value="off")`. Run a dry-run, show the current flag value and expected effect, then pause for human approval of the exact command hash. Do not restart or scale payment as part of this runbook.
## Expected effect
Payment server errors fall and checkout successes recover while elevated but valid traffic continues.
## Verification
Check fresh payment and checkout error-rate samples, plus a successful checkout trace, for five continuous minutes. Confirm metrics are current and traffic remains served.
## Rollback
If errors fail to improve or another service degrades, restore the recorded prior flag value only through a newly dry-run, exact-command approval. Escalate with trace IDs if prior state is unknown.
