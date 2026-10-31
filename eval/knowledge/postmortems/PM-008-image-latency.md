---
doc_id: PM-008
title: Synthetic exercise — product image latency
kind: postmortem
service: [image-provider, frontend-proxy]
symptom: [slow image load, upstream timeout]
fault_family: dependency_latency
fix: {tool: set_flag, args: {flag: imageSlowLoad, value: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Summary
Synthetic development scenario D-010 tested slow image delivery through the frontend proxy.
## Timeline
- T+0: imageSlowLoad fixture enabled.
- T+5m: image-provider and proxy spans showed the slow path.
- T+7m: approved flag-off action; image traces and p95 were rechecked.
## Root cause
Injected image-provider latency, distinct from a core product API failure.
## Fix
Flag disabled only after dry-run and exact approval; the core frontend was not restarted.
## Lessons
- Apply when the image path alone is slow and imageSlowLoad matches the time window. Healthy image-provider spans with slow proxy spans point elsewhere.
