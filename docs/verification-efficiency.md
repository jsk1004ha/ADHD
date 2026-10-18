# Fewer verification rounds with current evidence

Prepare one sufficient check plan while implementing. Finish a coherent repair
group before running its affected checks; do not start a full suite per file edit.
Review the change and finish cleanup before freezing the final candidate. A later
review fix invalidates its affected checks and dependent checks, not every earlier
pass. Required repository checks still run. Use a full suite when the repository
requires it, common foundations change, or the impact scope cannot be established.
Do not infer complete coverage from file names or a static import guess alone.

`mandatory_checks` names checks whose valid passing evidence is required.
`critical: true` instead requires actual execution in each batch. External-state
and time-sensitive checks also run fresh. Missing impact or environment coverage
forces execution. Neither diagnosis nor a budget silently lowers these gates.

## Explicit time limits and earlier failure feedback

After installing this version and starting a managed session, a native `begin`
can set a total wall budget and reserve part for verification and review:

```text
python adhd.py native begin --session SESSION --workspace PROJECT --payload-file begin.json --time-limit-seconds 900 --review-reserve-seconds 180
```

These are opt-in seconds, also available as `time_limit_seconds` and
`review_reserve_seconds` in the begin payload. Without a total limit, legacy
behavior is unchanged. A reserve needs a total and must be smaller than it;
the default reserve is 20% of the total, capped at 300 seconds. Explicit task
limits apply in goal mode and retain the original expiry across resume.
In the reserved window, finish implementation and focus on checks and independent
review. Exhaustion preserves unfinished work; it never means success.

The native controller observes deadlines at host hook boundaries. It cannot
interrupt an in-flight host/model call. Use a shared batch budget to bound
checks launched by ADHD itself. Additional top-level batch fields are:

```json
{"time_limit_seconds":600,"scheduling":"shortest_first","fail_fast":true}
```

Each check may declare `estimated_seconds`. Estimates only order ready checks
and inform `batch diagnose`; unknown durations remain unknown. Dependencies and
exclusive resources still apply. Defaults retain declared order and execution
of independent checks after a failure. With `fail_fast`, the failed iteration
defers unstarted checks so the cause can be fixed sooner. Every mandatory check
still needs valid passing evidence before acceptance. A skipped, failed, timed
out or budget-exhausted batch cannot be attached as a passed batch.

The batch uses one monotonic deadline, including input preparation; each owned
process receives the smaller of its normal timeout and the remaining budget.
Expiry before launch records an unstarted check without manufacturing an observed
process receipt. Existing process-tree cleanup is retained. Hashing, filesystem
operations, process startup, the runner polling interval and cleanup may add
overhead; this is not a hard operating-system quota. The CLI exits nonzero for
an exhausted batch. Standalone `check` specs may also set `time_limit_seconds`.

`scripts/benchmark_time_budget.py` compares a fixed, deliberately failing local
iteration and its repair. It retains every run and confirms that the successful
repair still runs all mandatory checks. This measures early failure feedback,
not coding-agent, token or production speedup. Global settings and model/effort
are never changed by these options.

Native time budgets require this implementation in the managed hooks and a fresh
SessionStart reader. A newer CLI refuses to queue budget requests to an older
reader, which could otherwise ignore the new fields. Building a source package
does not upgrade the currently running App session or its global installation.
After expiry, interrupting a child remains allowed. General tools and new children
remain blocked even when a new human turn is pending. Only the exact installed
Python/entrypoint command for `native status` or payload-free intent classification
is allowed, in this order (substitute the actual paths and recorded identifiers):

```text
& "PYTHON" "MANAGED_ENTRYPOINT" native sync-intent --session SESSION --workspace "PROJECT" --classification new_task --source-turn-id PENDING_TURN --base-revision REVISION
```

Use `no_change` for a status-only turn. A new task archives the expired contract;
it cannot reset or accept that contract. The still-running session's actual reader
and role observations are retained and rechecked. No arbitrary payload file,
extra shell statement, shell substitution, model call or spawn is allowed through
this exception. In `functions.exec`, the control transport must be exactly
`text(await tools.exec_command(JSON_OBJECT_WITH_CMD));` using a literal JSON object
with the exact command as `cmd`. This is a workflow boundary, not a security
sandbox or a host quota.

```text
python adhd.py batch diagnose --workspace PROJECT --spec-file checks.json
python adhd.py batch run --workspace PROJECT --spec-file checks.json
```

Diagnosis is read-only and starts no checks. It flags all-critical plans, shared
input scopes and incomplete declarations. Common scopes may be correct: inspect
the warning and declare each check's actual sources, dependencies and fixtures.

A minimal check entry uses the existing fields:

```json
{
  "id": "search_behavior",
  "argv": ["python", "-m", "unittest", "tests.test_search", "-q"],
  "subject_paths": ["src/search.py", "tests/test_search.py"],
  "dependency_paths": ["src/search_types.py"],
  "fixture_paths": ["tests/fixtures/search.json"],
  "environment_vars": ["SEARCH_MODE"],
  "impact_complete": true,
  "environment_complete": true,
  "requirements": ["R1"]
}
```

These are illustrative paths, not proof that another project's declarations are
complete. Include the interpreter/runtime dependencies and environment affecting
the real command. Put `search_behavior` in `mandatory_checks`; add `critical`
only when fresh execution is required. Do not change an existing repair plan to
drop or weaken checks; additions may reuse unchanged passing evidence.

Pass `previous_report` in the next plan to connect rounds. Reuse binds the same
run, contract revision/hash, check definition, dependency definitions, actual
executable, declared environment and current input bytes. An unrelated check
addition or another check's environment change does not invalidate this check.
If a prerequisite executes afresh, dependent checks also execute afresh. Old
reports without per-check keys retain conservative whole-plan reuse. An unknown
scope, changed tool, input, environment, revision or invalid receipt forces a
new execution. Identical failed inputs stay failed and held unless the existing
single justified flake-probe policy applies; a held failure cannot complete work.

Execution logs remain complete on disk. Read one batch summary, confirm the
suspected failure groups, assign one owner per cause, and collect related fixes
before the next repair round. Do not create one agent or retry loop per test.

Reports store and return `verification`: round, previous report, new/reused/held/
blocked checks, invalidation reasons, observed processes and wall duration.
`usage.check_processes` counts new process receipts; failed setup attempts without
receipts are reported separately as unobserved execution attempts. Provider
tokens remain `null`. A shared receipt is verified once within each criteria or
report-validation call. Executable content is hashed once per distinct resolved
path in each snapshot/tool-check call. Later calls start fresh; neither cache
survives another approval boundary or uses file timestamps instead of content.

Stop after all requirements have current valid evidence, the scope is preserved
and independent acceptance has no blocking finding. Do not run another full suite
or cleanup pass just to obtain a newer timestamp. A workflow owner such as Ralph
must retain its own required gates; coordinate its cleanup and final freeze rather
than starting a second ADHD execution loop.

For an observed before/after fixture, preserve the pre-change `adhd/native.py`
and `adhd/validation_batch.py` in a separate directory, then run:

```text
python scripts/benchmark_verification_efficiency.py --baseline-tree BEFORE --output counts.json
```

The fixed successful workload covers an initial batch, leaf repair, additional
mandatory check, isolated environment change and unchanged repeat. It measures
real local check counts and shared receipt validations. One fixture pair does not
establish model token savings or reliable end-to-end time savings.
