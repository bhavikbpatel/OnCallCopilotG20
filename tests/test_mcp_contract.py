"""The graph's real toolbox and clients against the real tickets and runbooks_kb MCP servers (stdio), through the hooks."""
import asyncio
import json
import sys
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastmcp")

from copilot import toolctx  # noqa: E402
from copilot.config import ROOT  # noqa: E402
from copilot.guardrails import GuardConfig  # noqa: E402
from copilot.hooks import Hooks, load_permissions  # noqa: E402
from copilot.state import Alert  # noqa: E402
from copilot.tools import McpRunbooks, McpTickets, McpToolbox  # noqa: E402
from eval.schemas.result import ToolCall  # noqa: E402


def stdio(module, env):
    return {"transport": "stdio", "command": sys.executable, "args": ["-m", module], "cwd": str(ROOT), "env": env}


def test_real_servers_through_toolbox_and_hooks(tmp_path, monkeypatch):
    env = {"APPROVAL_SECRET": "test-secret-0123456789abcdef", "INCIDENT_DB": str(tmp_path / "incidents.db"),
           "PYTHONPATH": str(ROOT), "PATH": __import__("os").environ.get("PATH", "")}
    servers = tmp_path / "mcp_servers.yaml"
    servers.write_text(json.dumps({"servers": {"tickets": stdio("mcp_servers.tickets.server", env),
                                               "runbooks_kb": stdio("mcp_servers.runbooks_kb.server", env)}}))
    alert = Alert(alert_name="CheckoutErrorRateHigh", service="checkout", signal="error_rate", summary="errors up",
                  query="q", threshold=0.05, value=0.4, fired_at=datetime.now(timezone.utc), fingerprint="f")
    hooks = Hooks(permissions=load_permissions(str(ROOT / "config" / "permissions.yaml")), max_tool_calls=20,
                  config=GuardConfig())

    async def go():
        box = await McpToolbox.connect(servers, ROOT / "config" / "permissions.yaml")
        with toolctx.scope(hooks=hooks, agent="triage", role="triage"):
            await McpTickets(box).create_incident("inc-contract-1", alert, "SEV2")
        with toolctx.scope(hooks=hooks, agent="runbook", role="runbook"):
            passages = await McpRunbooks(box).search("payment errors after a flag change", service="payment", k=3)
            model_tool = next(t for t in box.for_agent("runbook") if t.name == "get")
            got = json.loads(await model_tool.ainvoke({"runbook_id": passages[0].doc_id}))
        return passages, got

    passages, got = asyncio.run(go())
    assert passages and all(p.doc_id and p.text for p in passages)
    assert any(p.fix for p in passages)                                  # typed fixes reach the client (C0 uses them)
    assert got["ok"] and got["result"]["runbook_id"] == passages[0].doc_id
    log = hooks.tool_calls()
    assert [(e["agent_role"], e["tool"], e["driver"]) for e in log] == \
        [("triage", "create_incident", "model"), ("runbook", "search", "model"), ("runbook", "get", "model")]
    assert all(e["ok"] and not e["blocked"] for e in log)
    for e in log:
        ToolCall.model_validate(e)
