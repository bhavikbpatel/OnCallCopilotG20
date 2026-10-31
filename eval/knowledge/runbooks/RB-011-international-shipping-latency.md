---
doc_id: RB-011
title: International shipping quote slowdown
kind: runbook
service: [shipping, checkout]
symptom: [international shipping latency, slow shipping quote, checkout shipping delay]
fault_family: dependency_latency
fix: {tool: set_flag, args: {flag: intlShippingSlowdown, value: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
International shipping quotes are slow while other checkout steps can succeed; impact is regional or route-specific.
## Checks
Inspect shipping p95 and error traces by route/country if available. Compare checkout spans and recent `intlShippingSlowdown` changes. Distinguish shipping delay from a broad checkout/payment failure.
## Preconditions
Require shipping-path latency and matching flag evidence; confirm actual user impact and duration before setting incident severity.
## Action
Propose `set_flag(flag="intlShippingSlowdown", value="off")` through dry-run and exact-command approval.
## Expected effect
International quote latency falls without changing domestic shipping behavior.
## Verification
Check fresh international shipping traces and p95 for five minutes; confirm checkout success.
## Rollback
If there is no improvement, restore the recorded flag state after approval and escalate with route-specific evidence.
