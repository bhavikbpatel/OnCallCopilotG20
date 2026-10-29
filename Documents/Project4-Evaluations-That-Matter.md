# Project 4 Evaluations That Matter

Oct 28, 2026 · @Bhavik Patel

The evaluations the capstone guide (`capstone_projects.pdf`) makes most important for On-Call Copilot, ranked, with page references.

## Top tier: Project 4's own metrics (page 14)

The guide names specific metrics for On-Call Copilot; these are the core of the evaluation.

**1. Harmful or unnecessary actions: target zero** (most important)

- The guide says to give these "particular attention", and its "Watch out for" box warns against leaving them out of the count.
- *What it is:* any change the agent makes that was not needed or made things worse, for example restarting a healthy service or acting on a noisy alert where nothing was broken.
- *Why it matters:* Project 4 is about safe action in a live system. One harmful action undermines the whole safety story, even if every diagnosis is right.

**2. Correct root cause, top-1 and top-3, by service and fault type**

- *What it is:* top-1 means the agent's first guess is the right service and fault; top-3 means the right answer is in its top three. Report these per fault type, not only as one overall number.
- *The trap:* never score diagnosis using the agent's own confidence. Compare against the fault you injected; injection gives exact ground truth.

**3. Correct action proposed, and verified recovery**

- *What it is:* did the agent propose the known fix (such as switching the flag off), and did the shop actually recover afterwards?
- *Why it matters:* this is the evidence for signature challenge B (safe action). The checker confirms recovery from metrics itself rather than trusting the agent's claim.

**4. Time to diagnose and tool calls per incident**

- Efficiency numbers that show cost and speed. They matter most alongside the budget experiment (E4).

## Second tier: the four experiments (pages 5–6)

These carry the "fair experiments" part of the technical-depth marks. E4 is Project 4's own experiment, so it ranks first.

| Exp | Question it answers | Why the examiners care |
| --- | --- | --- |
| **E4: signature ablation** | Memory on vs off, including the misleading-lesson case; tool-call budgets of 5, 10 and 20 | Shows memory helps without blindly applying old lessons, and how diagnosis quality changes with budget |
| **E1: does the agent justify its cost?** | Fixed workflow (C0) vs one agent (C1) vs our team (C2), with the same model, tools and budget | Asked directly in the viva: "What benefit did the team of agents provide, and at what cost?" |
| **E2: do the defences work?** | Defences off vs on: attack success, plus how often legitimate work is blocked | Proves the security work; false blocks show the defences are not just blocking everything |
| **E3: did the lessons help?** | Held-out results before vs after the playbook, plus the cost of making lessons and any lesson that made things worse | Tests whether the improvement loop generalises to unseen cases |

## Third tier: what makes results trustworthy (pages 3, 5–6)

These produce no headline numbers, but getting them wrong can sink credibility in the viva.

- **Golden set discipline:** 60+ cases, each expected outcome written before the agent runs; about 20 dev and 40+ held-out cases; related variants kept together; held-out cases in a folder the agent builders cannot read.
- **Code checks first, judge second:** code checks the final state, actions, cited evidence and forbidden actions. An LLM judge from a different model family handles only what code cannot, checked against 20+ hand labels with Cohen's kappa.
- **Honest reporting:** for every configuration, report success counts, policy violations, attack success, false blocks, cost per case, median and 95th-percentile latency, and errors. Include failed and timed-out runs. Repeat the final held-out comparison 3 times and report the spread.
- **Contamination rule:** anything changed after seeing held-out results turns those cases into dev cases, and the report says so.

## Evaluation evidence from the minimum bar (page 3)

The examiners also expect these items. Most belong to teammates; Person 4 supplies the numbers.

| Evidence | Built by | Person 4's part |
| --- | --- | --- |
| A trace where the first hypothesis is wrong and the agent recovers (signature A) | Person 2 | Harness captures and keeps the trace |
| Live demo of an edit invalidating an approval | Person 3 | Checker logs approval hashes |
| Recall@5 before and after one change | Person 1 | None directly |
| Eval in CI that fails below a threshold, plus one failed run with an explanation | Person 5 | Runner and dev cases used by the CI gate |

## How this maps to the marks (page 6)

The rubric weights four areas about equally; evaluation feeds three of them.

- **Scope and impact:** "measure an outcome, and state the limits accurately"
- **Technical depth:** "complete both signature challenges properly, and run fair experiments"
- **Presentation:** "present numbers with context"

**If time gets tight, protect these in order:**

