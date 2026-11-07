"""On-Call Copilot console (Person 5): firing alert -> live investigation and plan -> approval of the exact
action -> postmortem review -> final status.

    streamlit run ui/app.py          # http://localhost:8501; needs the stack (docker compose up -d) and .env

One incident at a time per console process, shared by every browser tab that opens it. The logic is in
ui/incident.py; this file only draws it.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))     # `streamlit run` from anywhere
load_dotenv(ROOT / ".env")        # PROM_URL, LANGSMITH_*, COPILOT_ENGINEER; the runtime reads the rest

from ui.incident import IncidentSession, approval_answer, firing_alerts, review_answer, timeline_rows  # noqa: E402

PROM_URL = os.getenv("PROM_URL", "http://localhost:9090")
SHOP_URL = os.getenv("SHOP_URL", "http://localhost:8080")
REFRESH_S = 2
ACTIVE = ("starting", "running")

st.set_page_config(page_title="On-Call Copilot", layout="wide")


@st.cache_resource
def console() -> dict:
    """The console's one incident slot, shared by every tab of this server."""
    return {"session": None}


slot = console()

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("On-Call Copilot")
    engineer = st.text_input("On-call engineer", value=os.getenv("COPILOT_ENGINEER") or "on-call")
    st.caption(f"Alerts from Prometheus at {PROM_URL}")
    st.markdown(f"[Grafana]({SHOP_URL}/grafana) · [Jaeger]({SHOP_URL}/jaeger/ui) · [Flags]({SHOP_URL}/feature)"
                " · [Alertmanager](http://localhost:9093)")
    if os.getenv("LANGSMITH_TRACING", "").lower() == "true":
        st.caption(f"Tracing to LangSmith project `{os.getenv('LANGSMITH_PROJECT') or 'oncall-copilot'}`")
    session: IncidentSession | None = slot["session"]
    if session is not None and session.view()["status"] in ("done", "error"):
        if st.button("Close this incident"):
            slot["session"] = None
            st.rerun()

# ---------------------------------------------------------------- alerts
st.title("On-Call Copilot")
session = slot["session"]
busy = session is not None and session.view()["status"] not in ("done", "error")

st.subheader("Firing alerts")
st.button("Refresh alerts")
try:
    alerts = firing_alerts(PROM_URL)
except Exception as exc:  # noqa: BLE001 - shown, not raised
    alerts = []
    st.error(f"Cannot read alerts from Prometheus at {PROM_URL}: {exc}")
if not alerts:
    st.info("No shop alert is firing. To try the copilot, turn a fault on in the flag UI "
            f"({SHOP_URL}/feature, e.g. paymentFailure at 50%) and wait for its alert to fire (about 5 minutes).")
for i, item in enumerate(alerts):
    a = item["alert"]
    left, mid, right = st.columns([3, 5, 2])
    if a is None:
        left.markdown(f"**{item['name']}**  \n{item['state']}")
        mid.warning(f"Cannot open an incident from this alert: {item['problem']}")
        continue
    left.markdown(f"**{a.alert_name}**  \n{item['state']} · {item['severity'] or 'no severity'} · {a.service}")
    mid.markdown(f"{a.summary}  \n{a.signal} = {a.value:.4g} (threshold {a.threshold:.4g}) since "
                 f"{a.fired_at.astimezone():%H:%M:%S}")
    if right.button("Investigate", key=f"investigate-{i}", disabled=busy or item["state"] != "firing",
                    type="primary"):
        slot["session"] = IncidentSession(a, engineer_id=engineer)
        st.rerun()

session = slot["session"]
if session is None:
    st.stop()


# ---------------------------------------------------------------- live investigation
def render_live(view: dict) -> None:
    a, values = view["alert"], view["values"]
    st.subheader(f"Incident {view['incident_id']}")
    status = view["status"].replace("_", " ")
    sev = values.get("severity")
    st.markdown(f"**Status:** {status}" + (f" · **Severity:** {sev}" if sev else "")
                + (f" · **Final:** {values['final_status']}" if values.get("final_status") else ""))
    st.markdown(f"**Alert:** {a.alert_name} on `{a.service}`: {a.summary} ({a.signal} = {a.value:.4g}, "
                f"threshold {a.threshold:g})")

    plan_col, hyp_col = st.columns(2)
    plan = values.get("plan")
    with plan_col:
        st.markdown("**Plan**" + (f" (version {plan.version})" if plan else ""))
        if plan:
            for step in plan.steps:
                st.markdown(f"- **{step.name}**" + (f": {step.note}" if step.note else ""))
            if plan.reason:
                st.caption(f"Why this version: {plan.reason}")
        else:
            st.caption("Waiting for the commander's plan.")
    with hyp_col:
        st.markdown("**Hypotheses**")
        hyps = values.get("hypotheses") or []
        if hyps:
            st.dataframe([{"service": str(h.service), "fault": str(h.fault_type), "status": h.status,
                           "why": h.rationale} for h in hyps], hide_index=True)
        else:
            st.caption("None yet.")

    st.markdown("**Investigation**")
    rows = timeline_rows(values)
    if rows:
        st.dataframe(rows, hide_index=True)
    else:
        st.caption("Starting: connecting to the MCP servers and reading the alert.")


