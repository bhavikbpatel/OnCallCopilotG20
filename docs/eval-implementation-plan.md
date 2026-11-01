# Evaluation harness: implementation plan

**Owner:** Person 4 · **Status:** living document · **Started:** 31 Oct 2026

Build order for `eval/`. Each step depends on the ones above it. Held-out cases
and attacks are referred to only by format here; their content stays private.

## How we build (applies to every step)

1. **Test first, at three levels.**
   - *Unit tests*: pure logic (schedules, scoring, parsing) with no network and no shop.
   - *Contract tests*: real HTTP/Prometheus clients against small local fake servers
     that mimic the real API, including its quirks (e.g. asynchronous writes).
   - *Live tests*: marked `live`, skipped by default, run on the Mac against the
     running shop with `EVAL_LIVE=1 python -m pytest -m live`.
2. **No regressions.** The full suite (`python -m pytest`) must pass before any
   commit. Tests are never deleted or weakened to make a change pass.
3. **No LLM needed for steps 1–5.** Everything there is deterministic code, so it
   is fast, free and repeatable.
4. **Standard library over new dependencies** where reasonable (`urllib`,
   `http.server`, `threading`), so the harness runs on any team laptop.
5. **Every component is injectable** (clock, sleep, HTTP store), so slow or
   time-based behaviour is tested in milliseconds.
6. **Fail safe.** Anything that changes the shop must restore it on every exit
   path: normal end, exception, Ctrl+C.

## Steps

| # | Component | What it delivers | Needs from others | LLM key |
|---|---|---|---|---|
| 1 | `injector/` | Turn faults on/off per case (intensity, timing, intermittent, load), ground-truth record, contamination detection, guaranteed clean-up, CLI | — | No |
| 2 | `probes/` | Real metric names (signal → PromQL map), healthy baselines, fault effects; finalise dev-case thresholds and open questions | Shop running | No |
| 3 | `recovery/` | Independent recovery check from Prometheus for the result line (**done 31 Oct**) | Step 2 map | No |
| 4 | `runner/` | Case → preflight → inject → agent → auto-approval per config → timeout → recovery → score → clear → JSONL; batch summary; CI command (**done 1 Nov**, do-nothing agent) | Stub agent now; Person 2 entry point and Person 3 hooks log later | No (stub) |
| 5 | private set | 40+ held-out cases (**42 written 1 Nov**, private), 30+ attacks (**34 written 1 Nov**, private; 28 deliverable now), attack schema and delivery, attack-success and false-block checks (**done 1 Nov**) | Person 3 defences switch; Persons 1-3 for runbook / tool / chat vectors | No |
| 6 | `judge/` | Rubric, structured judge via OpenRouter, hand labels, Cohen's kappa | Real agent outputs | **Yes** |
| 7 | `experiments/` | E1–E4 configs, repeats, tables, experiment log, report | Everything above | Yes |

## Step 1 design: fault injector

**Switching method: the flag UI's REST API**, reached through the shop's proxy
(`http://localhost:8080/feature/api/read` and `/api/write`).

Why not edit `src/flagd/demo.flagd.json` directly? In 3.1.0 the flag UI loads that
file into memory once at start-up and writes its whole in-memory copy back on
every change (including scheduler changes). A direct file edit would be silently
overwritten by the next UI write. Using the API keeps one source of truth.

Quirks handled:
- `/api/write` replaces the **whole** configuration and is **asynchronous**, so
  every write is confirmed by reading back until it matches (with a timeout).
- `productCatalogFailure` is switched through its targeting rule
  (`targeting.if[1]`), not `defaultVariant`; same logic as the UI's own code.
- Every flag rests at `off` in 3.1.0.
- `paymentFailure`'s `90%` variant is actually 0.95 upstream; noted, not changed.

Contamination detection: the scheduler has no API, so the injector checks before,
during and after each run that every flag it did not set is still at rest. A run
where anything else moved is marked contaminated.

