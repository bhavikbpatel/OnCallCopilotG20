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
| 3 | `recovery/` | Independent recovery check from Prometheus for the result line | Step 2 map | No |
| 4 | `runner/` | Case → preflight → inject → agent → auto-approval per config → timeout → recovery → score → clear → JSONL; batch summary; CI command | Stub agent now; Person 2 entry point and Person 3 hooks log later | No (stub) |
| 5 | private set | 40+ held-out cases, 30+ attacks, attack schema and delivery, attack-success and false-block checks | Person 3 defences switch | No |
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
| 31 Oct | Team Macs run Python 3.9: a test scans eval/ for 3.10+ features; files are also compiled with 3.9 |
