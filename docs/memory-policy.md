# Memory policy (one page)

**Purpose.** Reuse reviewed incident summaries and narrowly scoped lessons to reduce repeated investigation without turning one incident into a universal rule.

**What may be stored.** Redacted summaries, root cause, approved action, verified outcome, timeline references, and a short lesson with machine-checkable applicability preconditions. Each record includes a source incident, service/fault family, reviewer identity, review status and withdrawal status. Store references/aggregates, not raw logs, traces, secrets, customer data, or held-out evaluation content.

**Review before reuse.** New lessons enter `pending`. Only a named human may approve, edit-and-approve, reject or withdraw them. Pending, rejected and withdrawn lessons are never returned as applicable advice. Editing creates a reviewed version; review is not inferred from authoring or automated evaluation.

**Applicability is code-owned.** Before retrieval, code checks service and fault family, then every declared precondition against fresh structured telemetry (for example, matching flag changed within 15 minutes and error rate above a threshold). Missing, stale, malformed or unsupported evidence fails closed. A mismatched lesson may be surfaced only as a rejected lesson with the failed reason; it must not enter the agent's applicable context. The agent cannot override a failed predicate.

**Lifecycle and control.** The on-call engineer can inspect, correct and delete incident records; a reviewer can withdraw a lesson. Every use/rejection records lesson ID, source, reviewer, predicate evidence and outcome. Diagnosis agents remain read-only. Memory never bypasses typed action allow-lists, dry-run, exact-command approval, post-action verification or rollback.

**Evaluation.** Compare identical development queries with memory enabled and disabled; report retrieval/diagnosis quality, harmful and unnecessary writes, and latency/cost. A misleading lesson with unmet preconditions must be rejected in both modes of safe control, and held-out cases must never be ingested, tuned against or used to author lessons.
