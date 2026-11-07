# Person 3: MCP servers, hooks, approval helper, guardrails

```
mcp_servers/            FastMCP servers: telemetry, runbooks_kb, tickets, ops_actions
mcp_servers/common/     result envelope, approval.py (tokens), token_store.py (single use), decision.py (propose/resolve)
copilot/hooks.py        pre/post-tool checks (runs inside the agent process)
copilot/guardrails/     injection filter, redaction, GuardConfig
config/permissions.yaml who may call which tool: Person 2's flat `agent: [tools]` lists plus a `_code_driven:` section the hooks read (the toolbox ignores it); the budget is limits.tool_calls in config/app.yaml
tests/mcp_servers/      tests for all of the above
docs/threat-model.md
```

Run the tests from the repository root (Python 3.12+; needs the packages in `eval/requirements.txt`, plus `fastmcp` for serving and for the one marking test):

```bash
python -m pytest tests/mcp_servers -q
python -m mcp_servers.ops_actions.server      # stdio MCP server; same for telemetry, runbooks_kb, tickets
```

These tests live under `tests/`, so plain `pytest tests` runs them. `tests/mcp_servers/conftest.py` puts the repo root and the shared fakes on the path.

## Tools

| Server | Tool | Kind | Notes |
|---|---|---|---|
| telemetry | `query_promql(query, start?, end?, step?)` | read | allow-listed metrics, max 1 h, evidence_id |
| telemetry | `search_logs(service, level?, text?, start?, end?, limit<=50)` | read | OpenSearch; redacted; untrusted |
| telemetry | `get_traces(service, status="error", start?, end?, limit<=20)` | read | Jaeger; failed-span summary |
| telemetry | `recent_changes(since="15m")` | read | flags currently not `off` |
| runbooks_kb | `search(query, service?, symptom?, k<=10)`, `get(runbook_id)` | read | wraps `eval.knowledge` |
| tickets | `create_incident`, `append_timeline`, `save_postmortem` | write (internal) | no approval |
| tickets | `get_incident`, `draft_status` | read | `draft_status` returns the payload and hash to approve |
| tickets | `publish_status(incident_id, text, approval_token)` | write (external) | gated |
| ops_actions | `dry_run(tool, ...)` | read | returns `payload_hash`, `would_do`, `current_state`, `expected_effect`, `rollback_plan`, `rollback_action` |
| ops_actions | `set_flag(flag, variant)`, `restart_service(service)`, `scale_service(service, replicas)`, `rollback(service, to_version)` | write | each also takes `approval_token` |

Every tool returns `{"ok": true, "data": ...}` or `{"ok": false, "error": {"code", "message"}}`.

## How a fix flows (for Persons 2, 4 and 5)

1. `dry_run({"tool": "set_flag", "flag": "paymentFailure", "variant": "off"})` -> `payload_hash`, `rollback_action`.
2. Gate: `proposal = mcp_servers.common.decision.propose(action, expected_effect=..., rollback_action=...)`, `interrupt(proposal)`, then `outcome = mcp_servers.common.decision.resolve(proposal, decision)`.
   `outcome["record"]` goes into `approvals` (no token inside); `outcome["token"]` goes to the write call.
   In evaluation the runner uses `auto_decision(proposal)` with `decided_by="auto"`.
3. Write call: `set_flag(flag, variant, approval_token)`. An edited action has a new hash, so the old token fails with `approval_hash_mismatch`.
4. Undo a fix that did not verify: dry-run the stored `rollback_action`, approve, execute with `purpose="rollback"`.

Route every call through the hooks and export the log:

```python
hooks = Hooks(max_tool_calls=10)                      # 5 / 10 / 20 for E4
result = hooks.call(agent, role, server, tool, args, invoke, purpose="fix")
tool_calls, budget = hooks.tool_calls(), hooks.budget()   # straight into the result line
```

## Environment

