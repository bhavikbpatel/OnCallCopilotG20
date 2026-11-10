"""The engineer's console without the page (Person 5): firing shop alerts in, one incident through the copilot
graph with live progress, and the two pauses (approval, postmortem review) answered by a person.

No Streamlit here, so it can be tested; ui/app.py draws it.

    alerts = firing_alerts("http://localhost:9090")          # the shop's alert rules that are firing now
    session = IncidentSession(alerts[0]["alert"])            # runs on its own thread and event loop
    session.view()                                           # status, state values, the pause waiting, errors
    session.answer(approval_answer(session.view()["pending"], "approve", engineer="uppili"))

The graph is driven the way copilot/graph.py documents for the incident API (stream, then answer each pause
with Command(resume=...)), on copilot.runtime.copilot_runtime(): the same MCP servers, checkpointer and tracing
as the incident API. Every tool call goes through one `Hooks` per incident (permissions, approval check, budget,
loops, injection filter, log), set the way copilot.run.run_incident sets it (copilot.toolctx). The approval token
is minted inside the graph; the console only sends the engineer's decision, bound to the payload hash the
engineer was shown.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import threading
import urllib.parse
import urllib.request
from contextlib import AbstractAsyncContextManager
from typing import Any, Callable, Optional

from copilot.state import Alert, ApprovalDecision, ReviewDecision
from eval.probes.faults import MOVE_THRESHOLDS
from eval.telemetry.signals import promql

SHOP_GROUP = "shop"     # infra/prometheus/rules/shop.yml; the copilot's own alerts go to the team, never here
# How infra/prometheus/build_rules.py writes a shop rule (tests/test_ui.py checks shop.yml still matches):
# error_rate / error_count: "(query) > absolute"; the other signals: "(query) > (baseline) + absolute and
# (query) > (baseline) * (1 + relative)", the baseline being the same signal 15-45 minutes earlier.
RULE_WINDOW = "5m"
ABSOLUTE_SIGNALS = ("error_rate", "error_count")

Fetch = Callable[[str], Any]
RuntimeFactory = Callable[[], AbstractAsyncContextManager]


def _get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.load(resp)


def rule_query(signal: str, service: str) -> str:
    return promql(signal, service, RULE_WINDOW)


def baseline_query(signal: str, service: str) -> str:
    return f"avg_over_time(({rule_query(signal, service)})[30m:1m] offset 15m)"


def threshold_for(signal: str, baseline: Optional[float] = None) -> float:
    """The level the rule fired above: fixed for errors, from the earlier level for the other signals."""
    absolute, relative = MOVE_THRESHOLDS[signal]
    if signal in ABSOLUTE_SIGNALS:
        return float(absolute)
    if baseline is None:
        raise ValueError(f"{signal}: the rule compares with the signal's earlier level, which is unknown")
    return max(baseline + absolute, baseline * (1 + relative))


def to_alert(rule: dict, alert: dict, baseline: Optional[float] = None) -> Alert:
    """One alert from Prometheus's /api/v1/rules, as the graph's Alert (query and threshold as the rule has them)."""
    labels = {**rule.get("labels", {}), **alert.get("labels", {})}
    annotations = {**rule.get("annotations", {}), **alert.get("annotations", {})}
    fired_at = alert.get("activeAt") or dt.datetime.now(dt.timezone.utc).isoformat()
    service, signal = labels.get("service", ""), labels.get("signal", "")
    return Alert(
        alert_name=rule["name"],
        service=service,
        signal=signal,
        summary=annotations.get("summary") or rule["name"],
        query=rule_query(signal, service),
        threshold=threshold_for(signal, baseline),
        value=float(alert.get("value", "nan")),
        fired_at=fired_at,
        fingerprint=hashlib.sha256(f"{rule['name']}|{service}|{fired_at}".encode()).hexdigest()[:16],
    )


def _instant(prom_url: str, query: str, at: Optional[str], fetch: Fetch) -> Optional[float]:
    url = f"{prom_url.rstrip('/')}/api/v1/query?query={urllib.parse.quote(query)}"
    if at:
        url += f"&time={urllib.parse.quote(at)}"
    result = fetch(url).get("data", {}).get("result", [])
    return float(result[0]["value"][1]) if result else None


def firing_alerts(prom_url: str, fetch: Fetch = _get_json) -> list[dict]:
    """The shop's alerts that are firing or pending, firing first: {alert, state, severity, problem}.

    `alert` is None (and `problem` says why) when an alert cannot be turned into an incident."""
    data = fetch(prom_url.rstrip("/") + "/api/v1/rules?type=alert")
    out = []
    for group in data.get("data", {}).get("groups", []):
        if group.get("name") != SHOP_GROUP:
            continue
        for rule in group.get("rules", []):
            for alert in rule.get("alerts", []):
                if alert.get("state") not in ("firing", "pending"):
                    continue
                labels = {**rule.get("labels", {}), **alert.get("labels", {})}
                item = {"name": rule["name"], "state": alert["state"], "severity": labels.get("severity", ""),
                        "alert": None, "problem": None}
                try:
                    baseline = None
                    if labels.get("signal") not in ABSOLUTE_SIGNALS:
                        baseline = _instant(prom_url, baseline_query(labels["signal"], labels["service"]),
                                            alert.get("activeAt"), fetch)
                    item["alert"] = to_alert(rule, alert, baseline)
                except Exception as exc:  # noqa: BLE001 - listed with the reason, not dropped
                    item["problem"] = f"{type(exc).__name__}: {exc}"
                out.append(item)
    return sorted(out, key=lambda a: (a["state"] != "firing", a["name"]))


def approval_answer(payload: dict, decision: str, engineer: str, reason: str = "",
                    edited_action: Optional[dict] = None) -> dict:
    """The resume value for the approval pause. An approval carries the hash of the exact action shown."""
    fields: dict[str, Any] = {"decision": decision, "decided_by": engineer, "reason": reason}
    if decision == "approve":
        fields["payload_hash"] = payload["payload_hash"]
    elif decision == "edit":
        if not edited_action:
            raise ValueError("an edit needs the edited action")
        fields["edited_action"] = edited_action
    elif decision == "reject":
        fields["reason"] = reason or "rejected by the on-call engineer"
    else:
        raise ValueError(f"unknown decision {decision!r}")
    return ApprovalDecision.model_validate(fields).model_dump(mode="json")


def review_answer(accept: bool, engineer: str, edited_root_cause: Optional[str] = None) -> dict:
    """The resume value for the postmortem review pause."""
    return ReviewDecision(accept_postmortem=accept, edited_root_cause=edited_root_cause or None,
                          reviewed_by=engineer).model_dump(mode="json")


def timeline_rows(values: dict) -> list[dict]:
    """The incident's timeline, oldest first, as table rows."""
    rows = []
    for e in values.get("timeline") or []:
        at = e.at.astimezone() if getattr(e.at, "tzinfo", None) else e.at
        rows.append({"time": at.strftime("%H:%M:%S"), "step": e.node, "kind": e.kind, "what": e.text})
    return rows


