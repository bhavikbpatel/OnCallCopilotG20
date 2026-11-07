# Evidence: the engineer's console shows the approval screen (Person 5)

Sprint Backlog, Person 5: "Build the UI (Streamlit, Chainlit or Gradio): shows the alert, live investigation
stream, the plan, the approval screen with the exact command, and the final status note."

**Run (6 Nov 2026, new laptop, full stack on the 3.1.0 images).** `streamlit run ui/app.py`, then
`python -m eval.injector set paymentFailure 50%`; the console listed the real `CheckoutErrorRateHigh` alert
once Prometheus fired it, and the incident was started from the page.

| Time (IST) | What the console showed | Screenshot |
| --- | --- | --- |
| 21:41 | `CheckoutErrorRateHigh` firing: checkout error rate 0.4167, threshold 0.05, since 21:34:54 | `1-alert-firing.jpg` |
| 21:41-21:43 | The commander's plan (version 1), the hypothesis payment/paymentFailure, SEV1 from policy rule S1.1, and the live timeline: triage, hypothesise, diagnosis (supported after 4 calls), assess, runbook RB-001, propose, dry run | `2-live-plan-and-hypothesis.jpg` |
| 21:43:26 | Approval needed: the exact action `{"tool": "set_flag", "flag": "paymentFailure", "variant": "off"}`, its payload hash, the dry run (50% to off), the expected effect, the rollback (back to 50%) and the PromQL that checks recovery | `3-approval-needed.jpg` |
| 21:44:58 | Approved by the on-call engineer with a reason; the timeline records `approve by on-call: ...` | `4-approve-with-reason.jpg`, `5-timeline-approval-recorded.jpg` |
| 21:45 | The approved write ran: `paymentFailure` was `off` at 21:45:11 | |
| ~21:49 | Recovery verified; postmortem review with the status note, root cause, action taken and two proposed lessons | `6-postmortem-review.jpg` |
| ~21:50 | Postmortem accepted; final status **recovered**, action verified, not rolled back | `7-final-status-recovered.jpg` |

**How it is wired.** `ui/incident.py` drives the graph through `copilot.runtime.copilot_runtime()` (the incident
API's wiring: MCP servers, checkpointer, LangSmith tracing) on its own thread, with one set of hooks per incident
set through `copilot.toolctx` as `copilot.run.run_incident` does (the run above used the older `COPILOT_HOOKS=1`
switch, which #66 removed), and answers the two `interrupt()` pauses with the engineer's decision. An approval
carries the payload hash of the action shown; the approval token is minted inside the graph, never by the page.
The alert comes from the shop's own Prometheus rules (`infra/prometheus/rules/shop.yml`), rebuilt with the same
query and threshold.

**Live check of the per-incident hooks (7 Nov 2026, after #66 and #68).** The four MCP servers were rebuilt from
`main` (so #65's tool-description pins were enforced when the copilot connected) and `paymentFailure` was set to
50%. One incident was driven through `ui.incident.IncidentSession`, the session code `ui/app.py` uses, from a
short script instead of the page, so the incident's hooks log could be read at the end. The script approved only
`set_flag paymentFailure off` and accepted the postmortem.

| Time (IST) | Event |
| --- | --- |
| 17:51 | `paymentFailure` set to 50% |
| 17:59:53 | `CheckoutErrorRateHigh` firing (checkout error rate 0.5) |
| 18:01:49 | Approval asked for `{"tool": "set_flag", "flag": "paymentFailure", "variant": "off"}`; approved |
| 18:05:33 | Postmortem (root cause: the `paymentFailure` flag rolled to 50% at 12:21 UTC, payment errors) accepted; final status **recovered** |

The incident's hooks logged all 23 tool calls (`hooks-log-2026-11-07.jsonl`), each with its agent and role:
19 code-driven (triage's queries and ticket, runbook search, the dry run, the approved write, verify polling,
the postmortem) and 4 model-driven (the diagnosis agent: two PromQL queries, error traces, recent changes). The
dry run (seq 14) and the approved `set_flag` (seq 15) carry purpose `fix`. Nothing was blocked, no result was
flagged by the injection filter, and the model used 4 of its 30-call budget. The approval token is not in the
log: the hooks drop it before logging. The hooks' token and dollar totals read 0 because nothing feeds them
(`Hooks.add_cost` is called only by tests, so the eval runs read 0 too); the graph keeps its own token and cost budget.

**Not yet.** The page runs the graph in its own process; once Person 3's incident endpoint exists, the
same page can follow its stream instead (the backlog's "streaming comes from Person 3's endpoint").