| Variable | Meaning |
|---|---|
| `APPROVAL_SECRET` | **Required.** Same value for the gate/runner and every MCP server. Missing = nothing is issued and nothing verifies (fails closed) |
| `APPROVAL_DEV_MODE` | `1` allows a public built-in secret, for local tests only. Never for a demo or evaluation run |
| `APPROVAL_STATE_DB` | SQLite file of spent tokens (default `approval_state.db`); give every server the same path |
| `COPILOT_DEFENCES` | `on`, `off`, or `redaction=off,permissions=on`; overrides `defences:` in `config/app.yaml` (for E2) |
| `COPILOT_CONFIG` | Config file to read `defences:` from (default `config/app.yaml`) |
| `ACTUATOR_URL` | Base URL of the actuator (`mcp_servers/actuator`). Set = `ops_actions` calls its four endpoints (`POST /restart`, `/scale`, `/rollback`, `GET /status?service=`); unset = direct `docker compose` (laptop only) |
| `ACTUATOR_TOKEN` | Sent by `ops_actions` as `Authorization: Bearer <token>`; the actuator needs the same value. Required when the actuator listens beyond this machine |
| `ACTUATOR_HOST`, `ACTUATOR_PORT` | Where the actuator listens (defaults `127.0.0.1`, `8105`). Use `0.0.0.0` for containers to reach it, with a token |
| `MCP_ALLOWLIST` | Pinned servers/tool hashes (default `config/mcp_allowlist.yaml`) |
| `RUNBOOK_HASHES` | Reviewed passage hashes (default `config/runbook_hashes.yaml`). No file = not enforced |
| `MCP_TRANSPORT` | `stdio` (default) or `http`. With http: `MCP_HOST` (default 127.0.0.1; 0.0.0.0 in a container), `MCP_PORT` (defaults 8101 telemetry, 8102 runbooks_kb, 8103 tickets, 8104 ops_actions) |
| `OTEL_DEMO_DIR` | Shop checkout, same as `shop/astronomy/start.sh` (restart, scale, rollback) |
| `CAPSTONE_COMPOSE_OVERRIDE` | Path to `shop/astronomy/compose.capstone.yaml` |
| `ROLLBACK_VERSION_VAR` | Compose variable holding the image version (default `DEMO_VERSION`) |
| `KNOWN_VERSIONS` | Versions `rollback` may target (default: the pinned 3.1.0) |
| `PROM_URL`, `FLAG_UI_URL` | As in `eval` |
| `OPENSEARCH_URL`, `OPENSEARCH_LOG_INDEX`, `JAEGER_URL` | Defaults `http://localhost:9200`, `otel-logs-*`, `http://localhost:16686` |
| `INCIDENT_DB` | SQLite file for tickets (default `incidents.db`) |

Defences switch for E2, in `config/app.yaml` (or `COPILOT_DEFENCES=off`):

```yaml
defences: {injection_filter: true, redaction: true, permissions: true}
```

## Not verified against a live shop yet

The OpenSearch and Jaeger endpoints and log field names, `DEMO_VERSION` as the image variable, and the FastMCP wrappers (`build_server`, the read/write `tags`/`annotations`, `MCP_TRANSPORT=http`) were written from the docs and tested only through the plain Python classes. Run each server once against the running shop and fix field names if needed, and run `pytest tests/mcp_servers` with `fastmcp` installed so the marking test is not skipped.

## The actuator (restart, scale, rollback)
`mcp_servers/actuator/server.py` is the one process that runs Docker for the copilot. `ops_actions` calls it over four fixed endpoints (`POST /restart`, `/scale`, `/rollback`, `GET /status?service=`; plus `GET /health`), and it runs `docker compose` for the shop with the same code as the laptop path (`ComposeExecutor`). It does not trust its caller: every request is checked against the same allow-list as `ops_actions` (protected services, replicas 1 to 3, known versions), takes no extra fields, and runs as a fixed argument list, never a shell. It listens on `127.0.0.1:8105` by default and refuses to start on any other address without `ACTUATOR_TOKEN`. It runs on the machine that has the shop checkout (`OTEL_DEMO_DIR`), not in a container, so no container holds the Docker socket. `mcp-ops` in `infra/compose/copilot.yaml` reaches it at `host.docker.internal:8105`. Not running means the three actions return an `executor_error` and change nothing. Start it: `docs/run-mcp-servers-locally.md`. Tests: `tests/mcp_servers/test_actuator.py` (the whole path with Docker replaced by a recording stub; not yet run against a real Docker).

## Compose names, ports and images (for Person 5)

| Compose service | Module | HTTP port |
|---|---|---|
| `mcp-telemetry` | `mcp_servers.telemetry.server` | 8101 |
| `mcp-runbooks` | `mcp_servers.runbooks_kb.server` | 8102 |
| `mcp-tickets` | `mcp_servers.tickets.server` | 8103 |
| `mcp-ops` | `mcp_servers.ops_actions.server` | 8104 |

