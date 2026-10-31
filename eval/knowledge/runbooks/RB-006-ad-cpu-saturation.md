---
doc_id: RB-006
title: Ad service CPU saturation
kind: runbook
service: [ad]
symptom: [ad CPU saturation, high ad latency, CPU ratio above baseline]
fault_family: resource_saturation
fix: {tool: set_flag, args: {flag: adHighCpu, variant: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
Ad service CPU stays elevated with rising ad latency or timeouts; frontend may remain usable if ads are non-critical.
## Checks
Compare `container_cpu_utilization_ratio` by service with ad request latency and errors. Inspect traces and recent `adHighCpu` changes; compare request rate to determine whether load alone explains saturation.
## Preconditions
Require two or more fresh CPU samples above the service's known healthy range and evidence that `adHighCpu` is active. Do not scale based on one sample.
## Action
First propose `set_flag(flag="adHighCpu", value="off")` after dry-run and exact approval. Scaling is not part of this procedure; it requires a separate incident decision and a typed `scale(service="ad", replicas=2|3)` action.
## Expected effect
Ad CPU and latency fall toward baseline without degrading frontend request success.
## Verification
Observe CPU, ad latency and frontend errors for five minutes.
## Rollback
If there is no improvement, restore the recorded prior flag state with approval; escalate before any scaling or restart.
