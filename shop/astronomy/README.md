# OpenTelemetry Astronomy Shop

**Pinned version: 3.1.0** (released 18 Sep 2026). Flag names change between
releases, and our fault catalogue and golden set depend on them, so everyone
must run the same tag. Avoid 3.0.0 (marked problematic upstream).

## Setup (once)

```bash
cd /Volumes/BMac/IITB/Capstone          # clone OUTSIDE this repo
git clone https://github.com/open-telemetry/opentelemetry-demo.git
cd opentelemetry-demo && git checkout 3.1.0
```

Requirements: Docker Desktop with Compose v2, about 8 GB memory for Docker
(full mode), 14 GB disk. On a Mac with an external drive, set Docker Desktop's
**Disk image location** (Settings → Resources → Advanced) to the drive *before*
the first start, and stop Docker before ejecting the drive.

## Start and stop

```bash
shop/astronomy/start.sh     # full mode + our override
shop/astronomy/stop.sh
```

Set `OTEL_DEMO_DIR` if your clone is somewhere other than
`/Volumes/BMac/IITB/Capstone/opentelemetry-demo`.

**Do not use `make start`:** without `compose.capstone.yaml`, `recommendation`
stays unhealthy (its health check needs `nc`, which the image lacks) and
frontend, frontend-proxy and load-generator never start.

## URLs

| What | URL |
|---|---|
| Shop | http://localhost:8080/ |
| Flag UI | http://localhost:8080/feature |
| Load generator | http://localhost:8080/loadgen/ |
| Grafana | http://localhost:8080/grafana/ |
| Jaeger | http://localhost:8080/jaeger/ui/ |
| OpenSearch (logs; this machine only) | http://localhost:9200 |

## Before any evaluation run

3.1.0's flag UI includes a scheduler that switches random failure flags on by
itself. Make sure it is disabled, or it injects faults nobody recorded.

## Flags in 3.1.0

adFailure, adHighCpu, adManualGc, aiRunawayAgent, aiSlowResponse, cartFailure
(10–100%), emailMemoryLeak (1x–10000x), emitRawPii, failedReadinessProbe
(Kubernetes only), imageSlowLoad (5s/10s), intlShippingSlowdown (5s/10s),
kafkaQueueProblems, loadGeneratorFloodHomepage, paymentFailure (10–100%),
paymentUnreachable, productCatalogFailure, productCatalogLockContention,
recommendationCacheFailure.
