# ADHD native quickstart

Read this first for a substantive run. Open a section of `protocol.md` only when its row in the last table applies, or when the hook rejects a request and names that topic. Trivial questions are answered directly; no durable run is needed.

## Transport
Use the SESSION and `view.json` path from the hook. Commands run through the normal shell tool and return `queued`; the PostToolUse hook applies the request and exports a receipt. Read the receipt or the updated view next, and never wait for it inside the same command.

`"ADHD_PYTHON" "ADHD_ROOT/adhd.py" native OP --session SESSION --workspace "WORKSPACE" --payload-file ".adhd/OP.json"`

Common OP values: `begin`, `plan`, `checkpoint`, `candidate`, `sync-intent`, `pause`, `blocked`, `status`. Conditional operations are `goal`, `attach-large`, `attach-batch`; consult the matching references before using them. PowerShell uses `&` with quoted paths.

Delivery/action evidence operations: `record-action`, `authorize-action`, `action-decision`, `action-outcome`, `read-observation`, `wait-observation`. Read `docs/delivery-efficiency.md` at ADHD_ROOT before using them. These commands record/check authority; connected credentials alone do not authorize an action.

## Payloads
Begin (modes: research, study, report, coding, game; kinds: artifact, test, visual, source, reasoning, behavior, provenance, browser, mcp). Declare real output files:
```json
{"mode":"coding","criteria":[{"id":"R1","text":"Requested behavior works and existing behavior remains intact","kind":"behavior"},{"id":"R2","text":"Relevant regression tests pass","kind":"test"}],"artifacts":["src/main.py","verification.txt"],"assumptions":[],"non_goals":[]}
```
Plan (`standard`; `deep` adds `alternatives`, `risks`, `preflight` lists). Every criterion appears in a step, and dependencies point to earlier steps:
```json
{"objective":"Requested result","approach":"Chosen path and reason","verification":"Actual acceptance commands","steps":[{"id":"S1","action":"Implement and verify","depends_on":[],"requirements":["R1","R2"]}]}
```
Check (a receipt is the only evidence for a test criterion): `"ADHD_PYTHON" "ADHD_ROOT/adhd.py" check --workspace WORKSPACE --spec-file CHECK.json`, where the spec holds `run_id`, `contract_revision`, `argv` (array) and `subject_paths`.

Checkpoint, with `criterion_results`, `completed_steps` and `failure` added only from real receipts (`failure` carries `category`, `detail`, `evidence_id`):
```json
{"summary":"What changed and what was checked, with file pointers","next_action":"The next unresolved action","evidence_ids":[".adhd/checks/CHECK_ID/receipt.json"]}
```
Candidate, once every criterion has fresh evidence:
```json
{"files":["src/main.py","verification.txt"],"criterion_results":[{"id":"R1","pass":true,"evidence":"What was checked"},{"id":"R2","pass":true,"evidence":"Regression passed","evidence_ids":[".adhd/checks/CHECK_ID/receipt.json"]}],"sources":[],"procedure":["Reusable step","How to verify next time"]}
```
After the acknowledgement, spawn `adhd-verifier` on the exact `candidate.digest` with the view path. Never self-approve, and do not edit files after approval. A rejection means: fix the cause, rerun the affected checks, submit a new candidate. Missing auth, tools or models are reported as `blocked`, never as success.

## When to open protocol.md
| Situation | Section |
| --- | --- |
| Git checkout and coding task (capture scope BEFORE edits and begin) | `## Coding scope` |
| Native `goal`, pause/resume and explicit limits | `## Loop, resources and stopping` |
| `attach-large` or `attach-batch` | `large-task-roles.md`, `docs/large-tasks.md` or `docs/verification-efficiency.md` at ADHD_ROOT |
| User changed a condition mid-run (`sync-intent`), provenance, browser or MCP criteria, study `learning_check`, large artifacts | `## Work, checkpoint, then independent acceptance` |
| Delegating to scout, light, architect or implementer | `## Use agents only where they add value` |
| Pause, blocker, budget, resume semantics | `## Loop, resources and stopping` |
| Reusable method memory | `## Successful-method memory` |
| Office or PDF deliverable | `v12-gates.md` section `## Protected originals and document constraints` |
| Deep plan example | `v12-gates.md` section `## Plan` |
