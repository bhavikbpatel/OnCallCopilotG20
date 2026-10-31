---
doc_id: RB-017
title: High-impact cart errors during a traffic surge
kind: runbook
service: [cart, valkey-cart]
symptom: [cart failure during traffic surge, high cart error rate, shopping cart unavailable]
fault_family: cart_failure_high_impact
fix: {tool: set_flag, args: {flag: cartFailure, value: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
Cart operations fail at an elevated rate while request volume is also high. Checkout impact can be severe, but a traffic increase alone does not establish a cart fault.
## Checks
Compare cart request errors and throughput across aligned windows; inspect cart and Valkey-cart traces to locate the failing side. Check recent `cartFailure` changes and compare unaffected services to distinguish a broad load event from cart-specific failure.
## Preconditions
Require repeatable cart operation errors and a matching active `cartFailure` flag. Establish whether a material share of active shopping sessions is affected. If cart and Valkey signals disagree, keep diagnosing rather than changing the flag.
## Action
Propose `set_flag(flag="cartFailure", value="off")` only after a dry-run reports its current state. Require human approval for the exact command hash; do not drain or reset the cart store.
## Expected effect
Cart errors decrease while request volume remains healthy; dependent checkout requests begin succeeding.
## Verification
Observe fresh cart errors, Valkey dependency spans and checkout success for five minutes. Confirm that improvement is not simply a collapse in traffic.
## Rollback
If the action does not help or the dependency worsens, restore the recorded prior flag value through the approval gate and escalate with the failing trace IDs.
