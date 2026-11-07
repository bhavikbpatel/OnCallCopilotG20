# Rejected lessons

Lessons the reflection step proposed from dev runs that a person rejected, with the reason (capstone L12 evidence).

## C-D-003-002 (from DEV-C2-D-003-r1-20261104T155718Z)

- **Lesson:** When cart server errors are elevated and the cartFailure flag changed recently, treat that flag as the likely cause and turn it off rather than restarting cart or valkey-cart or rolling back unrelated changes.
- **Preconditions:** `[{"args": {"flag": "cartFailure", "within": "15m"}, "check": "recent_change"}, {"args": {"query": "(sum(rate(traces_span_metrics_calls_total{service_name=\"cart\",span_kind=\"SPAN_KIND_SERVER\",status_code=\"STATUS_CODE_ERROR\"}[5m])) or vector(0)) / sum(rate(traces_span_metrics_calls_total{service_name=\"cart\",span_kind=\"SPAN_KIND_SERVER\"}[5m]))", "threshold": 0.05}, "check": "promql_above"}]`
- **Failure it came from:** wrong verdict (incident vs noise); wrong top-1 root cause; no correct action proposed; no correct action executed
- **Rejected by:** Rohit on 2026-11-07
- **Reason:** The source run errored before the agent ran (alert did not fire; no hypotheses, 0 tokens), so there is no agent failure to learn from. It also duplicates C-D-004-003.

## C-D-005-004 (from DEV-C2-D-005-r1-20261104T162108Z)

- **Lesson:** When product-catalog errors are elevated and traces explicitly identify the Product Catalog Failure feature flag as the source, turn that flag off; do not escalate for lack of justification or restart/rollback the service. Confirm the error signal is elevated before acting, and use the named flag evidence to distinguish this from unrelated catalog failures.
- **Preconditions:** `[{"args": {"query": "(sum(rate(traces_span_metrics_calls_total{service_name=\"product-catalog\",span_kind=\"SPAN_KIND_SERVER\",status_code=\"STATUS_CODE_ERROR\"}[5m])) or vector(0)) / sum(rate(traces_span_metrics_calls_total{service_name=\"product-catalog\",span_kind=\"SPAN_KIND_SERVER\"}[5m]))", "threshold": 0.05}, "check": "promql_above"}]`
- **Failure it came from:** no correct action proposed; no correct action executed; the service did not recover
- **Rejected by:** Rohit on 2026-11-07
- **Reason:** With the productCatalogFailure flag check it needs, it duplicates C-D-013-010 (same flag, signal and action).

## C-D-008-006 (from DEV-C2-D-008-r1-20261104T165810Z)

- **Lesson:** When the ad service's server error rate is elevated, treat the signal as an ad incident rather than noise and disable the `adFailure` flag; do not leave the incident without a corrective action.
- **Preconditions:** `[{"args": {"query": "(sum(rate(traces_span_metrics_calls_total{service_name=\"ad\",span_kind=\"SPAN_KIND_SERVER\",status_code=\"STATUS_CODE_ERROR\"}[5m])) or vector(0)) / sum(rate(traces_span_metrics_calls_total{service_name=\"ad\",span_kind=\"SPAN_KIND_SERVER\"}[5m]))", "threshold": 0.05}, "check": "promql_above"}]`
- **Failure it came from:** wrong verdict (incident vs noise); wrong top-1 root cause; no correct action proposed; no correct action executed
- **Rejected by:** Rohit on 2026-11-07
- **Reason:** The source run errored before the agent ran (alert did not fire; no hypotheses, 0 tokens), so there is no agent failure to learn from. With a flag check added it would also duplicate C-D-014-011.

## C-D-015-012 (from DEV-C2-D-015-r1-20261104T182846Z)

