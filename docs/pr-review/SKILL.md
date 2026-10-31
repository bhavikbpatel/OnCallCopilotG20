---
name: pr-review
description: Review an OnCallCopilotG20 pull request, or self-review your own branch before raising one, against the capstone minimum bar, the team's shared specs and correctness, with the same checklist for everyone.
---

# PR review: On-Call Copilot (Capstone Project 4)

Use this before you raise a PR (self-review) and when you review someone else's. It works as a
checklist for a person and as instructions for Claude ("review branch X using docs/pr-review/SKILL.md").

## 1. The standard: same three questions for every PR

Judge every PR, your own included, against exactly these, in this order:

1. **Capstone minimum bar and signature challenges**: `capstone_projects.pdf`, pages 2–3 and 13–14.
2. **The shared specs the team agreed**: the Sprint Backlog ("Shared specs everyone codes against"),
   the System Design (`On-Call-Copilot-System-Design.md`), and the contracts in `docs/`
   (`eval-result-contract.md`, `shop-telemetry-findings.md`).
3. **Correctness**: does the code do what its own docs and comments say, and is that tested?

"It is different from how another person's code does it" is **not** a reason to ask for a change.
Point to one of the three sources instead.

### Fairness rules
- **Same checklist for everyone**, including PRs from the reviewer's own role.
- **When two agreed documents disagree**, raise it as a team decision (section 6). Do not blame the
  author for following one of them.
- **Facts, not suspicions.** Write down what you can point to (file and line). If something only
  *might* be wrong, ask a question instead of stating a finding.
- **Say what you did not do.** If you did not run the tests or the live shop, say so in the review.
- **Re-check main before posting.** Main moves quickly; fetch again and confirm your points still hold.
- **Never reveal private evaluation material** (held-out cases, red-team attacks) in a review, even
  as a hint. Raise those concerns privately with Person 4.
- **Start with what is done well**, then issues, then questions.

## 2. Get the change

```bash
git fetch origin
git log --oneline origin/main..origin/<branch>            # commits in the PR
git diff --stat origin/main...origin/<branch>             # files changed
git diff origin/main...origin/<branch> -- <path>          # read the changes
```

Self-review: run the same commands with your local branch name. If main has moved since you
branched, merge or rebase first, so you review what will actually be merged.

Also read the PR's existing comments and reviews (GitHub page, or
`gh api repos/bhavikbpatel/OnCallCopilotG20/pulls/<n>/reviews`) so you check whether earlier points
were addressed.

## 3. Run the tests (Python 3.12)

