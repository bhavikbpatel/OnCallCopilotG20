"""Run the incident graph end to end with real LLM calls against a stub shop (no MCP servers needed).

    python scripts/run_demo.py                     # you approve / edit / reject in the terminal
    python scripts/run_demo.py --auto              # auto-approve everything
    python scripts/run_demo.py --no-recovery       # the fix "does not help" → follow-up / rollback path
    python scripts/run_demo.py --scenario email    # two-step fix: flag off, then restart to free memory

Scenarios: `payment` (paymentFailure at 50%: payment errors, checkout alert) and `email`
(emailMemoryLeak at 100x: email memory keeps growing; turning the flag off stops growth, restart frees it).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.types import Command  # noqa: E402

from copilot.clients import RecoveryCount, ServiceHealth, ToolError  # noqa: E402
from copilot.config import ROOT, load_settings  # noqa: E402
from copilot.deps import CopilotDeps  # noqa: E402
from copilot.graph import build_graph  # noqa: E402
from copilot.memory import make_checkpointer, make_store  # noqa: E402
from copilot.state import Alert, DryRunResult, SetFlag, payload_hash  # noqa: E402
from mcp_servers.common.approval import check_token  # noqa: E402

NOW = datetime.now(timezone.utc)
SCENARIOS = {
    "payment": {"flag": "paymentFailure", "variant": "50%", "service": "checkout", "signal": "error_rate",
                "alert": "CheckoutErrorRateHigh", "summary": "Checkout error rate above 5% for 5 minutes",
                "threshold": 0.05, "bad": 0.12, "good": 0.0},
    "email": {"flag": "emailMemoryLeak", "variant": "100x", "service": "email", "signal": "memory_mb",
              "alert": "EmailMemoryHigh", "summary": "email container memory above 300 MB and growing",
              "threshold": 300.0, "bad": 620.0, "good": 140.0},
}
SC = SCENARIOS["payment"]


def _series(name: str, labels: dict, values: list) -> str:
    return json.dumps({"evidence_id": name, "series": [{"labels": labels, "values": values}]})


# ---------- stub shop: telemetry tools for the Diagnosis agent ----------

@tool
def query_promql(query: str, start: str = "", end: str = "", step: str = "30s") -> str:
    """Run a PromQL range query. Returns series with labels and values."""
    q = query.lower()
    if SC["flag"] == "emailMemoryLeak":
        if "email" in q or "memory" in q:
            return _series("promql:email_memory:15m", {"container_name": "email"}, [[1, "210"], [2, "430"], [3, "620"]])
        return _series("promql:errors:5m", {"service_name": "email"}, [[1, "0"], [2, "0"]])
    if "cart" in q or "valkey" in q:
        return _series("promql:cart_errors:5m", {"service_name": "cart"}, [[1, "0.001"], [2, "0.001"]])
    if "payment" in q:
        return _series("promql:payment_errors:5m", {"service_name": "payment"}, [[1, "0.001"], [2, "0.31"]])
    return _series("promql:checkout_errors:5m", {"service_name": "checkout"}, [[1, "0.12"]])


@tool
def recent_changes(since: str = "") -> str:
    """Feature-flag changes and deploys since a time."""
    return json.dumps({"evidence_id": "changes:30m", "changes": [
        {"kind": "flag", "flag": SC["flag"], "variant": SC["variant"], "at": NOW.isoformat()}]})


@tool
def get_traces(service: str, status: str = "error", start: str = "", end: str = "", limit: int = 20) -> str:
    """Error trace summaries for a service."""
    if service == "payment" and SC["flag"] == "paymentFailure":
        return json.dumps({"traces": [{"trace_id": "tr-9f1", "root": "checkout", "error_span": "payment.Charge",
                                       "message": "payment failed (flag paymentFailure)"}]})
    return json.dumps({"traces": []})


STUB_TOOLS = [query_promql, recent_changes, get_traces]


# ---------- stub shop: clients used by code-driven steps ----------

class StubTelemetry:
    def __init__(self, recovers: bool):
        self.recovers, self.flag_off, self.restarted = recovers, False, False

    @property
    def _healthy(self) -> bool:
        if not self.recovers:
            return False
        if SC["flag"] == "emailMemoryLeak":                 # memory only drops once the process restarts
            return self.flag_off and self.restarted
        return self.flag_off

    @property
    def _value(self) -> float:
        return SC["good"] if self._healthy else SC["bad"]

    async def service_health(self, window="5m"):
        if SC["flag"] == "emailMemoryLeak":
            return [ServiceHealth(service="email", error_rate=0.0), ServiceHealth(service="checkout", error_rate=0.0)]
        return [ServiceHealth(service="checkout", error_rate=0.12), ServiceHealth(service="payment", error_rate=0.31),
                ServiceHealth(service="cart", error_rate=0.0)]

    async def recent_changes(self, since):
        return [{"kind": "flag", "flag": SC["flag"], "variant": SC["variant"], "at": NOW}]

    async def query_scalar(self, query):
        return self._value

    async def signal_value(self, signal, service, window="2m"):
        return self._value

    async def errors_since(self, service, since):
        requests = 12.0
        errors = requests * self._value
        return RecoveryCount(errors=errors, requests=requests, error_rate=errors / requests)


class StubRunbooks:
    async def search(self, *args, **kwargs):
        return []


class StubOps:
    def __init__(self, telemetry: StubTelemetry):
        self.telemetry = telemetry
        self.flags = {SC["flag"]: SC["variant"]}            # the injected fault

    async def dry_run(self, action):
        undo = None
        if action.tool == "set_flag":                      # undo = restore the flag's current variant
            undo = SetFlag(flag=action.flag, variant=self.flags.get(action.flag, "off"))
        return DryRunResult(would_do=action.model_dump_json(), expected_effect="see action",
                            current_state={"flags": dict(self.flags)}, rollback_action=undo)

    async def execute(self, action, token):
        code = check_token(payload_hash(action), token)        # the same check as the real ops_actions server
        if code:
            raise ToolError(code, "approval token refused")
        if action.tool == "set_flag":
            self.flags[action.flag] = action.variant
            self.telemetry.flag_off = self.flags.get(SC["flag"]) == "off"
        elif action.tool == "restart_service":
            self.telemetry.restarted = self.telemetry.flag_off   # restarting with the leak still on does not help
        print(f"   [shop] executed {action.model_dump_json()} (token verified)")
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
        return {"decision": "approve", "decided_by": "demo"}
    choice = input("   [a]pprove / [e]dit / [r]eject? ").strip().lower()[:1]
    if choice == "e":
        edited = json.loads(input('   new action JSON, e.g. {"tool": "restart_service", "service": "cart"}: '))
        return {"decision": "edit", "edited_action": edited, "decided_by": "demo"}
    if choice == "r":
        return {"decision": "reject", "reason": input("   reason: "), "decided_by": "demo"}
    return {"decision": "approve", "decided_by": "demo"}       # the gate mints the token


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
    global SC
    SC = SCENARIOS[args.scenario]
    load_dotenv(ROOT / ".env")
    if not os.getenv("APPROVAL_SECRET"):
        os.environ["APPROVAL_DEV_MODE"] = "1"                 # stub shop only: the public dev secret
    settings = load_settings().model_copy(update={"verify_poll_s": 1, "verify_window_s": 3, "verify_window_by_signal": {},
                                                  "verify_initial_delay_s": 0, "verify_settle_s": 0})
    telemetry = StubTelemetry(recovers=not args.no_recovery)

    async with make_checkpointer() as saver, make_store() as store:
        deps = CopilotDeps(settings=settings, telemetry=telemetry, runbooks=StubRunbooks(), ops=StubOps(telemetry),
                           tickets=StubTickets(), store=store, tools_factory=lambda agent: STUB_TOOLS)
        graph = build_graph(deps, checkpointer=saver, store=store)
        config = {"configurable": {"thread_id": f"INC-{uuid.uuid4().hex[:6]}", "engineer_id": "demo"}}
        alert = Alert(alert_name=SC["alert"], service=SC["service"], signal=SC["signal"], summary=SC["summary"],
                      query=f"{SC['service']}_{SC['signal']}", threshold=SC["threshold"], value=SC["bad"],
                      fired_at=NOW, fingerprint="demo")

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
        print(f"shop flags at end: {deps.ops.flags}")
        print(f"budget used: {state['budget'].total}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--auto", action="store_true", help="auto-approve every pause")
    parser.add_argument("--no-recovery", action="store_true", help="the fix does not help → follow-up / rollback path")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="payment")
    asyncio.run(main(parser.parse_args()))
