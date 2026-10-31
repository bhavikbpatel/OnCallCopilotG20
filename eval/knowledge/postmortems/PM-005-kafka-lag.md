---
doc_id: PM-005
title: Synthetic exercise — Kafka consumer backlog
kind: postmortem
service: [kafka, fraud-detection]
symptom: [consumer lag, queue backlog]
fault_family: queue_backlog
fix: {tool: set_flag, args: {flag: kafkaQueueProblems, variant: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Summary
Synthetic development scenario D-007 tested fraud-detection consumer lag.
## Timeline
- T+0: kafkaQueueProblems test fault enabled.
- T+5m: consumer lag and throughput were inspected by group.
- T+7m: approved flag-off action; lag stopped rising and began draining.
## Root cause
Injected Kafka queue fault supported by lag samples and the matching fixture change.
## Fix
Dry-run and approval preceded the typed flag-off action; no offset reset or queue purge occurred.
## Lessons
- Apply only when lag rises for a known group and the flag change matches. A single lag sample or a traffic burst without consumer errors is not sufficient.
