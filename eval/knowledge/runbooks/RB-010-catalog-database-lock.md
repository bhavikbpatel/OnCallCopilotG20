---
doc_id: RB-010
title: Product catalog latency from database lock contention
kind: runbook
service: [product-catalog, astronomy-db]
symptom: [catalog lock contention, database wait, catalog p95 latency]
fault_family: database_contention
fix: {tool: set_flag, args: {flag: productCatalogLockContention, variant: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
Product-catalog latency increases and traces spend time waiting on astronomy-db; request errors may be low.
## Checks
Compare catalog p95 with astronomy-db span duration and lock/wait evidence if instrumented. Inspect slow traces, request volume and recent flag changes; compare unrelated services.
## Preconditions
Require traces showing catalog calls blocked on the database and an active `productCatalogLockContention` flag. High catalog latency alone is insufficient.
## Action
Propose `set_flag(flag="productCatalogLockContention", value="off")` only. Dry-run and exact approval are mandatory; do not kill queries or restart the database.
## Expected effect
Database wait time and catalog p95 return toward baseline.
## Verification
Observe at least five minutes of fresh catalog latency and successful database spans.
## Rollback
If contention persists, restore the prior flag using a new approval and escalate with trace evidence.
