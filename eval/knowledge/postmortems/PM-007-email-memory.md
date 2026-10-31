---
doc_id: PM-007
title: Synthetic exercise — email memory growth
kind: postmortem
service: [email]
symptom: [email RSS growth, memory leak]
fault_family: memory_growth
fix: {tool: set_flag, args: {flag: emailMemoryLeak, value: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Summary
Synthetic development scenario D-009 exercised email memory growth.
## Timeline
- T+0: emailMemoryLeak fixture enabled.
- T+5m: multiple memory samples and email spans confirmed an increasing trend.
- T+7m: approved flag-off action; growth stopped and deliveries were checked.
## Root cause
Injected memory-leak flag, supported by rising email memory and the fixture change.
## Fix
Flag disabled via dry-run and exact approval. A restart was not inferred from memory level alone.
## Lessons
- Reuse only when memory rises across at least two samples and the matching flag is recent. High but stable RSS is not enough.
