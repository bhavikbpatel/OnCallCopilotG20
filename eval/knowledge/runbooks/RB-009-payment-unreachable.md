---
doc_id: RB-009
title: Checkout cannot reach a healthy payment service
kind: runbook
service: [checkout, payment]
symptom: [payment unreachable, checkout connection refused, downstream timeout]
fault_family: dependency_connectivity
fix: {tool: set_flag, args: {flag: paymentUnreachable, variant: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
Checkout reports payment connection errors while payment's own server error rate may be flat or absent. This differs from processor errors generated inside payment.
## Checks
Inspect checkout client spans for connect/refused/timeout errors and payment server spans for successful traffic. Check deployment/configuration and recent changes; verify `paymentUnreachable` is active before attributing the fault to it.
## Preconditions
Evidence must localize failure to checkout's dependency call and show the flag change. A healthy payment service alone does not prove a specific cause.
## Action
If the matching flag is active, propose `set_flag(flag="paymentUnreachable", variant="off")`; dry-run, approve the exact hash, then execute. Otherwise do not use RB-001's payment-error advice.
## Expected effect
Checkout-to-payment calls connect and checkout errors return to baseline.
## Verification
Require a fresh successful checkout-to-payment trace and falling checkout error rate for five minutes.
## Rollback
If connectivity does not recover, restore the recorded previous flag state via approval and escalate for endpoint/configuration investigation.
