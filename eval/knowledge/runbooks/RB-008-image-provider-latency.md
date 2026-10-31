---
doc_id: RB-008
title: Slow product images from image provider
kind: runbook
service: [image-provider, frontend-proxy]
symptom: [slow product images, image provider latency, proxy upstream timeout]
fault_family: dependency_latency
fix: {tool: set_flag, args: {flag: imageSlowLoad, variant: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
Image loads are slow while core product APIs may remain healthy; frontend-proxy spans show slow upstream calls.
## Checks
Compare image-provider and frontend-proxy p95 latency, timeouts and trace span durations. Check recent `imageSlowLoad` changes and verify the latency is isolated to image requests.
## Preconditions
Require image-path latency evidence and a matching active flag. If proxy latency is high but image-provider spans are healthy, investigate the proxy/network path instead.
## Action
Propose `set_flag(flag="imageSlowLoad", value="off")`; dry-run and obtain approval for the exact typed action before execution.
## Expected effect
Image-provider and proxy latency decline; core frontend success remains unchanged.
## Verification
Check fresh image request traces and p95 latency for five minutes.
## Rollback
If the image path remains slow, restore the prior flag value using the gate and escalate with the slow trace IDs.