- **Lesson:** When frontend throughput rises but its error rate remains below 1% and p95 latency stays below 500 ms, do not infer a frontend fault from throughput alone; classify the alert as traffic noise unless independent failure evidence appears.
- **Preconditions:** `[{"args": {"query": "(sum(rate(traces_span_metrics_calls_total{service_name=\"frontend\",span_kind=\"SPAN_KIND_SERVER\",status_code=\"STATUS_CODE_ERROR\"}[5m])) or vector(0)) / sum(rate(traces_span_metrics_calls_total{service_name=\"frontend\",span_kind=\"SPAN_KIND_SERVER\"}[5m]))", "threshold": 0.01}, "check": "promql_below"}, {"args": {"query": "histogram_quantile(0.95, sum by (le) (rate(traces_span_metrics_duration_milliseconds_bucket{service_name=\"frontend\",span_kind=\"SPAN_KIND_SERVER\"}[5m])))", "threshold": 500}, "check": "promql_below"}]`
- **Failure it came from:** wrong verdict (incident vs noise); wrong top-1 root cause
- **Rejected by:** Rohit on 2026-11-07
- **Reason:** Can suppress a real incident: in a flood plus another fault, frontend errors stay under 1% and p95 under 500 ms, so it would say noise. The code (is_noise, the commander's noise decision) already owns the noise verdict.

## C-D-016-013 (from DEV-C2-D-016-r1-20261104T183438Z)

- **Lesson:** For a frontend error alert, do not infer an incident or assign a root cause when the frontend server error rate is below the noise floor; classify it as noise unless corroborating telemetry shows a real failure.
- **Preconditions:** `[{"args": {"query": "(sum(rate(traces_span_metrics_calls_total{service_name=\"frontend\",span_kind=\"SPAN_KIND_SERVER\",status_code=\"STATUS_CODE_ERROR\"}[5m])) or vector(0)) / sum(rate(traces_span_metrics_calls_total{service_name=\"frontend\",span_kind=\"SPAN_KIND_SERVER\"}[5m]))", "threshold": 0.01}, "check": "promql_below"}]`
- **Failure it came from:** wrong verdict (incident vs noise); wrong top-1 root cause
- **Rejected by:** Rohit on 2026-11-07
- **Reason:** The source run errored before the agent ran (alert did not fire; no hypotheses, 0 tokens), so there is no agent failure to learn from.

## C-D-017-014 (from DEV-C2-D-017-r1-20261104T184238Z)

- **Lesson:** When the imageSlowLoad flag has recently changed, check image-provider as an independent checkout failure source even if payment errors already explain purchase failures; disable the active imageSlowLoad flag too rather than stopping after fixing payment.
- **Preconditions:** `[{"args": {"flag": "imageSlowLoad", "within": "15m"}, "check": "recent_change"}]`
- **Failure it came from:** no correct action proposed; no correct action executed
- **Rejected by:** Rohit on 2026-11-07
- **Reason:** The graph acts on one fault per incident, so 'also fix imageSlowLoad' cannot be carried out, and in imageSlowLoad-only cases the wording invites an unneeded payment action.

## C-D-018-015 (from DEV-C2-D-018-r1-20261104T185237Z)

- **Lesson:** When cart errors coincide with elevated fraud-detection consumer lag, investigate the fraud-detection kafkaQueueProblems fault flag rather than dismissing queue trouble from normal Kafka broker metrics or lag on another topic; disable that flag. This prevents treating the cart symptom as the only cause.
- **Preconditions:** `[{"args": {"query": "(sum(rate(traces_span_metrics_calls_total{service_name=\"cart\",span_kind=\"SERVER\",status_code=\"STATUS_CODE_ERROR\"}[5m])) or vector(0)) / sum(rate(traces_span_metrics_calls_total{service_name=\"cart\",span_kind=\"SERVER\"}[5m]))", "threshold": 0.01}, "check": "promql_above"}, {"args": {"query": "sum(kafka_consumer_group_lag_sum_ratio{group=\"fraud-detection\",topic=\"orders\"})", "threshold": 0}, "check": "promql_above"}]`
- **Failure it came from:** no correct action proposed; no correct action executed
- **Rejected by:** Rohit on 2026-11-07
- **Reason:** Its queries use span_kind="SERVER", which matches no data, so it can never apply; it has no kafkaQueueProblems flag condition, and cart errors do not show as a rate.

## C-D-020-017 (from DEV-C2-D-020-r1-20261104T192219Z)

- **Lesson:** When `emailMemoryLeak` has recently been enabled and email memory is elevated, treat this as an incident and turn that flag off; do not dismiss it as noise or target another service.
- **Preconditions:** `[{"args": {"flag": "emailMemoryLeak", "within": "15m"}, "check": "recent_change"}, {"args": {"query": "sum(container_memory_usage_total_bytes{container_name=\"email\"}) / 1e6", "threshold": 500}, "check": "promql_above"}]`
- **Failure it came from:** wrong verdict (incident vs noise); wrong top-1 root cause; no correct action proposed; no correct action executed
- **Rejected by:** Rohit on 2026-11-07
- **Reason:** The source run errored before the agent ran (alert did not fire; no hypotheses, 0 tokens), so there is no agent failure to learn from. Its 500 MB threshold is never reached (the leak peaks at about 100 MB); it duplicates C-D-009-007.
