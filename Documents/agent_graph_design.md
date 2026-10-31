# On-Call Copilot — Agents & Orchestration Design

Owner: Rohit ( Agents & Orchestration) · Status: DRAFT · Last updated: 2026-10-28

Tags used in this doc:

- **\[Guide\]** — stated in the capstone guide (`capstone_projects.pdf`, page noted)  
- **\[Vision\]** — stated in the team's Product Vision doc  
- **\[Proposal\]** — our design choice, open for review

---

## 1\. Starting point: the incident endpoint

### 1.1 What starts an incident

1. A fault happens in the shop (in testing, Bhavik fault injector flips a flaged flag such as `paymentFailure`).  
2. A **Prometheus alert rule** fires, e.g. *checkout error rate above 5% for 5 minutes*. **\[Guide p13\]**  
3. **Alertmanager** (or a Grafana alert) sends a webhook to our **incident API**. **\[Guide p13\]**  
4. The incident API **starts the graph** for that incident. The engineer can **watch the investigation live**. **\[Guide p13\]**

Owners **\[Vision\]**:

| Piece | Owner |
| :---- | :---- |
| Alert rules (Prometheus / Alertmanager) | **Uppili** |
| FastAPI incident endpoint | (Asiya / Bhavik) |
| Streaming FastAPI service \+ UI | **Uppili** |
| The graph the endpoint calls | Rohit  |

### 1.2 What the endpoint receives

```json
{                                                                                                                                                                                                              
    "status": "firing",                                                                                                                                                                                          
    "alerts": [{                                                                                                                                                                                                 
      "labels": {                                                                                                                                                                                                
        "alertname": "CheckoutErrorRateHigh",                                                                                                                                                                    
        "service": "checkout",                                                                                                                                                                                   
        "severity": "page"                                                                                                                                                                                       
      },                                                                                                                                                                                                         
      "annotations": {                                                                                                                                                                                           
        "summary": "Checkout error rate above 5% for 5 minutes",                                                                                                                                                 
        "value": "0.12"                                                                                                                                                                                          
      },                                                                                                                                                                                                         
      "startsAt": "2026-11-03T10:15:00Z",                                                                                                                                                                        
      "fingerprint": "a1b2c3"                                                                                                                                                                                    
    }]                                                                                                                                                                                                           
  }

```

```json
{
  "version": "4",
  "groupKey": "...",
  "status": "firing",
  "receiver": "oncall-copilot",
  "groupLabels": {}, "commonLabels": {}, "commonAnnotations": {},
  "externalURL": "...",
  "alerts": [
    {
      "status": "firing",
      "labels": {},
      "annotations": {},
      "startsAt": "2026-11-03T10:15:00Z",
      "endsAt": "0001-01-01T00:00:00Z",
      "generatorURL": "...",
      "fingerprint": "a1b2c3"
    }
  ]
}
```

The outer structure is fixed by Alertmanager. **What is inside `labels` and `annotations` is decided by how we write our alert rules.**

**\[Proposal\]** Every alert rule must carry these labels/annotations, so the endpoint can build the graph input the same way for every alert:

| Where | Key | Example | Why the graph needs it |
| :---- | :---- | :---- | :---- |
| label | `alertname` | `CheckoutErrorRateHigh` | always present (Alertmanager) |
| label | `service` | `checkout` | where the symptom is seen |
| label | `signal` | `error_rate` / `latency_p95` | what kind of symptom |
| annotation | `summary` | `Checkout error rate above 5% for 5m` | human-readable |
| annotation | `threshold` | `0.05` | used later to check recovery |
| annotation | `value` | `0.12` | value when it fired |
| annotation | `query` | the PromQL of the rule | lets the graph re-check the same metric |

To agree with Upilli (rules) and Asiya (endpoint).

### 1.3 What the endpoint gives the graph — the `Alert` object **\[Proposal\]**

The endpoint turns each **firing** alert into one small object. This is the only input the graph receives.

```py
class Alert(BaseModel):
    alert_name: str        # labels.alertname
    service: str           # labels.service
    signal: str            # labels.signal
    summary: str           # annotations.summary
    query: str             # annotations.query   (PromQL)
    threshold: float       # annotations.threshold
    value: float           # annotations.value
    fired_at: datetime     # startsAt
    fingerprint: str       # fingerprint (used to avoid duplicate incidents)
```

Endpoint rules **\[Proposal\]**:

