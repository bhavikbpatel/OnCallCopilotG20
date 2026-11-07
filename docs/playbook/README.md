# Frozen playbook

- **Hash:** `0c05d04cf34aa7ac5758fe2644993377757636683aaf9e6755b4c9a2faa64bf3`
- **Frozen:** 2026-11-07 17:28 UTC
- **Reviewed by:** Rohit
- **Lessons:** 9 approved, 8 rejected (see `rejected.md`)
- **Cost of writing the lessons:** 81269 tokens, $0.0113 over 17 reflection(s) (0 without a known cost); review time not included

Built by `python -m copilot.reflexion` from dev runs only. Every lesson was approved by a person and has preconditions the graph checks against live telemetry before using it.

Held-out and experiment runs read it with `memory: playbook` (read-only). To put it into another Postgres:

```bash
POSTGRES_DSN=... python -m copilot.reflexion load docs/playbook/playbook.json
```

Any change to a lesson gives a new hash, and held-out runs made with the old one must be redone.

## Notes

- D-012's first reflection failed the format check (within: "15"); that failed, unreviewed row was deleted by hand and the reflection re-run, giving C-D-012-017.
- Four candidates (D-003, D-008, D-016, D-020) came from runs that errored before the agent ran; all four were rejected, and failed_runs now skips such runs.
- Decisions were drafted with an assistant, checked by three independent reviews (telemetry, safety, process and facts), then confirmed by Rohit.
- Known limit: lessons are shown only after the root cause is chosen, for that one service, so they shape the action, not the diagnosis.
