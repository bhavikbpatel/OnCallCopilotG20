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
API's wiring: MCP servers, hooks with `COPILOT_HOOKS=1`, checkpointer, LangSmith tracing) on its own thread, and
answers the two `interrupt()` pauses with the engineer's decision. An approval carries the payload hash of the
action shown; the approval token is minted inside the graph, never by the page. The alert comes from the shop's
own Prometheus rules (`infra/prometheus/rules/shop.yml`), rebuilt with the same query and threshold.

**Not yet.** The page runs the graph in its own process; once Person 3's incident endpoint exists, the
same page can follow its stream instead (the backlog's "streaming comes from Person 3's endpoint").
