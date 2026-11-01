---
doc_id: RB-007
title: Email service memory growth
kind: runbook
service: [email]
symptom: [email memory leak, rising RSS, email errors]
fault_family: memory_growth
fix: {tool: set_flag, args: {flag: emailMemoryLeak, variant: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
Email process memory rises over time, possibly followed by email delivery errors; checkout may remain healthy.
## Checks
Inspect `container_memory_usage_total_bytes` or the discovered RSS metric at multiple timestamps, email spans and traffic volume. Check recent `emailMemoryLeak` changes and distinguish growth from a stable traffic-related plateau.
## Preconditions
Require at least two increasing memory samples and evidence of the matching active flag. Do not restart solely because memory is high; capture baseline and error trend first.
## Action
Propose `set_flag(flag="emailMemoryLeak", variant="off")`; require dry-run, exact approval and captured prior value. If memory has not recovered within about 15 minutes after the flag is off, propose `restart_service(service="email")` as a separate action with its own dry-run and human approval.
## Expected effect
Memory growth stops; memory may not immediately fall. Email delivery errors return to baseline.
## Verification
Check memory slope and successful email spans for at least five minutes after the flag change. If memory remains elevated without a recovery trend after about 15 minutes, capture the evidence and seek separate approval for an email-service restart; flag-off alone may stop growth without reclaiming memory.
## Rollback
If errors rise or the trend continues, restore the prior flag value with a newly approved action and page the email owner. Restart only after its own explicit approval; do not cycle the service speculatively.