- `status = resolved` → do not start a graph; if an incident is open for that fingerprint, record it on the incident.  
- Same `fingerprint` already has an open incident → do not start a second one.  
- Alert text is **data, not instructions** — passed through input guardrails / secret redaction (Person 3). **\[Guide p2, p13\]**

### 1.4 How the endpoint calls the graph **\[Proposal\]**

The graph is a LangGraph graph with a checkpointer. Each incident is one **thread**: `thread_id = incident_id`. **\[Guide p5: checkpointer, `interrupt()` \+ `Command(resume=...)`\]**

| Endpoint | What it does | Graph call |
| :---- | :---- | :---- |
| `POST /incidents/alert` | Receive webhook, build `Alert`, create `incident_id`, start the run, return `202 {incident_id}` | `graph.astream({"alert": alert}, config)` in background |
| `GET /incidents/{id}/stream` | Live stream of what the graph is doing (SSE) | events from `graph.astream(...)` |
| `POST /incidents/{id}/decision` | Engineer approves / edits / rejects | `graph.astream(Command(resume=decision), config)` |
| `GET /incidents/{id}` | Current state of the incident | `graph.get_state(config)` |

```py
config = {"configurable": {"thread_id": incident_id}}
```

---

## 2\. Overall flow

The guide's walkthrough **\[Guide p13–14\]**, as graph steps:

```
 Alert
   │
   ▼
 1. Commander plans          scope → hypotheses → test → act → verify → communicate
   │
   ▼
 2. Triage                   what is affected, how bad (severity), what changed recently
   │
   ▼
 3. Diagnosis (parallel)     one agent per suspect service tests the hypotheses
   │                         ◄── if evidence contradicts the hypotheses: REPLAN (back to 1/3)
   ▼
 4. Runbook                  find the matching runbook + past incidents; check old lessons apply
   │
   ▼
 5. Propose ONE action       set_flag / restart / scale / rollback, with expected effect
   │
   ▼
 6. ⏸ Engineer approves      the exact command (edit → must approve again)
   │
   ▼
 7. Remediation              dry run → run → watch metrics
   │                         ◄── if not recovered: ROLL BACK and REPLAN
   ▼
 8. Comms                    status note + postmortem with timeline
   │
   ▼
 9. ⏸ Person reviews → memory stores the incident and its lessons
```

Two loops make this "agentic" rather than a fixed script:

- **Replan on contradicting evidence** (step 3\) — Signature challenge A. **\[Guide p14\]**  
- **Roll back and replan if the fix did not help** (step 7\) — Signature challenge B. **\[Guide p14\]**

If the agent runs out of budget or cannot find a safe action, it **escalates** to the engineer instead of acting. **\[Proposal\]**

### 2.1 The graph **\[Proposal\]**

The same flow as a LangGraph graph. Node names match the sections in §3. A graph diagram is a required deliverable for L8. **\[Guide p3\]**

![][image1]  
Reading the graph:

- `⏸` / hexagon \= the graph **pauses** with `interrupt()` and waits for a person. **\[Guide p5\]**  
- **Only Remediation changes the shop**, and only after approval. All other nodes are read-only. **\[Guide p14\]**  
- The two loops back to *Commander: hypothesize* are the replan paths (Signature A and B).  
- Routing decisions (which arrow to follow) are made by **code** from the state, not by the model. **\[Guide p2: "the model proposes; code and people decide"\]**

---

## 3\. Nodes

Each node will be described with the same template:

> **Role** · **Input** · **Output** · **Tools it may call (read/write)** · **What the model decides vs what code decides** · **When it fails**

| \# | Node | One-line role | Status |
| :---- | :---- | :---- | :---- |
| 3.1 | Intake | turn the `Alert` into a new incident state | TODO |
| 3.2 | Commander — plan | write the plan object | TODO |
| 3.3 | Triage | impact, severity, recent changes, suspect services | TODO |
| 3.4 | Commander — hypothesize | choose hypotheses to test, split the budget | TODO |
| 3.5 | Diagnosis (one per suspect) | test hypotheses with metrics, logs, traces (read-only) | TODO |
| 3.6 | Commander — assess | converge, replan or escalate | TODO |
| 3.7 | Runbook | runbook \+ past incidents \+ lesson precondition check | TODO |
| 3.8 | Propose action | one allow-listed action with expected effect and rollback | TODO |
| 3.9 | Approval gate | pause for the engineer on the exact command | TODO |
| 3.10 | Remediation | dry run, execute, verify, roll back | TODO |
| 3.11 | Comms | status note \+ postmortem | TODO |
| 3.12 | Review \+ memory | person reviews; store incident and lessons | TODO |

