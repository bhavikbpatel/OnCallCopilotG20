# Evaluation harness (Person 4)

Scores the On-Call Copilot against faults we inject ourselves, so the true root
cause and correct fix are always known. Code decides first; an LLM judge from a
different model family only scores what code cannot.

## Layout

| Path | Contents | Status |
|---|---|---|
| `schemas/catalogue.py` | Astronomy Shop 3.1.0 flags, the 12 fault types and their root-cause services | done |
| `schemas/case.py` | Case spec format (validated) | done |
| `schemas/result.py` | Result-line format every run must produce; `payload_hash` | done |
| `schemas/*.schema.json`, `schemas/examples/` | JSON Schema exports and an example result line | done |
| `checkers/checks.py` | Code checks: root cause top-1/top-3, correct action, harmful/unnecessary/forbidden actions, approvals, dry run, permissions, budget, loops, recovery, severity | done |
| `cases/dev/` | 20 development cases. Shared: anyone may use these to debug | done |
| `config/models.yaml` | OpenRouter judge model (different family from the agents) | done |
| `settings.py` | Reads secrets from environment, `eval/.env` or the repo-root `.env` (both git-ignored) | done |
| `adapters/copilot_state.py` | Person 2's final `IncidentState` -> result line | done |
| `injector/` | Fault injector: applies a case's faults (intensity, timing, intermittent, load) through the flag UI API, confirms every change, detects contamination, always clears, writes a ground-truth record; CLI | done |
| `telemetry/prom.py` | Prometheus client (standard library only) | done |
| `telemetry/signals.py` | The agreed signal → PromQL map (error rate, error count, p95, p50, throughput, memory, CPU %, Kafka lag) and `errors_since` for recovery; lesson predicates use supported service-scoped signals | done |
| `probes/` | Live-shop probes. `discover`: metric names, baselines, error breakdown, noise check. `faults`: inject each fault, measure which signals move, how fast, and whether they recover after the flag is cleared | done |
| `recovery/` | Independent recovery check from Prometheus for the result line: errors since the fix (low-traffic paths) or the signal past its limit in 2 distinct data points; threshold or factor x pre-injection baseline; "cannot judge" (never "healthy") when there is no data, too little traffic or no baseline | done |
| `knowledge/runbooks/`, `knowledge/postmortems/` | 20 operational runbooks and 10 explicitly synthetic dev postmortems | done (local corpus) |
| `knowledge/retrieval.py` | Markdown ingestion, metadata filters, generated diagnostic subqueries, BM25 + TF-IDF vector RRF + top-20 deterministic reranker | local baseline |
| `knowledge/memory.py` | SQLite accepted-incident store, lesson review/deletion lifecycle and fail-closed preconditions | local baseline |
| `knowledge/severity.py` | Deterministic severity classifier matching `../docs/severity-policy.md`; unknowns fail closed | done |
| `knowledge/retrieval_queries.json` | 41 labelled development queries for Recall@5 | done |
| `runner/` | Runs cases end to end: preflight, baselines, inject, synthetic alert (fires on the probe's calibrated movement rule), agent with auto-approval and timeout, recovery check, always clear, contamination check, result line, score; JSONL + summary; CI gate. Ships the do-nothing agent | done |
| `judge/` | Judge prompt, rubric, Cohen's kappa calibration (dev outputs only) | later |
| `experiments/` | E1–E4 config files, all run by the same runner | later |
| `tests/` | pytest for everything above | done |
| `results/` | Run outputs. Git-ignored | — |

The contract for teammates is `../docs/eval-result-contract.md`.

## Setup

```bash
# Keep the virtual environment on BMac but outside the repo, so it is never committed
python3 -m venv /Volumes/BMac/IITB/Capstone/4/.venv
source /Volumes/BMac/IITB/Capstone/4/.venv/bin/activate
cd /Volumes/BMac/IITB/Capstone/4/OnCallCopilotG20
pip install -r eval/requirements.txt
cp eval/.env.example eval/.env                          # then fill in the key
```

## Run the tests

```bash
cd OnCallCopilotG20/eval && python -m pytest                 # unit + contract tests (no shop needed)
cd OnCallCopilotG20/eval && EVAL_LIVE=1 python -m pytest -m live -v   # live tests: shop must be running
```

## Fault injector

```bash
cd OnCallCopilotG20
python -m eval.injector status                    # every flag; active ones marked
python -m eval.injector inject D-001 --duration 600
python -m eval.injector clear                     # put every flag back to rest
```

`inject` always clears on exit (normal end, error, Ctrl+C, closed terminal) and
saves a ground-truth record in `eval/results/ground_truth/`. Before evaluation
runs, make sure the flag UI's scheduler (http://localhost:8080/feature/scheduler)
is stopped; if anything else changes a flag, the run is marked contaminated.
See `../docs/eval-implementation-plan.md` for the design.

## Live-shop probes

```bash
cd OnCallCopilotG20
python -m eval.probes discover --no-noise   # ~10 s, read-only
python -m eval.probes discover              # ~4 min, adds the flag-write noise check
python -m eval.probes faults --only control,paymentFailure   # ~13 min quick check
python -m eval.probes faults                # ~90 min: control, 12 faults, flood
python -m eval.probes reanalyse eval/results/probes/faults-....json   # re-score with current rules
```

Reports go to `eval/results/probes/` (git-ignored).

## Runner

```bash
cd OnCallCopilotG20
python -m eval.runner --cases eval/cases/dev --only D-001 --config-id TRY       # one case
python -m eval.runner --cases eval/cases/dev --config-id DEV-NULL               # all dev cases
python -m eval.runner --cases eval/cases/dev --config-id CI --min-success 0.6   # CI gate (exit 1 below)
```

Each case: every flag must be at rest (or `--force-clear`), the alert's healthy level
and the recovery baseline are measured, the faults are injected, and the agent starts
only when the synthetic alert fires (its signal above the probe's movement threshold in
two distinct data points). The agent gets the alert and nothing else. Approvals are
answered by `--approval` (approve or reject) and recorded; after the run nothing more is
approved. Recovery is judged by `eval/recovery`; every flag is cleared at the end, also
on Ctrl+C. A run whose alert never fires, or where a flag changed that neither the plan
nor the agent's approved actions changed, is an `error`, not an agent failure.

Output: `eval/results/runs/<config_id>-<time>/results.jsonl`, `scores.jsonl`,
`summary.json`, and `ground_truth/` (git-ignored). A dev case takes about 6-12 minutes
(baseline, alert, recovery window).

Agents: `do-nothing` (floor baseline). Person 2's graph plugs in as another agent in
`runner/agents.py` (alert in, approvals through `ctx.approve`, final state out).

## Knowledge and memory baseline

```bash
cd OnCallCopilotG20 && python -m eval.knowledge.evaluate
cd OnCallCopilotG20 && python -m pytest eval/tests/test_knowledge.py
```

The evaluator compares keyword-only BM25 against generated-query hybrid
retrieval on the labelled development set. Recall@5 is a dev-only measurement,
not evidence of held-out generalization; relevant labels may include multiple
sections that answer an alert. The TF-IDF vector index and deterministic
reranker are dependency-light placeholders, not the planned
BGE/Qdrant/cross-encoder production stack. `knowledge/lessons.yaml` contains
proposals only; call `MemoryStore.load_proposals()` and require a named reviewer
to approve lessons before retrieval. Signal predicates must use names registered
in `telemetry/signals.py`; supported signals are restricted to service-qualified
error rate and p95 latency. Unknown metrics (including DB lock wait, which the
pinned shop does not confirm) are rejected. See `../docs/memory-policy.md`,
`../docs/severity-policy.md` and `../docs/knowledge-demo.md`. No held-out cases
are included.

## Regenerate the JSON Schemas after changing a schema

```bash
cd OnCallCopilotG20 && python -m eval.schemas.export
```

## Rules

1. Expected outcomes are written before the agent runs on a case.
2. **Held-out cases and red-team attacks are not in this repo.** They live in
   the folder named by `EVAL_PRIVATE_DIR`, which must be outside the repo.
   Persons 2 and 3 must not see them.
3. Gold answers never appear in prompts, tools, runbooks, memory or injected log text.
4. Diagnosis is never scored using the agent's own confidence.
5. Failed and timed-out runs are reported, never dropped.
6. If anything changes after seeing held-out results, those cases become
   development cases and the report says so.
7. API keys go only in `eval/.env`, the repo-root `.env` or the shell environment, never in a committed file.
   The harness uses Person 2's `OPENAI_API_KEY` as the OpenRouter key only if it starts with `sk-or-`.

## Target shop

OpenTelemetry Astronomy Shop **3.1.0**. See `../shop/astronomy/README.md`.
