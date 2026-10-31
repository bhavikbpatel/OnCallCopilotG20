---
doc_id: PM-006
title: Synthetic exercise — ad CPU saturation
kind: postmortem
service: [ad, frontend]
symptom: [ad CPU high, ad latency]
fault_family: resource_saturation
fix: {tool: set_flag, args: {flag: adHighCpu, variant: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Summary
Synthetic development scenario D-008 covered ad-service CPU saturation.
## Timeline
- T+0: adHighCpu fixture enabled.
- T+5m: CPU, ad latency and frontend success were compared.
- T+7m: approved flag-off action; metrics returned toward baseline.
## Root cause
Injected adHighCpu, supported by sustained ad CPU and correlated latency.
## Fix
The flag was disabled after dry-run and approval; no speculative scaling occurred.
## Lessons
- Use only with multiple elevated CPU samples and the matching flag. Traffic-driven saturation requires a separate capacity review.
