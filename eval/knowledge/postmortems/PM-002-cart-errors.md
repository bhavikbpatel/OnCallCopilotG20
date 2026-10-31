---
doc_id: PM-002
title: Synthetic exercise — cart and Valkey failures
kind: postmortem
service: [cart, valkey-cart]
symptom: [cart 5xx, Valkey connection errors]
fault_family: feature_flag
fix: {tool: set_flag, args: {flag: cartFailure, variant: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Summary
Synthetic development scenario D-003 exercised cart failures; it is not a real customer incident.
## Timeline
- T+0: cartFailure test variant enabled.
- T+5m: cart errors alerted; traces showed cart-path failures and Valkey symptoms.
- T+7m: approved flag-off action; error-rate recovery checked.
## Root cause
Injected cartFailure. Evidence was the correlated fixture change and cart request errors; downstream Valkey symptoms alone would not prove the cause.
## Fix
Dry-run and approval preceded `set_flag(cartFailure, off)`; cart operations recovered.
## Lessons
- Apply only when cart errors and the matching recent flag change are present. If Valkey fails independently or the flag is unchanged, diagnose that dependency; do not reuse payment advice.
