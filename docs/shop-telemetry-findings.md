# Astronomy Shop 3.1.0: telemetry findings

**From:** Person 4 (Evaluation) · **Date:** 31 Oct 2026 · **Status:** measured on the
pinned 3.1.0 shop (full mode, Docker Desktop on macOS), all flags at rest unless stated.

How measured: `python -m eval.probes discover` (see `eval/probes/`). These findings
decide how the harness checks recovery and matter for anyone building agents,
runbooks, alerts or dashboards.

## 1. Background errors exist even when nothing is broken

With every fault flag off, the shop still produces thousands of errors, almost all
from **feature-flag lookups for flags that do not exist** in 3.1.0's flag file:

| Source | Errors per 5 min | Cause |
|---|---|---|
| `flagd`, span `resolveBoolean` (server) | ~3,100 | flagd answering lookups for unknown flags |
| `fraud-detection` → flagd `ResolveBoolean` (client) | ~2,850 | lookup failing (flag name not reported) |
| `cart` → flagd `ResolveBoolean` (client) | ~300 | **"Flag not found: `cartMetricCardinalityOverflow`"** |

Consequences:
- **flagd shows ~35% failing requests permanently.** It is a built-in red herring:
  it is not a fault and must not be "fixed".
- cart (~19%) and fraud-detection (~50%) look error-heavy if you count *all* their
  spans, because their outgoing flag lookups fail.

**Person 1:** worth a runbook note: "flagd ResolveBoolean errors and flag-not-found
lookups from cart/fraud-detection are known background noise in 3.1.0."
**Person 2:** the agent will see this noise on every incident; diagnosis should not
rank flagd as a root cause from error counts alone.

## 2. The clean health signal: a service's own incoming (server) requests

Measured on **server spans only**, every service shows **0% errors at rest** except
flagd. So the harness judges health and recovery on server spans:

| Signal | Query basis | Unit |
|---|---|---|
| Error rate | server spans of the service; "no error series" counts as 0 | ratio 0–1 |
| p95 latency | server-span duration histogram | ms |
| Throughput | server spans | requests/s |
| Memory | `container_memory_usage_total_bytes{container_name}` | MB |
| CPU | `container_cpu_utilization_ratio{container_name}` | **percent** (e.g. 91.6), not 0–1 |
| Kafka lag | `kafka_consumer_group_lag_sum_ratio{group, topic="orders"}` | messages |

Exact queries: `eval/telemetry/signals.py`.

## 3. Checkout traffic is low

Server requests in 5 minutes (normal Locust load): frontend ~3,600, product-catalog
~1,400, cart ~400, **checkout ~12, payment ~14, email ~14**. That is about 2.5
checkouts a minute. A 2-minute window holds ~5 checkouts: too few to judge a 10%
failure. The harness therefore counts **errors since the fix was applied** instead
of using fixed windows on the checkout path.

## 4. Some published 3.1.0 images do not match the 3.1.0 source

- **recommendation** runs a **Java** program (`/opt/java/openjdk/bin/java`), although
  the repo's `src/recommendation` is Python. It serves requests normally but sends
  **no telemetry**: its OTLP exporter uses HTTP against the collector's gRPC port
  (4317), so the collector resets the connection. It is missing from all span metrics.
  - Fault `recommendationCacheFailure` is therefore visible only through container
    memory/CPU and through its callers (frontend), not its own spans.
  - **Proposed fix (needs Person 5's agreement):** in `shop/astronomy/compose.capstone.yaml`
    set `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` for recommendation. Not applied yet.
- cart and (probably) fraud-detection look up flags that are not in 3.1.0's flag file
  (section 1), which also points to images built from different code.

## 5. Other facts

- Prometheus: v3.13.1 at `http://localhost:9090`; data arrives about **12–40 s** behind
  real time (OTLP push + scrape). Checks must wait at least that long.
- A fault becomes visible in traces quickly: D-001 (paymentFailure 50%) produced its
  first failing checkout **11 s** after injection.
- Services absent from span metrics: recommendation (above), astronomy-db and
  valkey-cart (databases, not instrumented), kafka.
- Noise check: rewriting the flag configuration without changing any value did not
  produce a clear error spike. cart/flagd moved by amounts similar to their normal
  variation; to be repeated during the full fault probe.

## Open items

1. Person 5: agree (or not) the recommendation telemetry fix in section 4.
2. Person 1: runbook note on background flag-lookup noise.
3. Person 4: full fault probe to measure each fault's effect, then update dev cases.