@st.fragment(run_every=REFRESH_S)
def live() -> None:
    view = session.view()
    render_live(view)
    if view["status"] != st.session_state.get("seen_status"):     # a pause or the end: redraw the whole page
        st.session_state["seen_status"] = view["status"]
        if view["status"] not in ACTIVE:
            st.rerun()


view = session.view()
st.session_state.setdefault("seen_status", view["status"])
if view["status"] in ACTIVE:
    st.session_state["seen_status"] = view["status"]
    live()
else:
    render_live(view)

# ---------------------------------------------------------------- the pauses and the end
if view["status"] == "awaiting_approval":
    p = view["pending"]
    st.divider()
    st.subheader("Approval needed")
    st.warning("The copilot wants to change the shop. Nothing runs until you decide.")
    act_col, info_col = st.columns(2)
    with act_col:
        st.markdown(f"**Exact action** ({p.get('purpose', 'fix')})")
        st.code(json.dumps(p["action"], indent=2), language="json")
        st.caption(f"Payload hash: `{p['payload_hash']}`: your approval is bound to this exact action.")
    with info_col:
        dry = p.get("dry_run") or {}
        st.markdown(f"**Dry run:** {dry.get('would_do', '')}")
        if dry.get("current_state"):
            st.markdown("**Current state:**")
            st.json(dry["current_state"], expanded=False)
        st.markdown(f"**Expected effect:** {p.get('expected_effect', '')}")
        st.markdown("**Rollback:** " + (f"`{json.dumps(p['rollback_plan'])}`" if p.get("rollback_plan")
                                         else "none available"))
        st.markdown(f"**Recovery is checked with:** `{p.get('verify_query', '')}`")
    reason = st.text_input("Reason (optional for approve, recorded with your decision)")
    approve_col, reject_col, _ = st.columns([1, 1, 4])
    if approve_col.button("Approve", type="primary"):
        session.answer(approval_answer(p, "approve", engineer, reason))
        st.rerun()
    if reject_col.button("Reject"):
        session.answer(approval_answer(p, "reject", engineer, reason))
        st.rerun()
    with st.expander("Edit the action instead"):
        edited = st.text_area("Edited action (JSON)", value=json.dumps(p["action"], indent=2), height=160)
        if st.button("Send edited action"):
            try:
                session.answer(approval_answer(p, "edit", engineer, reason, edited_action=json.loads(edited)))
                st.rerun()
            except (json.JSONDecodeError, ValueError) as exc:
                st.error(f"Not a valid action: {exc}")

elif view["status"] == "awaiting_review":
    p = view["pending"]
    pm = p.get("postmortem") or {}
    st.divider()
    st.subheader("Postmortem review")
    if p.get("status_note"):
        st.markdown(f"**Status note:** {p['status_note'].get('text', '')}")
    st.markdown(f"**Summary:** {pm.get('summary', '')}")
    st.markdown(f"**Root cause:** {pm.get('root_cause', '')}")
    if pm.get("action"):
        st.markdown(f"**Action taken:** `{json.dumps(pm['action'])}`")
    lessons = pm.get("lessons_proposed") or []
    if lessons:
        st.markdown("**Lessons proposed:**")
        for lesson in lessons:
            st.markdown(f"- {lesson.get('text', lesson)}")
    root_cause = st.text_input("Correct the root cause (optional)")
    accept_col, reject_col, _ = st.columns([2, 2, 3])
    if accept_col.button("Accept postmortem", type="primary"):
        session.answer(review_answer(True, engineer, root_cause))
        st.rerun()
    if reject_col.button("Reject postmortem"):
        session.answer(review_answer(False, engineer))
        st.rerun()

elif view["status"] == "done":
    values = view["values"]
    st.divider()
    st.subheader("Final status")
    final = values.get("final_status") or "unknown"
    (st.success if final == "recovered" else st.warning)(f"Incident ended: **{final}**")
    note = values.get("status_note")
    if note is not None:
        st.markdown(f"**Status note:** {note.text}")
    if values.get("escalation_reason"):
        st.markdown(f"**Escalated because:** {values['escalation_reason']}")
    outcome = values.get("outcome")
    if outcome is not None:
        st.markdown(f"**Action:** `{outcome.action.model_dump_json()}`, verified: {outcome.verified}, "
                    f"rolled back: {outcome.rolled_back}")

elif view["status"] == "error":
    st.error(f"The incident stopped with an error: {view['error']}")
