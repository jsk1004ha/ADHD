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