---

## 4\. Agent memory

Two kinds of memory **\[Guide p5\]** **\[Vision §8\]**:

| | Short-term | Long-term |
| :---- | :---- | :---- |
| Scope | One incident (one thread) | Across incidents |
| Mechanism | LangGraph **checkpointer** \+ typed state | LangGraph **store** (namespaces) |
| Backend | `AsyncPostgresSaver` (dev: `InMemorySaver`) | `AsyncPostgresStore` (dev: `InMemoryStore`) |
| Key | `thread_id = incident_id` | namespace tuple \+ key |
| Written by | Every node (auto checkpoint after each step) | **Only** node 3.12 Review \+ memory, after a person approves |
| Read by | Every node (its own slice) | Runbook (incidents, lessons); Commander \+ Comms (engineer prefs) |

Ownership: the store backend, lesson schema and precondition evaluator are built with the Knowledge & Memory engineer (role 2); this section fixes the **interface the graph uses**.

### 4.1 Short-term memory **\[Proposal\]**

**Checkpointer.** Compiled once: `graph = builder.compile(checkpointer=saver, store=store)`. A checkpoint is written after every node, so:

- the graph can pause at `interrupt()` for minutes (approval gate, review) without holding a process;  
- **resume demo**: kill the container at the approval gate → restart → `Command(resume=...)` on the same `thread_id`; action and `payload_hash` are still in state **\[Vision §4\]**;  
- `graph.get_state(config)` backs `GET /incidents/{id}`; `get_state_history` gives the timeline for the postmortem.

**State and reducers.** The `IncidentState` from the system design (§4), plus two fields for memory:

```py
class EvidenceItem(BaseModel):
    evidence_id: str          # promql+time range | trace_id | log doc id
    hypothesis_id: str
    summary: str              # one line, written by the tool/agent — never raw telemetry
    supports: bool | None     # True supports, False contradicts, None neutral
    at: datetime

class TimelineEvent(BaseModel):
    at: datetime
    node: str
    kind: Literal["plan", "replan", "tool", "verdict", "action", "approval", "rollback", "comms"]
    text: str

class IncidentState(TypedDict):
    ...                                                        # fields from system design §4
    evidence: Annotated[list[EvidenceItem], keep_last_n_per_hypothesis]   # N = 5
    evidence_summary: str                                      # running summary, rewritten by Commander-assess
    timeline: Annotated[list[TimelineEvent], operator.add]     # append-only, feeds postmortem
    memory_context: MemoryContext | None                       # long-term reads, see 4.2
```

| Field | Reducer | Why |
| :---- | :---- | :---- |
| `hypotheses` | `merge_by_id` | parallel Diagnosis branches update different hypotheses at once |
| `evidence` | `keep_last_n_per_hypothesis` | bounded size; older items are folded into `evidence_summary` |
| `timeline` | `operator.add` | never trimmed — it is the audit trail |
| `plan_history` | `operator.add` | trace must show every plan version (Signature A) |
| `budget` | `sum_ledgers` | parallel branches each add their spend |

**Context control — what each agent sees.** Agents never get the whole state. Each node builds its prompt from a **view**:

| Node | Sees |
| :---- | :---- |
| Commander | alert, plan, hypotheses (status \+ last evidence), `evidence_summary`, budget left, engineer prefs |
| Triage | alert, recent changes |
| Diagnosis | its **one** hypothesis, its sub-budget, its evidence |
| Runbook | triage report, supported hypothesis, `memory_context` |
| Remediation | runbook advice, proposed action, dry-run result |
| Comms | timeline, final hypothesis, outcome, engineer prefs |

**Diagnosis scratchpad is discarded.** Each Diagnosis branch is a ReAct subgraph with its own private `messages`. Only its `HypothesisResult` (verdict \+ evidence ids) is merged into the parent state; the tool-call chatter is not. This is what keeps parallel diagnosis from blowing up context.

**Rules.**

- Raw telemetry never enters state; tools return summaries \+ `evidence_id`. **\[Vision §8\]**  
- Secrets are scrubbed by the post-tool hook before anything reaches state. **\[Vision §9\]**  
- Checkpoints hold incident data → same retention/redaction rules as the incident DB.

### 4.2 Long-term memory **\[Proposal\]**

