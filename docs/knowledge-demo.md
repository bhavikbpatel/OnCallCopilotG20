# Knowledge and memory demo

1. Load a development case such as D-001, D-011 or D-012 and show the alert, affected service and evidence timestamps. All diagnosis tool calls are read-only.
2. Search with the symptom and service filter. Show the top five passage IDs, metadata and whether each result is a runbook or synthetic postmortem. Explain that this repository uses a deterministic local BM25/TF-IDF baseline; the production BGE/Qdrant/cross-encoder adapters remain deployment work.
3. Demonstrate memory review: add a lesson as `pending` and show it is not returned; approve it as a named reviewer; run every structured precondition against the current telemetry snapshot.
4. For D-011, present the misleading paymentFailure lesson while checkout-to-payment connection errors are present and payment server errors are flat. The lesson must be rejected because its required payment errors and recent flag change are absent.
5. If diagnosis supports a fix, show its typed arguments, dry-run output, expected effect and rollback. Pause for a human to approve the exact command/hash; execute only after approval and verify with fresh metrics/traces. Otherwise take no action.
6. Report the independently observed outcome and the memory-on/off comparison; do not present synthetic incidents as real production events or expose held-out cases.