Run each with `MCP_TRANSPORT=http MCP_HOST=0.0.0.0`. All of them need `APPROVAL_SECRET`, and `mcp-ops` and `mcp-tickets` should share one volume for `APPROVAL_STATE_DB`. The servers import `eval.schemas`, `eval.injector` and `eval.knowledge`, so the image needs that code but must **not** include `eval/heldout/`: list `eval/heldout` in `.dockerignore`.

## Typed outputs, pinning and pins

* Every tool result is validated against a Pydantic model in `mcp_servers/common/models.py`; a wrong shape returns `output_schema_error`.
* `python -m mcp_servers.common.pinning --write` regenerates `config/mcp_allowlist.yaml` after a reviewed change to a tool docstring (a test fails if they drift). It records docstrings with a stand-in for FastMCP, so confirm once against the real server list.
* After a person reviews the runbooks, `python -m mcp_servers.runbooks_kb.server --write-pins` signs them; commit `config/runbook_hashes.yaml`. Until then nothing is withheld.

## What `copilot/tools.py` (Person 2) expects, and what the servers return

| Call from the graph | Server answer |
|---|---|
| `telemetry.query_promql {query,start,end,step}` (ISO times) | `result.series[] = {labels, values: [[unix_ts, value], ...]}` (value `null` when NaN/inf) |
| `telemetry.recent_changes {since}` (ISO time) | `result.changes[] = {flag, variant, at (ISO), first_seen_age_s, evidence_id}` |
| `runbooks_kb.search {query,k,service,symptom}` | `result.passages[]` with `score` (rank-derived `1/(1+rank)`, not a model score) |
| `ops_actions.dry_run {tool,...flat action}` | `result.{payload_hash, action, would_do, current_state, expected_effect, rollback_action}` |
| `ops_actions.<tool> {...args, approval_token}` | `result.{action_id, payload_hash, ...}` |
| `tickets.create_incident {incident_id, alert, severity}` | `result.incident_id` (the id the graph sent) and `result.warnings`. Reads `alert.alert_name` and `alert.service`. A repeat call changes nothing (`incident_already_exists`); an alert with no name is refused (`missing_alert_name`); an unrecognised service opens the incident with the service empty and `service_not_recognised:<x>` |
| `tickets.append_timeline {incident_id, at, text}` | `result.incident_id`. `at` is ISO-8601 or epoch seconds; missing means now; unreadable is refused (`bad_argument`) |
| `tickets.save_postmortem {incident_id, postmortem}` | `result.incident_id` |

Failures are `{ok: false, error: {code, message}}`, which her `_parse` turns into `ToolError`.

## Hooks on the graph's tool calls
Every tool call, code-driven or model-driven, goes through one `Hooks` per incident (`Hooks.acall`): role permission, approval token, budget and loop checks before; injection filter, redaction and logging after. The routing is Person 2's (`copilot/toolctx.py`: `McpToolbox.call`, `for_agent`, the node wrapper in `graph.py`, `run_incident`); the checks are in `copilot/hooks.py`. Calls the graph makes itself (verify polling) are logged with `driver: "code"` and are not budgeted. The defences follow `defences:` in `config/app.yaml` or `COPILOT_DEFENCES`. To run the servers locally and try them, see `docs/run-mcp-servers-locally.md`.

## Runbook pins
`config/runbook_hashes.yaml` holds the sha256 of every reviewed runbook/postmortem passage; a passage that differs or is not listed is withheld (`pin_check: on`). A test fails if the runbooks change without new pins. After a person reviews the change: `py -m mcp_servers.runbooks_kb.server --write-pins`, then commit the file.

## Failing loudly
Tools refuse bad input with a named error instead of guessing (no placeholder alert names, no silently replaced timestamps). The one deliberate exception is alert data from outside (an unknown service): the incident still opens and the result carries a warning. Log-sink failures are counted in `Hooks.costs()["sink_errors"]`. Severity is stored in the eval form (`SEV-2`); the graph's `SEV2` is accepted and normalised.

## Not granted yet
No `memory.*` tool is in `config/permissions.yaml`: long-term memory runs in-process in the graph and no memory MCP server exists. Add the tools and their allow-list entries together when one does.
