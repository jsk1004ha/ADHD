# Delivery evidence and execution decisions

The native controller keeps one v2 state and one acceptance gate. A new `begin`
may add `preserve_conditions`, `runtime_context`, `delivery_target`, and an
explicit `deadline`. Each entry names an existing requirement ID and a recorded
user turn. Missing entries remain unknown. `sync-intent` adds requirements by
default; replacements and retractions use explicit operations. A repeated
deadline with the same `due_at` keeps the first observed timestamp and turn.
The optional fields require a fresh `SessionStart` from this reader. Existing v2
states still load, while future state versions and `min_reader_version > 2` are
rejected before mutation. Managed install/upgrade/rollback checks active state;
new schema writes are not enabled. The new CLI also refuses to queue metadata
or delivery requests unless the bridge view reports this reader and the managed
installation points to this release. A manually invoked old binary outside the
managed path remains outside that check.

For coding runs with new contract metadata, run the declared batch after
assembly and attach its passed report before `candidate`:

```text
python adhd.py batch run --workspace PROJECT --spec-file checks.json
python adhd.py native attach-batch --session SESSION --workspace PROJECT --payload-file batch-ref.json
```

The batch manifest should declare `subject_paths`, `dependency_paths`,
`fixture_paths`, `environment_vars`, `requirements`, and whether impact and
environment declarations are complete. `critical`, external-state and
time-sensitive checks always run fresh. Reuse additionally needs an unchanged
run/revision/contract, per-check definition/dependencies, executable, declared
environment and validated receipt. An unrelated check addition or another
check's environment change does not invalidate unchanged checks. Missing impact
or environment completeness disables reuse. `mandatory_checks` requires passing
evidence; it does not imply `critical: true`. Diagnose a plan without executing
it using `batch diagnose --spec-file checks.json`. See
[verification efficiency](verification-efficiency.md) for grouped repair,
final-freeze ordering and observed execution counts. `batch build-decision`
accepts a JSON manifest with artifact, source/toolchain/dependency hashes,
flags and environment hash. Hash maps use workspace-relative file paths. An
earlier successful receipt must cover every declared input and the exact output
bytes; fresh receipt validation rehashes those files before recommending reuse.
Missing paths, unmatched hashes or output-only receipts disable reuse. The
caller still declares input completeness, flags and custom environment:

```text
python adhd.py batch build-decision --workspace PROJECT --spec-file build.json --previous-build previous-build.json
```

An identical failed check with unchanged declared inputs is held instead of
executed again. A `retry_policy` entry naming that check may allow one
`bounded_flake_probe` with `max_attempts: 1`, a reason and the prior failed
receipt as `evidence_ref`. A second identical verified failure requires a new
plan hypothesis; changed inputs and critical checks still receive fresh checks.

`native record-action` stores an operation, canonical targets, scope digest,
exact user excerpt, source turn and optional exact host tool request hash.
`native authorize-action` binds a later brief approval only when one action is
pending and the exact action proposal was observed in the prior assistant
message, or binds an explicit observed user excerpt to its own turn. This
records conversation authorization; host permission remains unknown. A matching
host `PostToolUse` denial is classified and retains its tool ID/request hash.
The matching `PreToolUse` request is held after an identical denial. A plain
“blocked by policy” message has an unknown detailed reason; only structured
host policy codes are classified as host-policy denial. A missing host event
cannot be converted into a verified denial. Failed CLI checks can be recorded
through `native action-outcome`; they remain distinct from a host policy block.

For release or live delivery, `candidate.delivery_evidence` requires separate
publish and verification receipts. Release verification must observe the hash
of a declared artifact included in publish subjects. Live verification must
observe the expected SHA and healthy result, while publish subjects cover the
declared source paths. Verification subjects must cover the same artifact or
source paths. Push and CI alone leave delivery incomplete. These
structural checks remain subject to independent review of the actual receipt
commands, remote source and current output. The bridge view reports artifact,
acceptance and delivery status separately. Deadline guidance activates at
50% and 80% of the explicit interval and when overdue, without changing
mandatory requirements or treating a deadline as publication authority.

`native read-observation` records a question/scope/source-hash/section key and
returns whether an unchanged read can be reused; independent review reads
remain fresh. `native wait-observation` returns 5, 15, 30 or 60 second poll
guidance and resets on changed status. Native child packets preserve exact
original-language excerpts on disk with revision and recovery path. An intent
amendment exports a delta for each running child; forwarding that delta remains
the director's responsibility. No hook runs checks, builds or deployments.

Observed token accounting can be summarized with
`python adhd.py metrics summarize --input-file events.json`. Missing provider
fields stay unknown, repeated IDs do not double count, cumulative resets start
new segments, and child values explicitly included in parent totals are not
added again. For a fixed paired experiment, use:

```text
python scripts/benchmark_delivery_efficiency.py --baseline-tree .adhd/efficiency-20261010/baseline-source --candidate-tree . --output benchmark.json
```

This executable replay runs one benign failed check five times per arm in
separate temporary homes, carrying each report forward. It records actual
process starts, distinct failed receipts, raw wall durations and the same
`needs_repair` outcome. It also measures a worker prompt resend and a valid
context delta on the same card, checking that original-language excerpts and
the recovery reference survive. It is a failure-policy microbenchmark:
provider tokens, first usable artifact and actual delivery time remain unknown.
No installation, model call or network operation is part of the fixture.

For later observed successful task pairs, use:

```text
python scripts/compare_delivery_efficiency.py --baseline baseline.json --candidate candidate.json --output comparison.json
```

Each input is an array of observed rows keyed by `case_id`; paired records
must have the same model, effort, scope and environment hashes. Acceptance
must include passed requirements, preservation, security and independent review,
plus delivery for a release/live task. A zero baseline or missing metric yields
`null`; three pairs are exploratory, not statistical proof. Keep deterministic
replays and hook/payload microbenchmarks separate from live model token and
time outcomes. This driver never invents provider usage or claims causal
savings from a fixture.
