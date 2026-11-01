# Person 3: tools, hooks, approval gate, guardrails

```
tools/        MCP servers (FastMCP): telemetry, runbooks_kb, tickets, ops_actions; common.py, approval.py
control/      permissions.yaml, hooks.py, gate.py, guardrails.py
docs/threat-model.md
```

Run the tests from the repository root (Python 3.12+; needs the packages in `eval/requirements.txt`, plus `fastmcp` for serving and for the one marking test):

```bash
python -m pytest tools/tests -q
python -m tools.ops_actions.server      # stdio MCP server; same for telemetry, runbooks_kb, tickets
```

The PR-review commands (`pytest tests`, `cd eval && pytest`) do not include this folder. When `pyproject.toml` lands, add `testpaths = ["tests", "tools/tests"]` and run it in CI.

## Tools

| Server | Tool | Kind | Notes |
|---|---|---|---|
| telemetry | `query_promql(query, start?, end?, step?)` | read | allow-listed metrics, max 1 h, evidence_id |
| telemetry | `search_logs(service, level?, text?, start?, end?, limit<=50)` | read | OpenSearch; redacted; untrusted |
| telemetry | `get_traces(service, status="error", start?, end?, limit<=20)` | read | Jaeger; failed-span summary |
| telemetry | `recent_changes(since="15m")` | read | flags currently not `off` |
| runbooks-kb | `search(query, service?, symptom?, k<=10)`, `get(runbook_id)` | read | wraps `eval.knowledge` |
| tickets | `create_incident`, `append_timeline`, `save_postmortem` | write (internal) | no approval |
| tickets | `get_incident`, `draft_status` | read | `draft_status` returns the payload and hash to approve |
| tickets | `publish_status(incident_id, text, approval_token)` | write (external) | gated |
| ops-actions | `dry_run(tool, ...)` | read | returns `payload_hash`, `would_do`, `current_state`, `expected_effect`, `rollback_plan`, `rollback_action` |
| ops-actions | `set_flag(flag, variant)`, `restart_service(service)`, `scale_service(service, replicas)`, `rollback(service, to_version)` | write | each also takes `approval_token` |

Every tool returns `{"ok": true, "data": ...}` or `{"ok": false, "error": {"code", "message"}}`.

## How a fix flows (for Persons 2, 4 and 5)

1. `dry_run({"tool": "set_flag", "flag": "paymentFailure", "variant": "off"})` -> `payload_hash`, `rollback_action`.
2. Gate: `proposal = control.gate.propose(action, expected_effect=..., rollback_action=...)`, `interrupt(proposal)`, then `outcome = control.gate.resolve(proposal, decision)`.
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

The OpenSearch and Jaeger endpoints and log field names, `DEMO_VERSION` as the image variable, and the FastMCP wrappers (`build_server`, the read/write `tags`/`annotations`, `MCP_TRANSPORT=http`) were written from the docs and tested only through the plain Python classes. Run each server once against the running shop and fix field names if needed, and run `pytest tools/tests` with `fastmcp` installed so the marking test is not skipped.
