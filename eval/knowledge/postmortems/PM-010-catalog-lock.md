---
doc_id: PM-010
title: Synthetic exercise — catalog database contention
kind: postmortem
service: [product-catalog, astronomy-db]
symptom: [database lock wait, catalog latency]
fault_family: database_contention
fix: {tool: set_flag, args: {flag: productCatalogLockContention, value: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Summary
Synthetic development scenario D-012 tested catalog latency caused by a database lock.
## Timeline
- T+0: lock-contention fixture enabled.
- T+5m: product-catalog traces showed astronomy-db wait time and elevated p95.
- T+7m: approved flag-off action; database spans and catalog latency were checked.
## Root cause
Injected productCatalogLockContention, supported by database wait spans and the matching fixture change.
## Fix
The typed flag-off action was dry-run and approved. No query termination or database restart occurred.
## Lessons
- Apply only when catalog traces demonstrate astronomy-db waits and the matching flag is active. Catalog errors without wait evidence do not establish lock contention.
