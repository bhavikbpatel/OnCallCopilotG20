# Run the four MCP servers locally and try them

Windows PowerShell, from the repo root. The servers run as four separate processes on ports 8101-8104;
the graph reaches them over HTTP (`config/mcp_servers.yaml`).

## 1. Install (once)

```
py -m venv .venv
.venv\Scripts\activate
py -m pip install -e .
py -c "import fastmcp, langchain_mcp_adapters; print('ok')"
```

`pyproject.toml` pins `fastmcp==2.14.7` and `mcp>=1.27,<2`. fastmcp 4 needs `mcp` 2.x, which breaks
`langchain-mcp-adapters`, so do not upgrade `mcp` by hand. `pip check` should report nothing.

## 2. Start the shop (for telemetry and the flag dry runs)

Follow `shop/astronomy/README.md` (`compose.capstone.yaml`, not `make start`). `runbooks_kb` and `tickets`
need no shop.

## 3. Start the servers: four terminals

In each terminal, activate `.venv` and set the same secret. `ops_actions` and `tickets` must share one token store.

```
$env:APPROVAL_SECRET = "local-dev-secret"
$env:APPROVAL_STATE_DB = "$PWD\approval_state.db"
$env:MCP_TRANSPORT = "http"
py -m mcp_servers.telemetry.server       # 8101
```

Same variables, then one of these per terminal:

```
py -m mcp_servers.runbooks_kb.server     # 8102
py -m mcp_servers.tickets.server         # 8103
py -m mcp_servers.ops_actions.server     # 8104
```

Defaults the servers use: `PROM_URL` http://localhost:9090, `FLAG_UI_URL` per `eval`, `OPENSEARCH_URL`
http://localhost:9200, `JAEGER_URL` http://localhost:16686. If Jaeger is only reachable through the shop's
proxy, try `$env:JAEGER_URL = "http://localhost:8080/jaeger/ui"` (unverified). Without `ACTUATOR_URL`,
restart/scale/rollback use `docker compose` directly (laptop only).

## 4. Try them

In a fifth terminal (same `.venv`, same `APPROVAL_SECRET`):

```
py scripts/try_servers.py
```

It lists which servers are up, how many tools it found, and prints OK or FAIL for each check.

| Check | Needs the shop? | What OK means |
|---|---|---|
| `runbooks_kb.search` | no | passages with IDs to cite |
| `tickets.create_incident`, `append_timeline`, `get_incident` | no | incident `TRY-1` stored with alert name `TryAlert` (re-running warns `incident_already_exists`) |
| `ops_actions.dry_run` | no for restart | a description of what would happen, never a change |
| `telemetry.recent_changes` | yes (flag UI) | the flags currently on |
| `telemetry.search_logs`, `get_traces` | yes (OpenSearch, Jaeger) | lines or traces for payment; a FAIL shows which URL or field is wrong |

Fix a FAIL by reading its message: a wrong URL is an environment variable above; a wrong log field name is
`LOG_SERVICE_FIELD` (default `resource.service.name`).

## 5. With the hooks on

The graph sends every tool call, code-driven or model-driven, through one `Hooks` per incident (`copilot/toolctx.py`,
`run_incident`); there is no switch to set. `hooks.tool_calls()` is the full tool log. For the viva demos that
need no servers: `py scripts/demo_approval.py` and `py scripts/demo_attacks.py`.

## Problems

* **Port already in use:** set `$env:MCP_PORT` for that server and the matching `MCP_*_URL` for the graph.
* **`approval_not_configured` or every token refused:** `APPROVAL_SECRET` differs between terminals or is missing.
* **`ImportError ... RequestContext`:** `mcp` was upgraded to 2.x. Run `py -m pip install -e .` again.
* **`FastMCP server support is not installed`:** `py -m pip install "fastmcp==2.14.7"`.
