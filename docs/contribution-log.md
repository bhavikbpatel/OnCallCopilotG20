# Contribution log

**Why:** the Sprint Backlog asks everyone to keep a contribution log (definition of done, "Everyone"),
and the PR-review checklist (`pr-review/SKILL.md` §4.1) checks that each PR updates it.

**How:** add one row per merged PR (or per piece of work that is not a PR), newest at the bottom,
in the same PR as the work. Say what changed in plain words; link the PR number. Each person writes
their own rows only.

| Date | Person | PR | What |
|---|---|---|---|
| 25 Oct | Person 4 (Bhavik) | #1 | System design document for the On-Call Copilot |
| 31 Oct | Person 4 (Bhavik) | #2 | Evaluation harness: case and result schemas, code checkers with tests, 20 dev cases |
| 31 Oct | Person 4 (Bhavik) | #3 | Fault injector: flag UI client, timed plans, contamination detection, guaranteed clean-up |
| 31 Oct | Person 4 (Bhavik) | #4 | Discovery probe and Prometheus client, verified on the live shop |
| 31 Oct | Person 4 (Bhavik) | #5 | Course-allowed OpenRouter models recorded; judge models not on the list refused |
| 31 Oct | Person 4 (Bhavik) | #7 | Signal map for recovery checks; shop telemetry findings |
| 31 Oct | Person 4 (Bhavik) | #9, #10, #11 | Fault-probe analysis, error-count signal and measured recovery checks for the dev cases (#9 merged, reverted by mistake in #10, re-applied in #11) |
| 31 Oct | Person 4 (Bhavik) | #12 | Evaluation contract aligned with Person 2's incident graph; adapter from incident state to result line |
| 31 Oct | Person 4 (Bhavik) | #13 | PR-review skill: one checklist for every pull request |
| 31 Oct | Person 4 (Bhavik) | #14 | Python 3.9 restriction dropped; recommendationCacheFailure marked unused; .DS_Store no longer committed |
| 31 Oct | Person 4 (Bhavik) | #15 | Independent recovery checker from Prometheus |
| 1 Nov | Person 4 (Bhavik) | this PR | Evaluation runner with synthetic alerts, auto-approval, do-nothing agent, JSONL results and CI gate; this log |
