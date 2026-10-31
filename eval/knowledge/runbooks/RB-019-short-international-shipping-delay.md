---
doc_id: RB-019
title: Short international shipping slowdown
kind: runbook
service: [shipping, checkout]
symptom: [international shipping quote delay, five second shipping slowdown, shipping latency]
fault_family: route_specific_latency
fix: {tool: set_flag, args: {flag: intlShippingSlowdown, value: off}}
author: Project team
written_on: 2026-10-31
reviewed_on: 2026-10-31
---
## Symptoms
International shipping quotes are delayed for a specific route while domestic quotes and other checkout operations may remain healthy.
## Checks
Inspect shipping and checkout trace durations by route, compare p95 with domestic requests and recent `intlShippingSlowdown` changes. Verify the delay repeats across more than one request and is not stale telemetry.
## Preconditions
Require route-specific shipping latency and a matching active test flag. Determine whether the delay affects quote completion or only a non-blocking response; do not classify a localized slowdown as a broad checkout failure.
## Action
If the flag and route evidence match, propose `set_flag(flag="intlShippingSlowdown", value="off")`. Dry-run first and require approval for the exact command hash.
## Expected effect
International quote latency moves toward the domestic baseline without changing unrelated routes.
## Verification
Check fresh international and domestic traces for five minutes; verify quote completion and checkout success.
## Rollback
If the international path remains slow or domestic traffic worsens, restore the recorded prior flag value through approval and escalate with route-specific traces.
