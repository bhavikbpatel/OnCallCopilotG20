# OnCallCopilotG20

This is the repo for our collaboration for the On Call Copilot Capstone project.

On-Call Copilot takes a firing alert, works out from live telemetry which service failed and why, proposes **one** allow-listed fix for the on-call engineer to approve, verifies recovery (or rolls back), and drafts the postmortem.

- Overall design: [Documents/On-Call-Copilot-System-Design.md](Documents/On-Call-Copilot-System-Design.md)
- Agent graph and memory design: [Documents/agent_graph_design.md](Documents/agent_graph_design.md)

---

## 1. Setup

### Prerequisites

- Python **3.11+** (tested on 3.14)
- An **OpenRouter** API key (https://openrouter.ai/keys) with these models allowed in your workspace guardrails: `openai/gpt-6-luna`, `openai/gpt-5-mini`
- Postgres 16, for a persistent checkpointer and memory store: needed for the frozen playbook (`memory: playbook`)
  and `python -m copilot.reflexion freeze` / `load`; in-memory is used if `POSTGRES_DSN` is not set. See below.

### Install

```bash
git clone <repo-url>
cd OnCallCopilotG20

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"
```

### Postgres (long-term memory, frozen playbook)

Either way, put the DSN in `.env` and check it with `python scripts/check_postgres.py` (creates the tables, then a
write / read / delete).

- **Docker:** `docker compose -f compose.postgres.yaml up -d` with `POSTGRES_PASSWORD` in `.env`, then
  `POSTGRES_DSN=postgresql://oncall:<password>@localhost:5432/oncall`.
- **Windows without Docker or admin rights** (portable binaries):
  1. Download `postgresql-16.x-1-windows-x64-binaries.zip` from https://www.enterprisedb.com/download-postgresql-binaries
     and unzip it to `%USERPROFILE%` (gives `%USERPROFILE%\pgsql`).
  2. `%USERPROFILE%\pgsql\bin\initdb -D %USERPROFILE%\pgsql-data -U postgres -W -A scram-sha-256 -E UTF8`
  3. `powershell -File scripts\postgres.ps1 start` (also `stop`, `status`; run `start` again after a reboot).
  4. `psql -h localhost -U postgres -c "CREATE ROLE oncall LOGIN PASSWORD '<password>'" -c "CREATE DATABASE oncall OWNER oncall"`

On Windows the code uses a selector event loop whenever `POSTGRES_DSN` is set (psycopg cannot use the default
Proactor loop). A test against a real Postgres is opt-in: `COPILOT_PG_TESTS=1 pytest tests/test_postgres_playbook.py`.

### Configure secrets

```bash
cp .env.example .env          # Windows: copy .env.example .env
```

Edit `.env` (it is git-ignored — **never put keys in `.env.example`**):

| Variable | Required | Meaning |
| :-- | :-- | :-- |
| `OPENAI_API_KEY` | yes | Your OpenRouter key (`sk-or-...`). The OpenAI client is pointed at OpenRouter. |
| `OPENAI_BASE_URL` | no | Override the LLM endpoint. Default comes from `config/app.yaml` (`https://openrouter.ai/api/v1`). |
| `POSTGRES_DSN` | no | e.g. `postgresql://user:pass@localhost:5432/copilot`. Empty → in-memory (state lost on restart). |
| `MCP_TELEMETRY_URL`, `MCP_RUNBOOKS_URL`, `MCP_TICKETS_URL`, `MCP_OPS_URL` | no | Override MCP server URLs from `config/mcp_servers.yaml`. |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` | no | Tracing, below. Off unless `LANGSMITH_TRACING=true` and a key are set. |

### Tracing (LangSmith)

Every LLM call, tool call and step of the incident graph goes to LangSmith, so you can follow a diagnosis step
by step (for example the run where the first hypothesis is refuted and the commander replans). An incident shows
up as up to three traces, because the graph pauses at the approval and at the postmortem review and resumes; they
share the incident's thread (its `run_id`).
Get a key at smith.langchain.com (Settings → API Keys), then in `.env`:

```bash
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_PROJECT=oncall-copilot
```

Nothing else to change: the eval runner, the incident runtime and `python -m copilot.reflexion` call
`copilot.tracing.setup()` after loading `.env`. Before anything leaves the machine, `copilot/tracing.py` masks
every string in the trace: approval tokens and other credentials by key, and keys, bearer tokens, emails and
card numbers in free text (the same `redact` the MCP servers use). Metric values and identifiers (`run_id`,
`incident_id`, tool-call ids) stay readable. Tests never trace (`tests/conftest.py`).
LangSmith's free plan (November 2026) has one seat and 5,000 traces a month (about three per incident); share
single traces with the team through LangSmith's share links.

### Check the install

```bash
pytest -q
```

---

## 2. Run the agent

### Option A — Demo with a stub shop (works today, no MCP servers needed)

Runs the real incident graph with **real LLM calls**. The shop (the online store the copilot looks after), its telemetry and the ops server are simulated, with a `paymentFailure` fault injected.

```bash
python scripts/run_demo.py                 # you decide at each pause: approve / edit / reject
python scripts/run_demo.py --auto          # auto-approve everything
python scripts/run_demo.py --no-recovery   # the fix "does not help" → one follow-up step → rollback → replan / escalate
python scripts/run_demo.py --scenario email  # two-step fix: emailMemoryLeak off, then restart email
```

What you will see:

1. Plan → triage (severity set by code) → parallel diagnosis with real tool calls → converge or replan.
2. **⏸ Approval**: exact action, expected effect, rollback plan and payload hash.
   - `a` approve → executes, then verifies the alert metric recovers
   - `e` edit → enter a new action JSON, e.g. `{"tool": "restart_service", "service": "cart"}` → new dry run, new hash, asked again
   - `r` reject → the commander replans
3. Status note + postmortem drafted.
4. **⏸ Review**: accept the postmortem and pick which lessons go into long-term memory.

A full run costs roughly 15–50k tokens.

### Option B — Against the real shop (needs the MCP servers)

Requires the four MCP servers from role 4 (`telemetry`, `runbooks_kb`, `tickets`, `ops_actions`) running at the URLs in `config/mcp_servers.yaml`. The incident API (FastAPI) uses the graph like this:

```python
from langgraph.types import Command
from copilot.runtime import copilot_runtime
from copilot.state import Alert, ApprovalDecision

async with copilot_runtime() as rt:                       # loads .env, connects MCP, builds graph
    config = {"configurable": {"thread_id": incident_id, "engineer_id": "eng1"}}

    # POST /incidents/alert → start (stream to the UI)
    async for ev in rt.graph.astream({"alert": alert}, config, stream_mode=["updates", "custom"]):
        ...                                                # "__interrupt__" = waiting for a person

    # POST /incidents/{id}/decision → resume the pause
    decision = ApprovalDecision(decision="approve", decided_by="eng1")   # the gate mints the token (APPROVAL_SECRET)
    async for ev in rt.graph.astream(Command(resume=decision.model_dump()), config, stream_mode=["updates", "custom"]):
        ...

    # GET /incidents/{id}
    state = rt.graph.get_state(config).values
```

Resume payloads: `ApprovalDecision` at the approval gate and `ReviewDecision` at the review gate (both in `copilot/state.py`).

### Option C — Evaluation runs (`run_incident`)

```python
from copilot.run import run_incident, CopilotAgent        # CopilotAgent: eval/runner's Agent protocol
result = await run_incident(alert, approve, run_id="E1-C2-D-001-r1", limits=BudgetLimits(tool_calls=20),
                            options={"architecture": "C2", "memory": "off", "defences": "on", "review_enabled": False})
result.final_state, result.tool_calls, result.fixed_at, result.budget_exhausted
```

- `approve(flat_action) -> {"decision", "decided_by", "payload_hash"}`; the gate mints the token (needs `APPROVAL_SECRET`).
- Every tool call, code-driven or model-driven, goes through one `Hooks` per incident, so `tool_calls` is the full
  hooks log (`driver: code` for verify polling, outside the budget).
- `architecture`: `C0` fixed workflow (runbook by alert name → its typed fix), `C1` one agent with all read tools,
  `C2` the team. Also selectable with `architecture:` in `config/app.yaml`.

### Option D — Everything in Docker (shop, MCP servers, monitoring)

One command starts the Astronomy Shop (pinned submodule, 3.1.0), its Prometheus, Grafana, Jaeger and
OpenSearch, the four MCP servers on the ports in `config/mcp_servers.yaml`, and Alertmanager:

```bash
git submodule update --init        # once: fetches vendor/opentelemetry-demo at 3.1.0
cp .env.example .env               # set APPROVAL_SECRET (and OPENAI_API_KEY)
docker compose pull --ignore-pull-failures   # once: the shop's 3.1.0 images (several GB)
docker compose up -d               # full shop, ~6-7 GB RAM for Docker
# 8 GB laptops: no Kafka, no browser load, smaller Jaeger
docker compose -f compose.lite.yaml pull --ignore-pull-failures
docker compose -f compose.lite.yaml up -d
```

Pull first. The shop's services can also be built from source, and `up` builds any whose image is not on
the machine yet: that is a long compile of the whole shop. `--ignore-pull-failures` skips our four MCP
images, which exist in no registry; `up` builds those in a few seconds. Do not use `--ignore-buildable`:
it skips the shop images too. The shop images are pinned to 3.1.0 in `shop/astronomy/capstone.env`.

On Windows, give WSL 2 enough memory first: `%UserProfile%\.wslconfig` with `[wsl2]`, `memory=6GB`,
`swap=4GB`, then `wsl --shutdown`. That needs 16 GB of RAM; on an 8 GB laptop Windows freezes, so use a codespace.

**No big laptop? GitHub Codespaces.** On GitHub: **Code → Codespaces → Create codespace on `<branch>`**.
`.devcontainer/` asks for a 4-core / 16 GB machine with Docker inside, fetches the submodule, installs
the package and writes `.env` with a new `APPROVAL_SECRET`. Add your OpenRouter key once as a Codespaces
secret named `OPENAI_API_KEY` (GitHub → Settings → Codespaces → Secrets, repository OnCallCopilotG20).
Then run `docker compose -f compose.lite.yaml pull --ignore-pull-failures` and
`docker compose -f compose.lite.yaml up -d` in the codespace terminal and open port 8080 from
the **Ports** tab. Stop the codespace when you finish: the free quota is 120 core-hours a month,
about 30 hours on this machine.

| What | URL |
| :-- | :-- |
| Shop / flags | http://localhost:8080 , http://localhost:8080/feature |
| Grafana (shop dashboards + "On-Call Copilot" folder) | http://localhost:8080/grafana |
| Prometheus / Alertmanager | http://localhost:9090 , http://localhost:9093 |
| MCP servers | http://localhost:8101/mcp … 8104/mcp (localhost only) |

Postgres for the copilot (`compose.postgres.yaml`, above) is a separate file; it shares the project name, so
Compose may list it as an orphan. That is harmless: do not pass `--remove-orphans`.

`set_flag` works from the container. `restart_service`, `scale_service` and `rollback` need the actuator
sidecar (`ACTUATOR_URL`, not built yet) and return an executor error in Docker until then.

**Alerts.** `infra/prometheus/rules/shop.yml` holds one rule per alert name the cases use
(`CheckoutErrorRateHigh`, `CartErrorRateHigh`, …), generated from `eval/telemetry/signals.py` so a live alert
watches the same PromQL as the runner: `python infra/prometheus/build_rules.py` after changing either.
Alertmanager posts them to the incident endpoint at `http://host.docker.internal:8000/alerts`.
`infra/prometheus/rules/agent.yml` watches the copilot itself (repeated rollbacks, loop or budget blocks,
harmful actions, failing runs); those go to the team, never to the copilot.

**Agent metrics.** Set `COPILOT_METRICS_PORT=9464` and the eval runner serves `/metrics` (`copilot/metrics.py`);
Prometheus scrapes it and the "On-Call Copilot: agent" dashboard shows runs, success, rollbacks, harmful
actions, cost, durations, tool calls and hook blocks. Rebuild the dashboard with
`python infra/grafana/build_dashboard.py`.

**CI.** `.github/workflows/ci.yml` runs both test suites, `docker compose config` (full and lite),
`promtool`, `amtool` and the MCP image build on every PR. `.github/workflows/eval-gate.yml` (manual) starts
the lite stack on a GitHub runner, runs development cases with `eval.runner --min-success`, and fails
below the threshold. It needs the `OPENAI_API_KEY` and `APPROVAL_SECRET` repository secrets.

### Option E — The on-call console (Streamlit)

With the stack running (Option D) and `.env` filled in:

```bash
pip install -e ".[ui]"
streamlit run ui/app.py          # http://localhost:8501
```

The page lists the shop's firing alerts from Prometheus. **Investigate** starts the copilot on one and shows the
plan, hypotheses and timeline live. When the copilot wants to change the shop, it pauses on an approval screen
with the exact action, its dry run, expected effect, rollback and payload hash: approve, reject, or edit the
action. After recovery is checked it pauses again for the postmortem review, then shows the final status.
To try it, turn a fault on (`python -m eval.injector set paymentFailure 50%`), wait about 5 minutes for
`CheckoutErrorRateHigh`, and `python -m eval.injector clear` afterwards. Set `COPILOT_ENGINEER` in `.env` for
the name recorded on approvals. One incident at a time per console. Evidence: `docs/evidence/ui/`.

---

## 3. Configuration

| File | What it controls |
| :-- | :-- |
| `config/app.yaml` | LLM endpoint, model per agent, temperatures, budgets, replan limit, verify window, memory flags |
| `config/prompts/*.yaml` | One prompt per agent step (role, context, task, constraints, format, examples) |
| `config/permissions.yaml` | Which MCP tools each agent may be given (Diagnosis: read-only) |
| `config/mcp_servers.yaml` | MCP server URLs and transport |

Current models: commander `openai/gpt-6-luna`; triage, diagnosis, runbook, remediation, comms `openai/gpt-5-mini`.
Semantic search over past incidents is off (`embedding_model: null`) until an embedding model is allowed on OpenRouter.

---

## 4. Project layout

```
copilot/
  graph.py         build_graph(): nodes + edges (diagram in the docstring)
  routing.py       every edge decision — made by code, not the model
  policy.py        severity table, noise rule, action allow-list, budget check
  state.py         IncidentState + all typed hand-offs and resume payloads
  agents/          one module per node: intake, commander, triage, diagnosis, runbook,
                   remediation (propose / dry run / approval / execute / verify / rollback),
                   comms, review, escalate
  memory/          short-term (checkpointer, per-node views) + long-term store (incidents, lessons, prefs)
  tools.py         MCP toolbox + clients backed by MCP tool calls
  runtime.py       copilot_runtime(): wires env, OpenRouter, MCP, checkpointer, store, graph
  config.py, deps.py, clients.py
config/            app.yaml, prompts/, permissions.yaml, mcp_servers.yaml
scripts/run_demo.py
tests/
Documents/         design docs
```
