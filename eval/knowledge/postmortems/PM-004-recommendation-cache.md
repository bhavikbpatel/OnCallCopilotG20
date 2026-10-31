---
doc_id: PM-004
title: Synthetic exercise — recommendation cache latency
kind: postmortem
service: [recommendation]
symptom: [recommendation latency, cache error]
fault_family: cache_or_memory
fix: {tool: set_flag, args: {flag: recommendationCacheFailure, value: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Summary
Synthetic development scenario D-006 exercised recommendation-cache symptoms.
## Timeline
- T+0: cache test fault enabled.
- T+5m: recommendation p95 rose; telemetry checked product-catalog as an alternative.
- T+7m: approved flag-off action; recommendation latency and traces verified.
## Root cause
Injected recommendationCacheFailure; the test change aligned with recommendation latency/cache evidence.
## Fix
Flag disabled through dry-run and exact approval. No restart was authorized or needed.
## Lessons
- Reuse only with cache-path evidence and a matching active flag. If product-catalog latency leads recommendation latency, investigate that dependency first.