**Namespaces** (the system design's table, made concrete):

| Namespace | Key | Value | Search |
| :---- | :---- | :---- | :---- |
| `("incidents",)` | `incident_id` | `IncidentRecord` | semantic (embedding on `symptom_text`) \+ filter `service`, `fault_family` |
| `("lessons", service, fault_family)` | `lesson_id` | `Lesson` | namespace prefix \+ filter `status="active"` |
| `("engineer_prefs", engineer_id)` | pref id | `EngineerPref` | list all |

```py
class IncidentRecord(BaseModel):
    incident_id: str
    alert_name: str
    services: list[ServiceName]
    fault_family: FaultFamily
    symptom_text: str            # embedded for similarity search
    root_cause: str
    action: Action | None
    outcome: Literal["recovered", "rolled_back", "escalated", "noise"]
    timeline: list[TimelineEvent]
    reviewed_by: str

class Precondition(BaseModel):
    check: Literal["recent_change", "promql_above", "promql_below"]
    args: dict                   # e.g. {"flag": "paymentFailure", "within": "15m"}

class Lesson(BaseModel):
    lesson_id: str               # L-007
    text: str
    preconditions: list[Precondition]
    applies_to: dict             # {service, fault_family}
    source_incident: str
    reviewed_by: str
    status: Literal["active", "withdrawn"]

class EngineerPref(BaseModel):
    text: str                    # "always show the PromQL used"
    created_at: datetime

class MemoryContext(BaseModel):          # what the graph puts in state after reading
    similar_incidents: list[IncidentRecord]       # top 3
    applicable_lessons: list[Lesson]               # preconditions all passed
    rejected_lessons: list[tuple[Lesson, str]]     # lesson + failed check (shown in trace)
    engineer_prefs: list[EngineerPref]
```

**Read points.**

| Node | Reads | How |
| :---- | :---- | :---- |
| 3.1 Intake | `engineer_prefs/<on-call>` | `store.search(("engineer_prefs", eid))` → `memory_context.engineer_prefs` |
| 3.7 Runbook | `incidents/` | `store.search(("incidents",), query=symptom, filter={service})`, k=3 |
| 3.7 Runbook | `lessons/<service>/<family>` | list active lessons for each suspect → **code** evaluates preconditions against live telemetry → split into applicable / rejected |

Precondition check is **code, not model** **\[Vision §8\]**. The model only ever sees a lesson that passed. A rejected lesson is recorded with the failed check, so the misleading-lesson demo (demo 3) shows in the trace. A check that cannot be evaluated (tool error, budget gone) counts as **failed**.

**Write points — only after a person says yes.** Node 3.12:

1. Comms produces `Postmortem{timeline, root_cause, action, lessons_proposed[]}`.  
2. `interrupt({postmortem, lessons_proposed})` — engineer accepts / edits / rejects the postmortem and **each** lesson separately.  
3. Accepted postmortem → `store.put(("incidents",), incident_id, record)`.  
4. Accepted lesson → `store.put(("lessons", service, family), lesson_id, lesson | status="active")`. Rejected lessons are logged with reason (needed for the report), not stored.  
5. Engineer prefs are **not** extracted by the model. They are written only through an explicit UI/API action, to avoid prompt-injected "preferences".

Never written: secrets, raw log lines, customer data from spans, held-out incident details. **\[Vision §8\]**

**Correct / delete.** Incidents: engineer edits or deletes via API. Lessons: set `status="withdrawn"` (kept for audit, never returned by reads). Prefs: list \+ delete.

### 4.3 Memory in evaluation **\[Proposal\]**

- **Memory on/off (E4):** off \= graph compiled with an empty store, Runbook returns an empty `memory_context`. Same graph code.  
- **Isolation:** every eval run uses a namespace prefix `(run_id, ...)` so runs cannot see each other's lessons.  
- **Held-out:** the review node is disabled on held-out runs, so nothing from them is ever stored.  
- **Frozen playbook (E3):** before the held-out run, export all active lessons, hash them, and load the store read-only from that export.

---

## 5\. Open questions

1. Alert rule labels/annotations in §1.2 — agree with Upalli and Asiya.  
2. Who builds which part of the FastAPI app (P3 endpoint vs P5 streaming service \+ UI)?  
3. One alert \= one incident? Or group alerts that fire together (e.g. checkout and payment both alerting) into one incident?  
4. Postgres vs SQLite for checkpointer \+ store (system design open decision) — the graph code works with both.  
5. Embedding model for `incidents/` search — reuse `bge-small-en-v1.5` from the runbook index? (agree with role 2)  
6. Evidence trim size N=5 per hypothesis — tune after the first traces.