Modules:
- `flagstore.py`: flag read/write (HTTP client, in-memory fake), variant get/set helpers.
- `plan.py`: case → timed list of flag events (pure, unit-tested).
- `injector.py`: executes a plan in the background, confirms each write, detects
  drift, always clears; context manager.
- `record.py`: ground-truth record per run (written to `eval/results/`, git-ignored).
- `__main__.py`: CLI `status | inject CASE | clear | set FLAG VARIANT`.

## Decisions log

| Date | Decision |
|---|---|
| 31 Oct | Injector uses the flag UI API, not direct file edits |
| 31 Oct | Contamination detected by checking untouched flags stay at rest |
| 31 Oct | Injector verified live: D-001 fault visible in Jaeger ~11 s after injection; clean clear |
| 31 Oct | Prometheus reached directly at localhost:9090; span-metrics names taken from the bundled Grafana dashboards |
| 31 Oct | Noise check uses a no-op flag write: any error spike after it is an injection artefact, not a fault |
| 31 Oct | Signal map fixed from discovery: server-span error rate/latency, container memory/CPU (CPU in percent), Kafka group lag; recovery counts errors since the fix (low checkout traffic) |
| 31 Oct | Fault probe measures each fault against a control run (no-op write) with identical sampling; recovery-after-clear answers whether leaks need a restart |
| 31 Oct | Rate windows must be at least 2 minutes: span metrics arrive about once a minute, and a 1-minute window returned no data on the real shop (first fault probe measured nothing). Enforced in signals.py; the probe also stops early if no request signal has data, and recovery sampling must cover window + 60 s |
| 31 Oct | Movement rules calibrated on the live control run: a signal must stay moved for 2 consecutive samples (memory sawtooth), traffic counts only at 2.5x rises or 70% falls (Locust swings 50-80%), and falls only on services with at least 0.5 requests/s (checkout path is ~0.02-0.07/s). Real control-run numbers kept as regression tests; `reanalyse` re-scores saved reports |
| 31 Oct | Full probe re-analysed: effect window extends 120 s past the clear (effects lag 90-180 s); new error_count signal for low-volume faults; latency needs 6x (10x below 1 req/s, ignored below 0.1 req/s); load-generator excluded. Both control runs clean; 10 of 13 items detected; trimmed real data kept as regression fixture |
| 31 Oct | Dev cases revised from the full probe: recovery judged against the run's own pre-injection baseline (latency, memory) or as errors since the fix with a 90 s settle (checkout path, cart); CPU banned as a recovery signal; recommendationCacheFailure excluded from the golden set (no observable effect) and D-006 replaced by imageSlowLoad 5 s late start; emailMemoryLeak restart acceptable, not required; noise-case alerts on traffic |
| 31 Oct | Effects must show in 2+ distinct Prometheus data points (identical readings <45 s apart are one point); default probe hold 300 s. Re-probe (300 s): cartFailure detected via error count, kafkaQueueProblems via lag; control clean despite single-point ad spikes. New p50 latency signal to settle adHighCpu |
| 31 Oct | Aligned with Person 2's graph: tool names restart_service/scale_service, set_flag {flag, variant}, payload hash = plain sha256 hex of the flat action (copilot.state.payload_hash), severity SEV-1..SEV-4, approvals approve/edit/reject, rollbacks recognised by purpose. Adapter eval/adapters/copilot_state.py turns IncidentState into a result line; the full tool log and approvals come from the caller. Settings also read the OpenRouter key from Person 2's root .env (OPENAI_API_KEY, only if it is an sk-or- key) |
| 31 Oct | Recovery checker (eval/recovery): since_fix counts errors and requests from settle_s to within_s after the fix, evaluated once that range has reached Prometheus (+40 s), and needs at least 5 requests (same as Person 2's verify_min_requests); window form polls every 30 s from max(settle_s, 40 s) and counts as recovered after 2 distinct data points in a row past the limit. No data, too few requests, no or zero baseline, an unsupported service/signal, Prometheus errors, or a window check started after its deadline give recovered = None with a reason. Cases must use op gt for throughput and op lt for every other signal |
| 31 Oct | Team moving to Python 3.12 (Person 2's graph needs 3.11+); installed on BMac with uv |
| 31 Oct | Team Macs run Python 3.9: a test scans eval/ for 3.10+ features; files are also compiled with 3.9 |
| 31 Oct | Python 3.9 restriction dropped: the team runs Python 3.12, so test_py39_compat.py is removed. recommendationCacheFailure marked unused in catalogue.py (kept until the team decides on a replacement). .DS_Store files removed from git and ignored |
| 1 Nov | Runner (eval/runner): the agent starts only when a synthetic alert fires, using the fault probe's calibrated movement rule as the threshold (CartErrorRateHigh watches the error count); a case whose alert never fires is an infrastructure error. The agent sees only the alert (Person 2's Alert fields). Approvals are automatic per config, recorded with their payload hash, and refused once the run ends (a timed-out agent thread cannot act later). Flag changes not made by the plan or by the agent's approved writes mark the run contaminated. Task success counts errors and timeouts as failures; the CI gate fails below --min-success. Ships only the do-nothing agent |
| 1 Nov | From the 300 s re-probe: adHighCpu excluded (no observable effect; D-008 now adFailure late start, D-018 pairs cart with kafka); traffic-rise bar 2x (was 2.5x) in the probe and the runner's alerts; imageSlowLoad cases recover within 600 s, emailMemoryLeak within 900 s with flag off + restart as a second correct option; probe gets --window to test a 5-minute window for intlShippingSlowdown. Real 300 s data kept as regression fixture |
| 1 Nov | intlShippingSlowdown with a 5-minute window: p95 showed nothing (slow requests under 5% of the window). New signal slow_requests (server requests over 1 s, from the histogram's 1000 ms edge) added to the signal map and the probe; counts only on services with none at rest, +1 to move. Cases and alerts switch to it only after a re-probe confirms it |
| 1 Nov | Held-out set: 42 cases in EVAL_PRIVATE_DIR (9 golden faults x 4 variants of intensity, start time, flood load and intermittent; 3 noise; 3 two-fault), scenario groups never shared with dev, severities from Person 1's policy (docs/severity-policy.md). intlShippingSlowdown cases wait for its signal decision. eval/schemas/split.py checks the split and prints counts only. Injector release: once the runner approves a fix for an injected flag, the plan stops changing it (intermittent faults no longer come back after a correct fix; D-019 was affected) |
| 1 Nov | intlShippingSlowdown excluded: no slow shipping request in three runs (5-minute p95 and the slow_requests count, which works on other services); its effect depends on the load generator sending international orders. D-013 replaced by productCatalogFailure late start (severity per Person 1's policy). Golden set: 9 fault types; split 20 dev + 42 held-out = 62 |
| 1 Nov | Case severities follow Person 1's policy (docs/severity-policy.md), applied to each fault's measured effect; every case cites the rule ID. Dev changes: D-001, D-017, D-019 SEV-2 -> SEV-1 (checkout error rate >= 10%, S1.1); D-005 SEV-3 -> SEV-2 (partial, S2.1). The case schema accepts only the policy's rule IDs and checks each rule's level. Open with Person 1: when severity is judged (the 5-minute duration clauses vs alerts firing 1-2 min after injection) and a minimum request count at the 10% boundary |
| 1 Nov | Live smoke run of the runner on the real shop (do-nothing agent, D-001 and D-015): alerts fired (the 2x traffic bar works live), recovery judged not recovered with enough traffic, flags cleared, no drift; 4-10 min per dev case. Red team: 34 private attacks (log/trace, error-message, encoded and multilingual injection, fake approvals, out-of-range actions, canary secrets, budget loops, impersonation), each run alongside a dev case; attacker actions never overlap a case's correct actions. HTTP vectors are delivered by the harness; runbook, tool-description and chat vectors wait for Persons 1-3 and are refused (not counted) until then. E2 scoring: success, influence, false blocks |
