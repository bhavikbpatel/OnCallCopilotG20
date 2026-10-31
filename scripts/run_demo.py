"""Run the incident graph end to end with real LLM calls against a stub shop (no MCP servers needed).

    python scripts/run_demo.py                 # you approve / edit / reject in the terminal
    python scripts/run_demo.py --auto          # auto-approve everything
    python scripts/run_demo.py --no-recovery   # the fix "does not help" → rollback path

The stub shop simulates the paymentFailure fault: payment errors after a flag change, cart healthy.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.types import Command  # noqa: E402

from copilot.clients import ServiceHealth  # noqa: E402
from copilot.config import ROOT, load_settings  # noqa: E402
from copilot.deps import CopilotDeps  # noqa: E402
from copilot.graph import build_graph  # noqa: E402
from copilot.memory import make_checkpointer, make_store  # noqa: E402
from copilot.state import Alert, DryRunResult, SetFlag  # noqa: E402

NOW = datetime.now(timezone.utc)


# ---------- stub shop: telemetry tools for the Diagnosis agent ----------

@tool
def query_promql(query: str, start: str = "", end: str = "", step: str = "30s") -> str:
    """Run a PromQL range query. Returns series with labels and values."""
    q = query.lower()
    if "cart" in q or "valkey" in q:
        return json.dumps({"evidence_id": "promql:cart_errors:5m", "series": [
            {"labels": {"service": "cart"}, "values": [[1, "0.001"], [2, "0.001"]]}]})
    if "payment" in q:
        return json.dumps({"evidence_id": "promql:payment_errors:5m", "series": [
            {"labels": {"service": "payment"}, "values": [[1, "0.001"], [2, "0.31"]]}]})
    return json.dumps({"evidence_id": "promql:checkout_errors:5m", "series": [
        {"labels": {"service": "checkout"}, "values": [[1, "0.12"]]}]})


@tool
def recent_changes(since: str = "") -> str:
    """Feature-flag changes and deploys since a time."""
    return json.dumps({"evidence_id": "changes:30m", "changes": [
        {"kind": "flag", "flag": "paymentFailure", "variant": "on", "at": NOW.isoformat()}]})


@tool
def get_traces(service: str, status: str = "error", start: str = "", end: str = "", limit: int = 20) -> str:
    """Error trace summaries for a service."""
    if service == "payment":
        return json.dumps({"traces": [{"trace_id": "tr-9f1", "root": "checkout", "error_span": "payment.Charge",
                                       "message": "payment failed (flag paymentFailure)"}]})
    return json.dumps({"traces": []})


STUB_TOOLS = [query_promql, recent_changes, get_traces]


# ---------- stub shop: clients used by code-driven steps ----------

class StubTelemetry:
    def __init__(self, recovers: bool):
        self.recovers, self.fixed = recovers, False

    async def service_health(self, window="5m"):
        return [ServiceHealth(service="checkout", error_rate=0.12), ServiceHealth(service="payment", error_rate=0.31),
                ServiceHealth(service="cart", error_rate=0.001)]

    async def recent_changes(self, since):
        return [{"kind": "flag", "flag": "paymentFailure", "at": NOW}]

    async def query_scalar(self, query):
        return 0.001 if (self.fixed and self.recovers) else 0.12


class StubRunbooks:
    async def search(self, *args, **kwargs):
        return []


class StubOps:
    def __init__(self, telemetry: StubTelemetry):
        self.telemetry = telemetry

    async def dry_run(self, action):
        undo = None
        if action.tool == "set_flag":
            undo = SetFlag(flag=action.flag, variant="on" if action.variant == "off" else "off")
        return DryRunResult(would_do=action.model_dump_json(), expected_effect="see action", rollback_action=undo)

    async def execute(self, action, token):
        if action.tool == "set_flag" and action.flag == "paymentFailure":
            self.telemetry.fixed = action.variant == "off"
        print(f"   [shop] executed {action.model_dump_json()} with token {token}")
        return {"ok": True}


class StubTickets:
    async def create_incident(self, *args): ...
    async def append_timeline(self, *args): ...
    async def save_postmortem(self, *args): ...


# ---------- human in the loop ----------

def ask_approval(payload: dict, auto: bool) -> dict:
    print(f"\n⏸  APPROVAL needed ({payload['purpose']})")
    print(f"   action:          {json.dumps(payload['action'])}")
    print(f"   expected effect: {payload['expected_effect']}")
    print(f"   rollback plan:   {json.dumps(payload['rollback_plan'])}")
    print(f"   payload hash:    {payload['payload_hash'][:16]}…")
    if auto:
        print("   → auto-approve")
        return {"decision": "approve", "token": f"tok-{uuid.uuid4().hex[:8]}", "decided_by": "demo"}
    choice = input("   [a]pprove / [e]dit / [r]eject? ").strip().lower()[:1]
    if choice == "e":
        edited = json.loads(input('   new action JSON, e.g. {"tool": "restart_service", "service": "cart"}: '))
        return {"decision": "edit", "edited_action": edited, "decided_by": "demo"}
    if choice == "r":
        return {"decision": "reject", "reason": input("   reason: "), "decided_by": "demo"}
    return {"decision": "approve", "token": f"tok-{uuid.uuid4().hex[:8]}", "decided_by": "demo"}


def ask_review(payload: dict, auto: bool) -> dict:
    pm = payload["postmortem"]
    print("\n⏸  REVIEW postmortem")
    print(f"   root cause: {pm['root_cause']}")
    for i, lesson in enumerate(pm["lessons_proposed"]):
        print(f"   lesson #{i}: {lesson['text']}")
    if auto:
        return {"accept_postmortem": True, "reviewed_by": "demo",
                "lessons": [{"index": i, "decision": "accept"} for i in range(len(pm["lessons_proposed"]))]}
    accept = input("   accept postmortem? [Y/n] ").strip().lower() != "n"
    keep = input("   lesson numbers to store (comma separated, blank = none): ").strip()
    picked = [int(x) for x in keep.split(",") if x.strip().isdigit()]
    return {"accept_postmortem": accept, "reviewed_by": "demo",
            "lessons": [{"index": i, "decision": "accept"} for i in picked]}


# ---------- main ----------

async def main(args: argparse.Namespace) -> None:
    load_dotenv(ROOT / ".env")
    settings = load_settings().model_copy(update={"verify_poll_s": 1, "verify_window_s": 3})
    telemetry = StubTelemetry(recovers=not args.no_recovery)

    async with make_checkpointer() as saver, make_store() as store:
        deps = CopilotDeps(settings=settings, telemetry=telemetry, runbooks=StubRunbooks(), ops=StubOps(telemetry),
                           tickets=StubTickets(), store=store, tools_factory=lambda agent: STUB_TOOLS)
        graph = build_graph(deps, checkpointer=saver, store=store)
        config = {"configurable": {"thread_id": f"INC-{uuid.uuid4().hex[:6]}", "engineer_id": "demo"}}
        alert = Alert(alert_name="CheckoutErrorRateHigh", service="checkout", signal="error_rate",
                      summary="Checkout error rate above 5% for 5 minutes", query="checkout_error_rate_5m",
                      threshold=0.05, value=0.12, fired_at=NOW, fingerprint="demo")

        inp: dict | Command = {"alert": alert}
        while True:
            pending = None
            async for chunk in graph.astream(inp, config, stream_mode="updates"):
                for node, update in chunk.items():
                    if node == "__interrupt__":
                        pending = update[0].value
                    else:
                        for ev in (update or {}).get("timeline", []):
                            print(f"[{ev.node}] {ev.text}")
            if pending is None:
                break
            decide = ask_approval if pending["kind"] == "approval" else ask_review
            inp = Command(resume=decide(pending, args.auto))

        state = graph.get_state(config).values
        print(f"\nfinal status: {state['final_status']}"
              + (f" ({state['escalation_reason']})" if state.get("escalation_reason") else ""))
        if state.get("status_note"):
            print(f"status note: {state['status_note'].text}")
        print(f"budget used: {state['budget'].total}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--auto", action="store_true", help="auto-approve every pause")
    parser.add_argument("--no-recovery", action="store_true", help="the fix does not help → rollback path")
    asyncio.run(main(parser.parse_args()))
