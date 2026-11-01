---
doc_id: RB-003
title: Product catalog request failures
kind: runbook
service: [product-catalog]
symptom: [product catalog 5xx, missing product response, catalog errors]
fault_family: feature_flag
fix: {tool: set_flag, args: {flag: productCatalogFailure, variant: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
Product-catalog requests fail, potentially for a subset of products; other store paths may remain healthy.
## Checks
Compare catalog server-span errors by operation and product where labels exist. Inspect error traces, request volume, recent flag changes and astronomy-db dependency spans. Confirm the failure is broader than a single expected not-found response.
## Preconditions
Require repeatable catalog errors plus evidence of active `productCatalogFailure`. A single product or isolated 404 is not evidence to change the flag.
## Action
Propose `set_flag(flag="productCatalogFailure", variant="off")`. Dry-run first; show current value and exact expected effect; wait for approval of the unchanged action hash.
## Expected effect
Catalog errors return to baseline while product requests remain served.
## Verification
Use fresh catalog error-rate data and a successful trace for an affected request; verify unrelated store services remain stable.
## Rollback
If errors persist or the flag state is uncertain, restore the recorded prior value through an approved action and escalate with trace evidence.