1. The zero-harm count
2. Root-cause accuracy scored against injected faults
3. Verified recovery
4. E4
5. E1

The rest strengthens the case but carries less weight.

## Dependencies: what Person 4 needs from each teammate

The evaluation can only score what the system exposes, so each item below must land by **30 Oct–1 Nov**, before integration (31 Oct–1 Nov) and the 2 Nov "everything built" date. Each row is a small contract: what is needed, by when, how Person 4 will check it, and which metric breaks without it.

| Owner | What we need (and in what form) | Due | How Person 4 checks it | Why it matters | Status |
| --- | --- | --- | --- | --- | --- |
| Person 2 | Final state includes **ranked hypotheses**: `[{service, fault_type, rank, status}]` | 30 Oct | Runner reads them from dev case D-001 without errors | Root cause top-1 / top-3 | Not started |
| Person 2 | Final state includes **proposed and executed actions** (tool + arguments) and the **verification result** | 30 Oct | Checker compares them with the case's expected action | Correct action, verified recovery, honest verification | Not started |
| Person 2 | Final state includes **replan count** and **lessons used** | 30 Oct | Present in every result line | Signature A evidence, E3 | Not started |
| Person 2 | **C0, C1 and C2** selectable from the config file, not separate code | 1 Nov | Runner switches configs with one setting | E1 | Not started |
| Person 2 | **Memory on/off** and **budget** (5 / 10 / 20) read from config | 1 Nov | Same case runs under each setting | E4 | Not started |
| Person 2 | **Playbook frozen** and agent version tagged before held-out runs | 2 Nov | Tag recorded in the experiment log | E3, contamination rule | Not started |
| Person 3 | Log of **every tool call, including blocked ones**: agent, tool, arguments, result, blocked yes/no | 30 Oct | Checker counts writes and blocked calls per run | Harmful actions, policy violations, attack success | Not started |
| Person 3 | **Approval records**: command hash, decision (approve / edit / reject) | 30 Oct | Every executed write has a matching approved hash | Policy violations | Not started |
| Person 3 | **Auto-approve / auto-reject** at the gate from config, with the exact proposal logged | 30 Oct | Runs complete with no human clicking | Makes \~560 runs possible | Not started |
| Person 3 | **Defences on/off switch** in config | 1 Nov | Same attack runs under both settings | E2 | Not started |
| Person 3 | A way to **load an extra MCP server at eval time** | 1 Nov | Person 4 plants a test server without sharing its content | Poisoned-tool-description attacks | Not started |
| Person 5 | **Shop decision** and a scriptable way to switch faults on and off (flagd or Toxiproxy) | 29 Oct | Injector turns paymentFailure on and off | Fault injector, all cases | Not started |
| Person 5 | **Eval mode** with short alert and verify windows (target about 2 minutes per incident) | 30 Oct | One dev case completes in about 2 minutes | Fits the held-out run schedule | Not started |
| Person 5 | **Trace ID and cost** per run, readable by the runner | 30 Oct | Present in every result line | Cost per case, latency | Not started |
| Person 5 | **CI step** that calls the runner on dev cases and fails below the threshold | 2 Nov | One passing and one failing CI run | Minimum-bar CI evidence | Not started |
| Person 1 | **Severity policy** with numbered rules | 29 Oct | Expected severity written for each case | Severity check | Not started |
| Person 1 | **Misleading-lesson postmortem** (lesson whose preconditions fail in another situation) | 30 Oct | Used in the E4 misleading-lesson case | E4 | Not started |
| Person 1 | **Review of held-out cases** against the severity policy; runbooks contain no case answers | 1 Nov | Review notes recorded | Credible held-out set | Not started |

### What Person 4 gives back

| To | What | When |
| --- | --- | --- |
| Persons 2 and 3 | The \~20 development cases and the checker code, so they test against the same scoring | 30 Oct |
| Person 5 | The runner command and the proposed CI threshold | 1 Nov |
| Everyone | Development results after each run, so each person sees their own numbers early | From 1 Nov |

### What is not shared

The **held-out cases** and the **red-team attacks** stay with Person 4, by design (capstone guide and sprint backlog). Person 1 may review held-out cases against the policy. Nobody else needs them to build their part; please do not ask for them, so the results stay credible.

### How we keep this on track

1. Walk through this table together in one 15-minute call.
2. Each owner updates the Status column here.
3. Check status at every stand-up until 1 Nov. Anything still "Not started" on 30 Oct is a risk to integration and gets raised that day.
4. If a field or format changes, update the row and tell Person 4 the same day.
