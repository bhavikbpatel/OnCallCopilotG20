# On-call severity policy

Severity is assigned by deterministic code from observed user impact, signal magnitude, duration and evidence quality. It is not inferred from an alert name or an agent's confidence. Record the Prometheus query/time range or trace IDs supporting each input. A missing/stale signal is unknown, not zero.

| Level | Trigger (all stated conditions unless “or”) | Response |
|---|---|---|
| **SEV1** | Broad critical-path user impact for at least 5 minutes with error rate ≥5%; **or** broad critical-path error rate ≥10%; **or** critical consumer lag ≥1,000 for ≥10 minutes. At least two independent evidence items are required. | Page primary and incident commander immediately; investigate concurrently; propose one scoped, allow-listed fix at a time. |
| **SEV2** | Broad or partial user impact lasting ≥5 minutes with error rate ≥1%; **or** critical-path p95 latency ≥500 ms for ≥5 minutes. At least two independent evidence items are required. | Page the responsible service owner; maintain incident updates; prioritize mitigation and verify recovery. |
| **SEV3** | Localized/low-impact issue, short duration, a signal below SEV2 thresholds, or fewer than two independent evidence items. | Service owner investigates during normal response; continue read-only evidence gathering where causation is uncertain. |
| **NONE** | Fresh telemetry supports no user-impacting failure and at least two independent healthy signals; volume or an isolated alert alone is not impact. | Close as noise with evidence; do not run remediation. |

**Evidence and duration rules.** Error rate is failed requests divided by total requests for the affected user path, over a rolling five-minute window. Latency is p95 and must be sustained for five minutes on a critical path to meet the SEV2 latency clause. Duration is measured from the first supported bad sample, not the alert delivery timestamp. “Broad” means a core checkout/browse path or multiple services; “partial” means a material route, region or cohort; “localized” means a non-critical feature or isolated resource. These scope labels must be backed by request/trace evidence. For unknown user impact, missing/stale error telemetry, an unknown denominator, one sample, or conflicting signals, do not treat absent data as zero or claim NONE; use `S3.0` and continue investigation. A higher impact condition takes precedence; re-evaluate when scope, duration or evidence changes.

Policy IDs emitted in results: `S1.1` broad sustained critical-path impact; `S2.1` sustained material impact; `S3.0` insufficient evidence; `S3.1` localized/short/below threshold; `S0.1` verified no impact. Thresholds are initial project policy and must be reviewed against the pinned shop's healthy baselines before production use.
