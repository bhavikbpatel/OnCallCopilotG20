# Evidence: the CI eval gate fails below its threshold (Person 5)

Sprint Backlog, Person 5: "Build CI (GitHub Actions) that runs the evaluation on development cases and fails
below a threshold. Keep one failed run and its explanation as evidence."

**The failed run.** https://github.com/bhavikbpatel/OnCallCopilotG20/actions/runs/42219466359
(6 Nov 2026, workflow `eval-gate`, branch `person5-ops` at b0fa97a, 15.5 minutes).

| Input | Value |
| --- | --- |
| cases | D-001 (payment errors at 50%) |
| agent | `do-nothing` (eval/runner/agents.py: the floor baseline; never investigates or acts, and escalates) |
| min_success | 0.6 |

**What happened.**

1. The runner pulled the shop's 3.1.0 images and started the lite stack in 3.5 minutes (12:40:54 to
   12:44:27 UTC); span metrics reached Prometheus.
2. The runner set `paymentFailure` to 50%, waited for `CheckoutErrorRateHigh`, and handed the incident to the
   agent. The do-nothing agent took no action, so the shop did not recover.
3. The runner's log: `D-001 r1: completed success=False not recovered`, then
   `FAIL: task success 0.0 is below 0.6`. It exited with code 1 and the job failed at
   "Run development cases and gate". The results were uploaded as the `eval-gate-results` artifact
   (kept until 4 Feb 2027).

**Why this failure is the right one.** The run completed: every setup step passed and the case status is
`completed`, not `error` or `timeout`, so this is not a flaky stack. The checker scored the agent against the
injected fault, never the agent's own confidence: no correct action, no recovery, task success 0.0. An agent
that does nothing must not pass, and the gate stopped it. This is the negative control for the gate; a real
gate run uses `agent: copilot` (it needs the `OPENAI_API_KEY` repository secret and spends OpenRouter credit).

**A fix the first run needed.** Before b0fa97a the gate started the stack with `docker compose up` alone. Every
shop service has a build section, and `up` builds a missing image from source instead of pulling it, so the
first run would have compiled the whole shop on the runner. The gate now runs
`docker compose -f compose.lite.yaml pull --quiet --ignore-pull-failures` first, as the README says.
