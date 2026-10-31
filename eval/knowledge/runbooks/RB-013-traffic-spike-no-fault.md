---
doc_id: RB-013
title: Traffic spike with no service fault
kind: runbook
service: [frontend, load-generator]
symptom: [traffic flood, increased homepage load, noisy latency alert]
fault_family: load_without_fault
fix: {tool: set_flag, args: {flag: loadGeneratorFloodHomepage, variant: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
Traffic or request volume rises and a latency alert fires, but scoped error rates and dependency traces do not show a fault.
## Checks
Compare throughput, error rate and p95 against baseline across affected services. Check trace freshness and flag/deploy history. Require at least two independent healthy signals before classifying as noise.
## Preconditions
Noise means no confirmed user-impacting failure. If evidence is stale, contradictory or incomplete, keep verdict undetermined and gather read-only data.
## Action
Take no remediation action for a normal production traffic spike. The flag action in metadata is only for clearing a known test fixture; it is forbidden in production response. Never disable alerts or change service capacity without separate approval.
## Expected effect
Incident is closed as no-fault only when errors remain at baseline and the alert resolves naturally.
## Verification
Recheck fresh errors, latency and request volume; document the evidence and no-action rationale.
## Rollback
No production change means no rollback. If a test-only load flag was intentionally set, restore its prior state through the dry-run and approval gate.
