# Native quickstart

Use the hook's session, view and installed Python/ADHD_ROOT paths. Commands queue
requests; inspect the next acknowledgement. Never modify controller state/view.json.

```text
PYTHON ADHD_ROOT/adhd.py native OP --session SESSION --workspace WORKSPACE --payload-file request.json
```

Use `begin`, `candidate` and `sync-intent` for the normal flow; `status`, `plan`,
`checkpoint`, `pause`, `blocked` and `goal` when needed. Advanced operations are
`attach-large`, `attach-batch`, `record-action`, `authorize-action`,
`action-decision`, `action-outcome`, `read-observation` and `wait-observation`.
Use `native --help` for flags. This is a reference, not a required sequence.

Begin with actual requirements and output files. Auto is the default; a formal
plan and separate reviewer are optional. Active runs retain their existing profile.

```json
{"mode":"coding","criteria":[{"id":"R1","text":"Requested behavior works","kind":"test"}],"artifacts":["src/main.py"]}
```

Run relevant checks with the current run_id and intent_version from view.json:

```text
PYTHON ADHD_ROOT/adhd.py check --workspace WORKSPACE --spec-file check.json
```
```json
{"run_id":"RUN","contract_revision":1,"argv":["python","-m","unittest","tests.test_feature"],"subject_paths":["src/main.py","tests/test_feature.py"]}
```

Candidate after the check, using its actual receipt:

```json
{"files":["src/main.py","tests/test_feature.py"],"criterion_results":[{"id":"R1","pass":true,"evidence":"Feature and failure-path regressions passed","evidence_ids":[".adhd/checks/ID/receipt.json"]}]}
```

Auto validates requirements, evidence and current artifacts, then completes without
a mandatory stopping point at implementation or tests. Report any unfinished
requested work as `remaining_work: ["Finish requested delivery"]` on checkpoint
or candidate; it stays active across compaction. Clear with `remaining_work: []`
after finishing it. Auto continues past implicit round/time/stagnation thresholds;
explicit limits, actual blockers and user stop requests remain effective.

Completion requires the full requested result and current evidence, without
a mandatory second model. Add `independent_review: true` to candidate when an
independent reviewer would help. Coding/game completion needs an artifact-related
execution receipt or declared browser/MCP observation. For documents, supply
`document_reviews` with the current render_sha256, inspected_pages, pass and
concrete evidence (and inspected_layout_checks if declared); this is recorded as
parent_checked, not independent approval. Failed/stale checks cannot pass. Preserve originals and
inspect the diff. Declare coding_scope or a batch only when useful or explicitly
required. For mandatory `adhd-verifier` review, choose --profile standard or deep and
use the [strict protocol](protocol.md).

Feedback: standalone status/approval replies do not invalidate auto work. Actual
changes use sync-intent with the pending source_turn_id, base_revision and
classification (no_change, amend, new_task). An amend contains add/replace/retract
operations, for example replacing criteria/R1. Preserve unrelated requirements,
check affected behavior and submit the updated candidate. Auto needs no plan rewrite.

```json
{"source_turn_id":"TURN","base_revision":1,"classification":"amend","operations":[{"op":"replace","target":"criteria/R1","value":{"id":"R1","text":"Also support Korean","kind":"test"}}]}
```

Checkpoint is optional for long work. Record concrete failures and a changed next
action when useful, not paperwork after every success. User authorization and host
denials are separate. Report missing required capabilities honestly.