The team uses Python 3.12 (the graph needs 3.11+).

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
python -m pytest -q tests                  # copilot tests
(cd eval && python -m pytest -q)           # eval tests; live ones skip unless EVAL_LIVE=1
```

Run the same commands on `main` too and compare. A PR must not turn a passing test into a failing
one. New behaviour needs new tests: each new branch of logic, each error path, each new tool.

If you cannot run the tests, say so in the review.

## 4. Checklist

Tick every item that applies to the files the PR touches. Each item names its source.

### 4.1 Repository hygiene (all PRs)
- [ ] Branch is not `main`; the PR targets `main`. (team git flow)
- [ ] No secrets: no `.env`, API keys or tokens; only `.env.example` with placeholder values.
      ("no keys in the repo", Sprint Backlog)
- [ ] No junk: `.DS_Store`, `__pycache__/`, `.pytest_cache/`, `*.egg-info/`, run outputs under `eval/results/`.
- [ ] **No held-out cases and no red-team attacks** anywhere in the repo (`heldout/`, `redteam/`,
      `private/`), and nothing from anyone's personal work folder. (capstone guide p. 5; Sprint Backlog, Person 4 rules)
- [ ] Files are in the folder the System Design repo layout gives them (`config/`, `copilot/`,
      `mcp_servers/`, `knowledge/`, `eval/`, `ops/`, `docs/`). Deviations are explained in the PR.
- [ ] Prompts and model IDs live in the one versioned config (`config/app.yaml`, `config/prompts/`),
      not in code. (capstone guide p. 3, L16)
- [ ] Models are the course-allowed OpenRouter models; the judge comes from a different model family.
      (capstone guide p. 5)
- [ ] Commit messages are detailed: what changed, why, and how it was tested.
- [ ] Contribution log updated. (Sprint Backlog, "Everyone")

### 4.2 The three project rules (capstone guide p. 2)
- [ ] **The model proposes; code and people decide.** Severity, recovery, permissions, allow-lists
      and arithmetic are plain, tested code, never an LLM's judgement.
- [ ] **Outside text is data, never instructions.** Logs, traces, runbook text, tool descriptions and
      search results are wrapped or marked as data, and no code path acts on instructions inside them.
- [ ] **A person approves each exact change.** Every write to the shop, and every external status
      update, goes through `interrupt()` with approval tied to a hash of the exact payload; any edit
      changes the hash and invalidates the old approval.

### 4.3 Signature challenges (capstone guide p. 14)
- [ ] **A. Diagnosis as planning:** hypotheses live in state with a status; a contradicting tool
      result refutes the hypothesis and triggers a replan; the tool-call budget is enforced and the
      agent stops and reports when it runs out.
- [ ] **B. Safe action:** no shell and no `docker exec`; only allow-listed actions with typed
      arguments; diagnosis agents have no write tools; a dry run comes before every write; recovery
      is verified, with rollback if it does not help.

### 4.4 Minimum bar, by area (capstone guide p. 3)
Check only the rows the PR touches.

| Area | Check |
|---|---|
| Retrieval (L4–L6) | Source and date metadata kept; BM25 + vectors fused with RRF; a reranker; **one query-side technique implemented** (rewriting, multi-query, HyDE or decomposition); answers cite passage IDs; Recall@5 on 30+ labelled queries, before and after one change. |
| Tools / MCP (L7) | FastMCP servers; typed inputs and outputs; errors returned as `{ok: false, error: {code, message}}`, never a crash; every tool marked read or write; write access only for the agent that needs it; one test per tool. |
| Orchestration (L8) | LangGraph with typed state, conditional edges, one self-correction loop, a persistent checkpointer; resume after a stop works. |
| Memory (L9) | Short-term trimming or summarising; one long-term store; the memory policy (what, when, who reads, how corrected or deleted) matches the code; a delete command exists. |
| Planning (L10) | A plan object shown to the user; replan when a tool result contradicts it; ReAct inside specialists. |
| Multi-agent (L11) | Supervisor plus at least three specialists, each with its own tools and a typed hand-off message (never free text); single-agent baseline with the same tools and budget. |
| Improvement (L12) | Reflexion on **dev cases only**; every lesson states when it applies and is reviewed by a person; only relevant lessons retrieved; playbook frozen before held-out runs; one rejected lesson kept with its reason. |
| Evaluation (L13) | Expected outcomes written before the agent runs; code checks first, judge second; judge from another model family, checked against 20+ human labels (Cohen's kappa); traces kept. |
| Human control (L14) | `interrupt()` with approve, edit and reject; approval bound to the exact payload; pre-tool hooks for permission and budget; post-tool hooks log and scrub. |
| Security (L15) | Input and output guardrails; each agent has only the tools it needs; defences can be switched off and on from config (for E2). |
| Deployment (L16) | FastAPI with streaming; `docker compose up` starts everything; CI runs the eval and fails below a threshold; one working alert. |

### 4.5 Evaluation integrity (capstone guide pp. 5–6, Sprint Backlog)
- [ ] Person 2 never reads held-out cases; Person 3 never reads the attack list.
- [ ] No gold answers in prompts, tools, runbooks or memory. Runbooks are written **per failure
      family**, not per golden-set case or variant.
- [ ] Lessons and seed memory come from dev cases only; held-out cases are never ingested or tuned against.
- [ ] Anything changed after seeing held-out results turns those cases into dev cases, and the report says so.
- [ ] Recovery is judged by code from metrics, never from the agent's own claim or confidence.
- [ ] Harmful or unnecessary actions are counted (target zero). Watch for defaults that could cause
      one, for example a generic runbook whose `fix` names one specific flag, which the C0 fixed
      workflow would apply to every case it matches.
- [ ] Experiments are selected by config, not by separate code (C0/C1/C2, memory on/off, budgets
      5/10/20, defences on/off).

### 4.6 Shared names and contracts
- [ ] Service, flag and variant names come from `eval/schemas/catalogue.py` (pinned Astronomy Shop
      3.1.0); nothing invents a service or flag.
- [ ] Telemetry uses the measured metric names in `eval/telemetry/signals.py` and
      `docs/shop-telemetry-findings.md`: `traces_span_metrics_*` by `service_name`, server spans only.
- [ ] Tool names and arguments match the tool contract (see the open decisions in section 6).
- [ ] Final incident state and result lines match `docs/eval-result-contract.md`, including the
      payload hash (sha256 of canonical JSON of the flat action).
- [ ] Severity is set by code from the written severity policy and records the rule ID it applied.

### 4.7 Correctness pitfalls already seen in this project
- [ ] **"No data" is never "healthy."** Prometheus returns `NaN` for 0/0 and nothing for an empty
      range: treat both as unknown (`None`), never as 0 and never as a number to compare.
- [ ] Low-traffic services (checkout: about 2.5 requests per minute) need "errors since the fix", not a
      short fixed window; too few requests means "cannot judge", never a rollback.
- [ ] Prometheus lags 12–40 s behind real time; recovery checks wait before the first reading.
- [ ] flagd errors (~35%) are permanent background noise and never a root cause on their own.
- [ ] `recommendation`, `astronomy-db`, `valkey-cart` and `kafka` have no span metrics: use
      container memory/CPU or their callers.
- [ ] Threshold direction is right for each signal: lower is better for errors, latency, memory, CPU
      and lag, but not for throughput.
- [ ] YAML reads an unquoted `off` as `False`: quote it (`'off'`) or handle it.
- [ ] Inputs that build PromQL or shell-like strings are validated (service names, windows).
- [ ] Unknown inputs fail closed (refuse or escalate), not open.

### 4.8 Docs match the code
- [ ] README and run commands still work as written.
- [ ] Every claim in the PR description, docs and docstrings is true of the code (for example, a
      policy rule in `docs/` is implemented the same way in code).
- [ ] Placeholders are labelled as placeholders, with what will replace them.

## 5. Severity of a finding

Label each point so the author knows what blocks the merge:

- **Must fix:** breaks the minimum bar, a project rule, a shared spec, a test, or evaluation integrity;
  or causes a wrong action, a wrong score or a leak.
- **Should fix:** correctness or test gaps that do not break anything yet.
- **Nit:** naming, wording, small tidy-ups.
- **Team decision:** two agreed documents disagree, or the PR needs a choice that is not one person's
  to make. Not the author's fault; raise it with the whole team.
- **Question:** you are not sure; ask.

## 6. Open team decisions (do not pick a side in a review)

These are known disagreements between the Sprint Backlog, the System Design and the current code.
Until the team settles each one and records it in the Sprint Backlog, a PR that follows **either**
side is acceptable; flag the conflict as a team decision.

| Topic | Sprint Backlog | System Design / current code |
|---|---|---|
| `set_flag` argument | `value` | `variant` |
| Restart and scale tool names | `restart`, `scale` | `restart_service`, `scale_service` |
| Rollback arguments | `rollback(action_id)` | `rollback(service, to_version)` |
| Severity labels | SEV1, SEV2, SEV3 | SEV-1 … SEV-4 (plus NONE for noise) |
| Checkpointer | SQLite | Postgres |
| Long-term memory | one store, owned by Person 1 | two implementations exist (`copilot/memory/` and a SQLite lesson store) |
| Knowledge corpus location | not stated | top-level `knowledge/` (System Design) vs `eval/knowledge/` |
| Verify window in eval mode | about 2 minutes per incident | cases allow 300–600 s; checkout needs about 3 minutes to judge |

When a decision is made, update this table (move the row to "Settled" with the date) in the same PR
that records it in the Sprint Backlog.

## 7. Write the review

Use this template. Keep each point to what, where (file and line), why (which source), and a
suggested fix.

```markdown
Review of `<commit sha>` (<branch>). Checked against the capstone minimum bar, the shared specs and
correctness. Tests: <ran on Python 3.12: N passed / not run, and why>.

**Done well**
- ...

**Must fix**
1. **<short title>.** <what and where>. <why, citing the source>. <suggested fix>.

**Should fix**
...

**Nits**
...

**Team decisions** (not this PR's fault; raised for the whole team)
- ...

**Questions**
- ...
```

Post it as a **comment** review (or "request changes" only for must-fix items). If you posted a point
that turns out to be wrong, correct it in a follow-up comment on the same PR.

## 8. Self-review before raising a PR

Before you click "Create pull request":

1. Merge or rebase the latest `main`; run all tests (section 3) on Python 3.12.
2. Walk the checklist (section 4) for every file you changed.
3. Write the PR description: what changed, why, which backlog task it covers, how you tested it,
   what is still a placeholder, and any team decision it touches (section 6).
4. Fix every must-fix item you find yourself, or list it in the description as a known gap.
