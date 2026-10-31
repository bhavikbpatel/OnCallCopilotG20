---
doc_id: RB-005
title: Kafka queue problems and fraud consumer lag
kind: runbook
service: [kafka, fraud-detection, accounting]
symptom: [Kafka consumer lag, delayed fraud checks, queue backlog]
fault_family: queue_backlog
fix: {tool: set_flag, args: {flag: kafkaQueueProblems, variant: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
Consumer lag or processing delay grows for fraud-detection or accounting; frontend errors may be absent.
## Checks
Inspect Kafka consumer lag and processing rates by group, error traces, and recent `kafkaQueueProblems` changes. Check whether lag is rising, stable or draining; distinguish an upstream traffic increase from a consumer fault.
## Preconditions
Require a sustained lag trend and matching active flag. A single high lag sample or unknown consumer group is insufficient for remediation.
## Action
Propose `set_flag(flag="kafkaQueueProblems", value="off")` only after dry-run and exact approval. Do not purge queues, replay offsets or scale consumers from this runbook.
## Expected effect
New records process successfully and lag stops growing, then drains toward baseline.
## Verification
Observe fresh lag and consumer throughput for at least five minutes; verify no rise in processing errors.
## Rollback
If lag grows or errors worsen, restore the recorded prior flag state through a newly approved action and escalate to the Kafka owner.
