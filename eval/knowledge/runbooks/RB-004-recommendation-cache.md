---
doc_id: RB-004
title: Recommendation cache failure or rising memory
kind: runbook
service: [recommendation]
symptom: [recommendation latency, cache failure, recommendation memory growth]
fault_family: cache_or_memory
fix: {tool: set_flag, args: {flag: recommendationCacheFailure, variant: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
Recommendation latency rises or cache calls fail; memory may climb over successive samples. Check whether product-catalog dependency latency is the actual source.
## Checks
Inspect recommendation p95 latency, memory samples and error traces; compare product-catalog spans and recent `recommendationCacheFailure` changes. Require at least two fresh memory samples before calling a trend.
## Preconditions
Use the flag action only with matching cache errors and an active flag. Restart is a separate, higher-impact action and is not authorized by this runbook; a persistently unhealthy service requires human escalation.
## Action
Propose `set_flag(flag="recommendationCacheFailure", variant="off")` after dry-run and exact-command approval. No automatic restart or scaling.
## Expected effect
Cache errors and recommendation latency decline toward baseline without increasing failures in product-catalog.
## Verification
Check recommendation p95 and error rate for a full five-minute window; confirm new recommendation traces complete.
## Rollback
If the change does not help, restore the previous flag value with a newly approved action; escalate memory growth rather than restarting speculatively.
