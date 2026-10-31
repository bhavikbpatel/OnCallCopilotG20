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

A second, smaller source: every service that listens to flagd for flag changes
(payment, ad, fraud-detection, ...) keeps a long-lived `flagd.evaluation.v2.Service/EventStream`
connection that **ends with an error every 10.0 minutes** and is reopened at once (a fixed
stream timeout, seen in Jaeger as a 20-minute "trace without root span" made of two 10-minute
errored spans). It happens on a timer whether or not anyone changes a flag. These are client
spans, so they do not affect server-span health signals.

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
| Error count | server-span errors in the window (for low-volume operations) | errors |

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
- Span metrics arrive about **once a minute**, so any `rate()`/`increase()` window must be
  **at least 2 minutes**; a 1-minute window returns no data.
- Measured on the probe (paymentFailure 50%): payment server error rate 0 -> 0.5-0.75 and checkout
  0 -> 0.33-0.8; frontend/frontend-proxy/product-catalog p95 latency roughly 3-4x; first seen in
  metrics about 90 s after injection (2-minute window plus delivery delay); back to baseline
  after the flag was cleared, no restart needed.
- Normal variation (control run): Locust traffic swings 50-80% over minutes; payment memory
  jumps between ~66 and ~95 MB on its own (sawtooth).
- A fault becomes visible in traces quickly: D-001 (paymentFailure 50%) produced its
  first failing checkout **11 s** after injection.
- Services absent from span metrics: recommendation (above), astronomy-db and
  valkey-cart (databases, not instrumented), kafka.
- Noise check: rewriting the flag configuration without changing any value did not
  produce a clear error spike. cart/flagd moved by amounts similar to their normal
  variation; to be repeated during the full fault probe (control item).
- Correction (31 Oct): an earlier guess that flag changes reset flagd connections
  was wrong. The EventStream errors come from a fixed 10-minute timeout (section 1).

## 6. What each fault actually does (full fault probe, 31 Oct)

Method: `python -m eval.probes faults`. Each fault held 150 s at the intensity shown,
then cleared and watched for 180 s; a control run (no fault) measured the same way.
Scored with the calibrated rules (`eval/probes/faults.py`); the control run shows no
movement on any service.

| Fault (intensity) | Visible on root service | First seen | Recovers after flag off |
|---|---|---|---|
| paymentFailure (50%) | payment error rate 0 -> 0.5-0.75; checkout errors too | ~90-120 s | yes, no restart |
| cartFailure (50%) | **not visible as a rate**: only EmptyCart fails (~2-3 calls/min); cart error rate ~1% | - | - |
| productCatalogFailure | product-catalog error rate 0 -> 0.08 (one product only); checkout errors | ~95 s | yes |
| recommendationCacheFailure | **no visible effect** (memory flat at 237 MB, CPU flat); the Java image appears not to implement it | - | - |
| kafkaQueueProblems | **weak**: fraud-detection lag reached 96 in one sample (orders are only ~2.5/min) | - | - |
| adHighCpu | ad p95 7.6 -> 105 ms (14x) | ~30 s | yes |
| emailMemoryLeak (1000x) | email memory 66 -> 100 MB | ~95 s | **yes, without a restart** (fell to 53 MB) |
| imageSlowLoad (5 s) | frontend-proxy p95 106 -> ~6,000 ms | ~90 s | yes |
| paymentUnreachable | checkout error rate 0 -> 1.0 (payment itself healthy) | ~60 s | yes |
| productCatalogLockContention | product-catalog p95 50 -> 15,000 ms; its traffic drops ~80%; wide knock-on slowness | ~60 s | **slowly**: still ~2x after 3 min |
| intlShippingSlowdown (5 s) | shipping p95 47 -> 7,750 ms (the load generator does send non-US addresses) | ~180 s | yes |
| adFailure | ad error rate 0 -> 0.18 | ~120 s | yes |
| flood (load only) | frontend traffic 12 -> 34 req/s (2.8x); **latency did not rise** | ~180 s | yes |

Measurement lessons (built into the probe and its tests):
- Effects appear **90-180 s** after injection (2-minute window plus delivery delay) and
  persist into the first minutes after clearing. Checks must allow for that lag.
- p95 latency is noisy: frontend swings ~4x and ad ~6x with nothing wrong; with
  under ~0.1 req/s (checkout) it is meaningless. Real latency faults were 14x-300x.
- Docker's CPU figure behaves like a slow running average (ad: 3% -> 7% during
  adHighCpu and still 6.4% several minutes later), so it cannot show recovery.
- Low-volume faults need an error **count**, not a rate (new `error_count` signal).

Consequences for the golden set (to agree with the team):
- recommendationCacheFailure cannot be diagnosed from telemetry in this shop version.
- kafkaQueueProblems is too weak at normal load to be a fair case as is.
- The flood alone does not raise latency, so noise cases need an alert that a
  traffic spike actually trips.
- emailMemoryLeak: a restart is acceptable, not required.

## Open items

1. Person 5: agree (or not) the recommendation telemetry fix in section 4.
2. Person 1: runbook note on background flag-lookup noise.
3. Person 4: update dev cases from section 6; re-run cartFailure and kafkaQueueProblems with the error-count signal and a longer hold.
4. Team: decide what replaces recommendationCacheFailure (and possibly kafkaQueueProblems) in the golden set.
