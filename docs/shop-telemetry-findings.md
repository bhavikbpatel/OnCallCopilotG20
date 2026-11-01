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

## 6. What each fault actually does (fault probes, 31 Oct)

Method: `python -m eval.probes faults`. Each fault held **300 s** at the intensity shown
(evening re-probe; an earlier run used 150 s), then cleared and watched for 180 s; a control
run (no fault) measured the same way. Scored with the calibrated rules
(`eval/probes/faults.py`): an effect must show in **two distinct data points in a row**. The
control run shows no movement on any service.

| Fault (intensity) | Visible on root service (300 s) | First seen | Recovers after flag off |
|---|---|---|---|
| paymentFailure (50%) | payment error rate 0 -> 1.0, errors 0 -> 4; checkout errors too | ~30 s | yes, no restart |
| cartFailure (50%) | not visible as a rate (only EmptyCart fails, ~2-3 calls/min); **visible as an error count**: cart 0 -> 8 | ~125 s | yes |
| productCatalogFailure | product-catalog error rate 0 -> 0.09 (one product only), errors 0 -> 58 | ~60 s | yes |
| recommendationCacheFailure | **no visible effect** (memory flat at 237 MB, CPU flat); the Java image appears not to implement it. **Excluded** | - | - |
| kafkaQueueProblems | fraud-detection consumer lag 0 -> 166 (110-344 in an earlier run); builds over minutes | ~150 s | yes |
| adHighCpu | **no visible effect**: ad p95/p50 within their normal noise; CPU crept 5% -> 9.5% and did not fall after the flag went off. **Excluded** (1 Nov) | - | - |
| emailMemoryLeak (1000x) | email memory 59 -> 96 MB | ~90 s | **not reliably**: stayed at 96 MB for 3 min (an earlier run fell back to 53 MB) |
| imageSlowLoad (5 s) | frontend-proxy p95 59 -> 7,700 ms; image-provider traffic drops ~75% | ~60 s | **slowly**: still ~5.5x baseline 2.5 min after |
| paymentUnreachable | checkout error rate 0 -> 1.0 (payment itself healthy) | ~60 s | yes |
| productCatalogLockContention | product-catalog p95 49 -> 15,000 ms, p50 9 -> 81 ms; wide knock-on slowness | ~120 s | yes (slowly in the 150 s run) |
| intlShippingSlowdown (5 s) | **real but intermittent**: shipping p95 5,750 / 6,500 / 7,000 ms (control max 170 ms), each spike between normal readings at ~0.2 req/s, so never two in a row. With a **5-minute window p95 showed nothing** (max 9 ms, 1 Nov): the slow requests are under 5% of a longer window. A `slow_requests` count (requests over 1 s) is being tested | - | - |
| adFailure | ad error rate 0 -> 0.13 | ~60 s | yes |
| flood (load only) | frontend traffic 13.9 -> 26-39 req/s (2.2-2.8x); **latency did not rise**. Caught once the traffic-rise bar is 2x (control runs stay within 1.3x of baseline) | ~90 s | yes |

Measurement lessons (built into the probe and its tests):
- Prometheus gets a new span-metric point about **once a minute**. An effect must show in at
  least **two different data points** to count; one point cannot be told apart from a one-off
  spike (ad p95 jumped 10x twice in a control run). The probe holds each fault **300 s**.
- Effects appear **30-150 s** after injection (2-minute window plus delivery delay) and
  persist into the first minutes after clearing. Checks must allow for that lag.
- p95 latency is noisy: frontend swings ~4x and ad ~6x with nothing wrong; with
  under ~0.1 req/s (checkout) it is meaningless. Real latency faults were 14x-300x.
- On services under ~1 req/s (shipping) a fault that hits only some requests shows as
  isolated spikes in a 2-minute p95 and disappears in a 5-minute p95; count the slow
  requests instead (`slow_requests`).
- Docker's CPU figure behaves like a slow running average, so it shows neither a fault's
  start clearly nor its recovery.
- Low-volume faults need an error **count**, not a rate (`error_count` signal).
- Traffic: a rise counts at **2x** (was 2.5x; the flood gave 2.2-2.8x, control runs stayed
  within 1.3x of their baseline and 1.9x peak-to-trough).

Decisions taken from this (1 Nov, Person 4 with Bhavik):
- recommendationCacheFailure and **adHighCpu are excluded** from the golden set
  (`catalogue.EXCLUDED_FAULT_TYPES`, with the evidence). Dev case D-008 is now adFailure with a
  late start; D-018 pairs cartFailure with kafkaQueueProblems instead of adHighCpu.
- Traffic-rise bar lowered to 2x in the probe; the runner's synthetic alerts use the same rule,
  so the noise cases' FrontendTrafficHigh alert fires on the flood.
- imageSlowLoad cases get 600 s to recover; emailMemoryLeak cases get 900 s, and "flag off plus
  restart email" is a correct option next to "flag off" alone.
- intlShippingSlowdown: the 5-minute-window re-probe (1 Nov) hid the effect completely, so p95 is the wrong
  measure for a fault that slows only some requests. New signal `slow_requests` (server requests slower
  than 1 s in the window, from the latency histogram) is measured by the probe; a re-probe decides
  whether cases and the ShippingLatencyHigh alert use it.

## Open items

1. Person 5: agree (or not) the recommendation telemetry fix in section 4.
2. Person 1: runbook note on background flag-lookup noise.
3. Person 4: intlShippingSlowdown re-probe with the `slow_requests` signal; then switch its case and alert to it (or exclude it).
4. Team: confirm the two exclusions (recommendationCacheFailure, adHighCpu). Person 2's FaultType
   enum and flag allow-list still include them, which is fine: the agent may hypothesise them,
   cases just never inject them.
