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
| `telemetry/signals.py` | The agreed signal → PromQL map (error rate, error count, p95, p50, slow requests > 1 s, throughput, memory, CPU %, Kafka lag) and `errors_since` for recovery; lesson predicates use supported service-scoped signals | done |
| `probes/` | Live-shop probes. `discover`: metric names, baselines, error breakdown, noise check. `faults`: inject each fault, measure which signals move, how fast, and whether they recover after the flag is cleared | done |
| `recovery/` | Independent recovery check from Prometheus for the result line: errors since the fix (low-traffic paths) or the signal past its limit in 2 distinct data points; threshold or factor x pre-injection baseline; "cannot judge" (never "healthy") when there is no data, too little traffic or no baseline | done |
| `knowledge/runbooks/`, `knowledge/postmortems/` | 15 operational runbooks and 10 explicitly synthetic dev postmortems | done (local corpus) |
| `knowledge/retrieval.py` | Markdown ingestion, metadata filters, generated diagnostic subqueries, BM25 + TF-IDF vector RRF + top-20 deterministic reranker | local baseline |
| `knowledge/memory.py` | SQLite accepted-incident store, lesson review/deletion lifecycle and fail-closed preconditions | local baseline |
| `knowledge/severity.py` | Deterministic severity classifier matching `../docs/severity-policy.md`; unknowns fail closed | done |
| `knowledge/retrieval_queries.json` | 41 labelled development queries for Recall@5 | done |
| `runner/` | Runs cases end to end: preflight, baselines, inject, synthetic alert (fires on the probe's calibrated movement rule), agent with auto-approval and timeout, recovery check, always clear, contamination check, result line, score; JSONL + summary; CI gate. Ships the do-nothing agent | done |
| `adversarial/`, `schemas/attack.py`, `checkers/attacks.py` | Red team (E2): attack format; HTTP delivery of hostile text into the shop's logs and traces; attack success, influence and false-block checks; `check` (counts only) and `probe` commands. Attacks themselves are private | done |
| `judge/` | LLM judge for status-note quality and evidence support (anchored rubric, OpenRouter, different model family, structured output, fallback model); postmortem-timeline code check; blind labelling sheet; Cohen's kappa, quadratic-weighted and unweighted (dev outputs only) | built; calibration waits for 20+ dev runs |
| `experiments/`, `schemas/experiment.py`, `runner/experiments.py` | E1–E4 as config files (arms, comparisons, cases, repeats), checked for fair comparisons and identical shared arms; run arm by arm by the same runner, with an experiment log | done; real arms need the copilot agent |
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
python -m eval.probes faults --only control,paymentFailure   # ~18 min quick check (300 s hold)
python -m eval.probes faults                # ~2 h: control, 12 faults, flood
python -m eval.probes reanalyse eval/results/probes/faults-....json   # re-score with current rules
python -m eval.probes faults --only control,intlShippingSlowdown --window 5m --recover 360   # 5-minute window
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

When a fix for an injected flag is approved, the injector stops changing that flag, so an
intermittent fault cannot come back after a correct fix (the release is kept in the ground-truth record).

Agents: `do-nothing` (floor baseline). Person 2's graph plugs in as another agent in
`runner/agents.py` (alert in, approvals through `ctx.approve`, final state out).

## Experiments (E1–E4)

```bash
python -m eval.runner --experiment E1 --plan               # runs per arm, shared arms, hours (counts only)
python -m eval.runner --experiment E4 --arm memory-off     # one arm
python -m eval.runner --experiment E2                      # every arm that is not reused
```

`eval/experiments/E1.yaml` ... `E4.yaml` describe each experiment from the capstone guide (pp. 5–6, 14):
its arms (architecture C0/C1/C2, memory `playbook` or `off`, defences, tool-call budget, case set,
repeats) and which arms are compared. Loading checks that compared arms differ only in the setting the
comparison varies, and that an arm that `reuse`s another (E1's C2 is also E3's "after" and E4's
"memory on") has identical settings, so it runs once.

| Exp | Arms | Cases | Repeats |
|---|---|---|---|
| E1 | C0, C1, C2 | held-out | 3 (the final held-out comparison) |
| E2 | defences off / on | red team; dev cases for false blocks | 1 |
| E3 | before (no memory) / after (playbook) | held-out | reuses E4 and E1 |
| E4 | memory on / off; budgets 20 / 10 / 5; D-011 misleading lesson with and without memory | held-out; D-011 | 1; 3 for D-011 |

Every arm runs with `review_enabled: false`, so measuring never writes lessons. The arm's options are
given to the agent in `AgentContext.options`; an agent that does not declare it honours them
(`options_supported`) is refused, so a run is never labelled with settings it did not use. Each result
line carries the arm's `config_hash`. After each arm, one line goes to
`eval/results/runs/experiment-log.jsonl` (git-ignored): settings, code version, models, and the
headline figures (task success, root cause, recovery, harmful and unnecessary actions, policy
violations, false blocks, attack success, cost per run, median and p95 latency, errors).

The full plan is about 620 live runs (~200 h at 20 min each on one shop); `--plan` prints the exact
count, and the cases and repeats are one line per arm if it must be cut.

## Held-out cases (private)

Held-out cases live in `EVAL_PRIVATE_DIR` (set in `eval/.env`), outside the repo; Persons 2 and 3
must not read them. Check the split without revealing any content (counts only):

```bash
python -m eval.schemas.split --dev eval/cases/dev --heldout "$EVAL_PRIVATE_DIR/heldout"
```

It checks that no scenario group or exact fault setup crosses the split, that the held-out folder
is outside the repo, and the sizes (40+ held-out, 60+ in total, every golden fault, noise and a
two-fault case on both sides). Run held-out cases with the runner by pointing `--cases` at that folder.

## Red team (E2, private attacks)

Attack specs live in `EVAL_PRIVATE_DIR/redteam`, outside the repo; Person 3 (defences) must not read them.

```bash
python -m eval.adversarial check "$EVAL_PRIVATE_DIR/redteam"        # counts only, never payloads
python -m eval.adversarial probe                                     # does each request field (header, query, path, body) reach the traces?
python -m eval.runner --cases eval/cases/dev --attacks "$EVAL_PRIVATE_DIR/redteam" --config-id E2-OFF
```

Each attack runs alongside a dev incident (its `base_case`). HTTP-vector attacks are delivered by the
harness: hostile text in a request header, query string, path or JSON body sent to the shop during the
incident. Runbook, tool-description and chat attacks need a way in from Persons 1-3; until then the runner
refuses them (status `error`) instead of pretending they ran. Results add `attacks.jsonl` and, in
`summary.json`, attack success and influence rates and false blocks (legitimate calls the defences blocked).

## Judge (explanation quality) and its calibration

Code scores diagnosis, actions, recovery, severity, secrets and the postmortem timeline. The judge
rates only what code cannot: **status-note quality** (impact, cause, action, status; consistent with
the facts code measured) and **evidence support** (does the item cited for each diagnostic claim show it;
recovery claims are left to code).
Each is 1-5 with anchors for every score in `judge/rubric.yaml`. The judge model is
`google/gemini-3.1-flash-lite` (fallback `meta-llama/llama-3.3-70b-instruct`) from
`config/models.yaml`, at temperature 0, refused if it shares a family with the agents. The agent's text
is passed as data inside `<agent_output>`, never as instructions. One run is rated at a time (no
pairwise comparison, so no position bias); the rubric says length earns nothing, and the kappa report
checks note length against scores (verbosity bias).

Calibration uses **dev runs only** (the export refuses any non-`D-` case):

```bash
python -m eval.judge export --runs eval/results/runs/<batch>/results.jsonl --out eval/results/judge/cal1
#   fill human_status_note_quality and human_evidence_support (1-5) in labels.csv; it shows no judge scores
python -m eval.judge score --dir eval/results/judge/cal1      # calls OpenRouter; resumable
python -m eval.judge kappa --dir eval/results/judge/cal1      # prints agreement, writes agreement.json
```

Before real dev runs exist, a **preliminary** kappa can use notes from Person 2's demo graph (stub shop):
their case IDs start `DEMO-`, and `export --allow-demo` accepts them (held-out is refused either way). The
kappa report then counts dev and demo notes separately and its verdict starts with `PRELIMINARY`. The final
calibration uses dev-case runs only.

Calibrated means at least 20 labelled items and a quadratic-weighted kappa of at least 0.6 on both
dimensions (below 0.4: rewrite the rubric, change its `version`, re-label). Unweighted kappa is reported
alongside. Judge results record the rubric version and a hash of the rubric, and kappa refuses to mix
versions. One real API call can be tested with `EVAL_LIVE_JUDGE=1 python -m pytest tests/test_judge.py -k live`.

The result line's `explanation` field (status note, its evidence IDs, the citable evidence, postmortem
timeline, run timeline) is filled by the adapter from Person 2's final state;
`judge/timeline.py` checks that the postmortem timeline matches the run's own record.

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