def _default_runtime() -> AbstractAsyncContextManager:
    from copilot.runtime import copilot_runtime
    return copilot_runtime()


def incident_hooks(settings: Any) -> Any:
    """One incident's hooks, built as copilot.run.run_incident builds them: the permissions file, the model's
    tool-call budget (code-driven calls are logged, not budgeted), the configured defences."""
    from copilot.hooks import Hooks, load_permissions
    return Hooks(permissions=load_permissions(str(settings.permissions_path)),
                 max_tool_calls=settings.limits.tool_calls)


def _event_loop() -> asyncio.AbstractEventLoop:
    """The incident's own loop, chosen as copilot.run.CopilotAgent chooses it: with Postgres on Windows, a selector
    loop (psycopg refuses Windows' default Proactor loop). The DSN may be only in .env (10 Nov dry run: the console
    failed every incident with "Psycopg cannot use the 'ProactorEventLoop'")."""
    from copilot.memory.backends import new_event_loop
    from copilot.run import run_env

    return new_event_loop(run_env().get("POSTGRES_DSN") or None)

class IncidentSession:
    """One incident, driven on its own thread and event loop so a page can poll it and answer its pauses.

    status: starting -> running -> awaiting_approval / awaiting_review -> running ... -> done (or error).
    """

    def __init__(self, alert: Alert, *, engineer_id: Optional[str] = None, incident_id: Optional[str] = None,
                 runtime: RuntimeFactory = _default_runtime):
        self.alert = alert
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.incident_id = incident_id or f"ui-{alert.alert_name}-{stamp}"
        self.engineer_id = engineer_id
        self._runtime = runtime
        self.hooks: Any = None           # this incident's copilot.hooks.Hooks, once it starts
        self._lock = threading.Lock()
        self._status = "starting"
        self._values: dict[str, Any] = {}
        self._pending: Optional[dict] = None
        self._error: Optional[str] = None
        self._answer: Optional[asyncio.Future] = None
        self._loop = _event_loop()
        self._thread = threading.Thread(target=self._main, daemon=True, name=f"incident-{self.incident_id}")
        self._thread.start()

    # ------------------------------------------------------------ for the page

    def view(self) -> dict:
        with self._lock:
            return {"incident_id": self.incident_id, "alert": self.alert, "status": self._status,
                    "values": dict(self._values), "pending": self._pending, "error": self._error}

    def answer(self, value: dict) -> None:
        """Answer the pause that is waiting (approval_answer / review_answer)."""
        with self._lock:
            future = self._answer if self._pending is not None else None
            if future is not None:          # the page redraws "running" at once, so the pause is never answered twice
                self._pending, self._status = None, "running"
        if future is None:
            raise RuntimeError("nothing is waiting for an answer")
        self._loop.call_soon_threadsafe(lambda: future.done() or future.set_result(value))

    def wait(self, statuses: tuple[str, ...], timeout: float = 30.0) -> str:
        """Block until the status is one of `statuses` (tests, scripts). Returns the status."""
        deadline = dt.datetime.now().timestamp() + timeout
        while dt.datetime.now().timestamp() < deadline:
            status = self.view()["status"]
            if status in statuses or status == "error":
                return status
            self._thread.join(0.05)
        raise TimeoutError(f"still {self.view()['status']} after {timeout:g} s")

    # ------------------------------------------------------------ the incident

    def _set(self, **fields: Any) -> None:
        with self._lock:
            for name, value in fields.items():
                setattr(self, f"_{name}", value)

    def _main(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        except BaseException as exc:  # shown on the page; never raised into Streamlit
            self._set(status="error", pending=None, error=f"{type(exc).__name__}: {exc}")

    async def _run(self) -> None:
        from langgraph.types import Command

        from copilot import toolctx

        async with self._runtime() as rt:
            graph = rt.graph
            config = {"configurable": {"thread_id": self.incident_id, "run_id": None,
                                       "engineer_id": self.engineer_id}, "recursion_limit": 200}
            inp: Any = {"alert": self.alert}
            self.hooks = incident_hooks(rt.deps.settings)
            with toolctx.scope(hooks=self.hooks):      # the graph's node tasks copy this context
                self._set(status="running")
                while True:
                    async for _ in graph.astream(inp, config, stream_mode=["updates", "custom"]):
                        snap = await graph.aget_state(config)
                        self._set(values=dict(snap.values))
                    snap = await graph.aget_state(config)
                    self._set(values=dict(snap.values))
                    pending = [i for t in snap.tasks for i in (t.interrupts or ())]
                    if not pending:
                        self._set(status="done")
                        return
                    payload = pending[0].value
                    kind = "approval" if payload.get("kind") == "approval" else "review"
                    future = self._loop.create_future()
                    self._set(answer=future, pending=payload, status=f"awaiting_{kind}")
                    value = await future
                    self._set(answer=None, pending=None, status="running")
                    inp = Command(resume=value)
