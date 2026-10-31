---
doc_id: RB-002
title: Cart failures and Valkey dependency errors
kind: runbook
service: [cart, valkey-cart]
symptom: [cart 5xx, cart read or write failure, Valkey connection error]
fault_family: feature_flag
fix: {tool: set_flag, args: {flag: cartFailure, variant: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
Cart operations fail or time out; cart and possibly Valkey-cart spans show errors. Cart-only impact is different from checkout/payment failure.
## Checks
Compare cart server-span error rate and latency with Valkey-cart dependency spans. Inspect traces for connection/reset errors and recent `cartFailure` flag changes; compare unaffected services and baseline traffic.
## Preconditions
Use this action only when cart errors correlate with an active `cartFailure` flag. If only Valkey-cart errors exist without the flag change, gather dependency evidence and escalate; do not claim the flag is causal.
## Action
Propose `set_flag(flag="cartFailure", value="off")` only. Require a dry run, exact-command human approval and the recorded previous flag value; diagnosis remains read-only.
## Expected effect
Cart error rate falls to its baseline and new cart operations succeed within five minutes.
## Verification
Check fresh cart error-rate samples and a successful cart trace. Confirm checkout impact separately.
## Rollback
If no improvement, restore the previous flag value through the same gate and escalate with the failing dependency trace IDs.
