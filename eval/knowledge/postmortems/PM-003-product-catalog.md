---
doc_id: PM-003
title: Synthetic exercise — product catalog request failures
kind: postmortem
service: [product-catalog]
symptom: [catalog 5xx, product request failure]
fault_family: feature_flag
fix: {tool: set_flag, args: {flag: productCatalogFailure, value: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Summary
Synthetic development scenario D-005 covered catalog errors affecting a product path.
## Timeline
- T+0: catalog fault flag enabled in the fixture.
- T+5m: catalog error samples and request traces confirmed impact.
- T+7m: approved flag-off action; a fresh affected-product request succeeded.
## Root cause
Injected productCatalogFailure. Evidence: catalog-scoped error traces and the test flag-change timestamp.
## Fix
One dry-run, exact approval and typed flag-off action cleared the injected fault; no service restart was performed.
## Lessons
- Apply only to repeatable catalog errors correlated with productCatalogFailure. A single not-found product or astronomy-db lock evidence requires a different diagnosis.
