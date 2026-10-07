# Hook observation and guidance

This change preserves native event processing and acceptance. It does not install,
upgrade, publish or change any model, permission, router or authentication setting.
The initial checkout was `main`, `2d5c4430290b`; the existing untracked
`verification/` directory is excluded from both commits and left intact.

## A: opt-in observation

Set `ADHD_HOOK_DIAGNOSTICS=1` on the hook process to enable numerical observations;
unset it or use `0` to disable. No configuration file is changed. Records use the
existing rotating `native/SESSION/ledger.jsonl`, event `hook_diagnostics`, and its
existing `FileLock`. They contain no prompt, tool input/output, credentials or
workspace path. Diagnostics are absent from execution `state.json` and model
`view.json`. Existing child-stop usage validation and records are reused.

`perf_counter_ns` is monotonic. The entrypoint starts its internal clock immediately
after importing the timer, before JSON/sys and the entire `adhd.native` import.
`startup_imports` covers JSON/sys imports; `native_import` covers native and its
transitive imports including the diagnostic implementation. Python interpreter startup and the
first timer import are outside this internal interval. `stdin_parse`, `lock_wait`
(FileLock construction/acquisition), `state_read` (including initialization when
missing), `state_prepare` (migration/capability observation), `goal_check`,
`event_dispatch`, nested `inbox`, `persist` (state plus exported view), and
`stdout_serialize` cover the existing meaningful phases. `inbox` overlaps
`event_dispatch`: do not add nested phases as if they were exclusive.

After stdout emission, observation reacquires the same session lock to append to
the ledger. `ledger_lock_wait` includes lock-path preparation and acquisition.
`internal_elapsed_ns` ends after that acquisition, before ledger serialization,
append, flush and final process shutdown. The **external** benchmark timer covers
subprocess launch through exit/output collection, including interpreter startup,
imports, processing, stdout and diagnostic persistence. Neither measure is a
model-response latency or an observed token count.

`emitted_context_chars` counts Python Unicode characters of the decoded strings in
`hookSpecificOutput.additionalContext`, `hookSpecificOutput.permissionDecisionReason`,
`reason`, `systemMessage`, and `stopReason`. `emitted_context_bytes` is their UTF-8
byte total; `emitted_fields` names the fields actually included. Event names,
decision enums and JSON escaping/envelope bytes are excluded. `stdout_json_bytes`
separately measures the serialized envelope plus platform newline. These are
hook-emitted strings: the host can truncate/ignore them further. No tokenizer is
used. Without a valid observed child-stop usage record, `host_usage` is null and
`host_usage_observed` is false. Real model usage comparison remains unavailable.
The combined count explicitly includes UI warning/error and stop-reason strings;
it is not proof that every included field reaches the model. The per-field list
keeps that distinction visible rather than treating the total as consumed tokens.

Only the new diagnostic append has a best-effort exception boundary. Original
validation and state-save errors still propagate into the hook's existing error
JSON. That error output can also be measured once a valid session/workspace was
identified. Pre-parse/unsupported/owner-bypassed events cannot acquire a trusted
ledger identity and produce no internal record; their external output/elapsed
time is still measurable. A missing diagnostic is unknown, never zero.

## Reproduce

Use an existing Python environment; the runtime reuses the bundled filelock.
No new dependencies or installation steps are introduced.

```powershell
python -m unittest tests.test_hook_diagnostics -q
python -m scripts.benchmark_hooks --ref baseline=2d5c443 --ref A=WORKTREE --rounds 5 --warmups 1 --scenario idle_prompt --scenario idle_cli_begin --scenario active_prompt --scenario active_stop --out .adhd/hook-baseline-A.json
```

WORKTREE copies tracked/staged files, preserving current source bytes; stage only
the assigned new source files first. Existing untracked user outputs are excluded.
Git archives are extracted into a temporary directory; no reset/checkout occurs.
`--tree LABEL=PATH` accepts an already prepared trusted source tree. The script
never installs hooks. Every sample uses a fresh temporary CODEX_HOME, ADHD_HOME
and workspace. Startup/original-prompt/optional actual CLI begin setup runs outside
the measured interval; the measured PostToolUse must acknowledge/record begin.
Source and on/off order alternate per round. Warmup samples are discarded.

The fixture, source hashes, OS, Python and vendored filelock version, exact argv,
repetitions and initial-state conditions are in the JSON report. Raw samples are
retained with medians and nearest-rank p95. Baseline has no opt-in diagnostic
implementation, so its requested `on` mode correctly remains unobserved. Results
are synthetic isolated hook executions, not actual App compression or live model
usage measurements. Final before/A/B measurements and validation are recorded
below after the separate guidance change.

The full regression suite exposed a reviewed public-file-list omission when
diagnostics were initially imported as a new runtime module. The implementation
now resides in the already packaged `adhd/native.py`; the source-side helper
reexports it for developer tests. The public bundle allowlist and installation
policy stay unchanged. The initial five-round baseline dataset retained in this
A commit predates that layout repair; its exact old source hashes are recorded.
The final comparison remeasures the repaired A against the baseline and B.

## B: profile guidance and restoration

Guidance reuses `execution_profile()` and the dictionary's canonical name/depth:
simple is optional planning with direct/limited delegation; standard is a brief
requirement-covered plan; deep retains its detailed plan. The directly referenced
native skill and protocol describe the same rules. Validation, review, models,
effort and delegation policy are unchanged; profiles are not automatically chosen
or promoted.

First contact supplies common rules, the skill, session/view and installed begin
entrypoint. Repeated unchanged UserPromptSubmit context is empty. Every such
event still validates, collects originals, tracks pending intent, handles
stop/resume/goal, observes capabilities/goals and persists state/view. A private
guidance signature is only output bookkeeping; it does not skip processing or
saving and is not exported in the model view. Status/contract/intent/plan/goal
changes emit current conditions and the next action. Idle is never used as a
task-size classifier; substantive work retains the bootstrapped begin path.

SessionStart restores the current objective, profile, unmet independent acceptance
and next action, including `source="compact"` and `source="resume"`. It does not
automatically resume paused work. Long excerpts are bounded with a full-view
pointer. Actionable conditions precede common reminders inside the existing
1200-character guidance budget; head/tail truncation of the whole restoration
message would lose its middle next-action field, so the final limit retains the
prefix. PostCompact/SubagentStop retain their existing empty additional-context
output and still process/persist their state. No new event handler is introduced.

This character budget is independent of the existing handler setting
`additionalContextLimit=1200`: Codex documents that setting as an approximate
token threshold for spilling full output to disk and sending a preview. It is
not a character limit or a hard token budget, and its setting is unchanged.

Local host evidence: `codex --version` and its npm manifest report `0.159.2`.
The matching official [SessionStart input schema](https://github.com/openai/codex/blob/rust-v0.159.2/codex-rs/hooks/schema/generated/session-start.command.input.schema.json)
accepts `startup`, `resume`, `clear`, `compact` and `fork`. Its
[output schema](https://github.com/openai/codex/blob/rust-v0.159.2/codex-rs/hooks/schema/generated/session-start.command.output.schema.json)
supports `hookSpecificOutput` with `hookEventName="SessionStart"` and string
`additionalContext`. Tagged
[PostCompact](https://github.com/openai/codex/blob/rust-v0.159.2/codex-rs/hooks/schema/generated/post-compact.command.output.schema.json)
and [SubagentStop](https://github.com/openai/codex/blob/rust-v0.159.2/codex-rs/hooks/schema/generated/subagent-stop.command.output.schema.json)
omit additionalContext. The [official hook documentation](https://developers.openai.com/codex/hooks)
describes compact restoration before the next model request; its source-value
list omits `fork`, which is present in the version-tagged schema. These source
checks do not attest a newly installed App binary or a real compaction run.

## Validation and boundaries

New tests cover phase/text/envelope semantics, disabled logging, unchanged state,
best-effort logging failure, propagated validation/save failure, measured error
output, existing usage reuse, concurrent ledger writes and valid/invalid inbox
processing. Guidance tests cover all profile depths/pins, first-contact entry,
quiet prompt preservation, actual CLI begin acknowledgement/processed records,
amendments, mandatory saves despite empty output, compact/resume reconstruction,
explicit stop/resume, session pause, goal guidance, existing unsupported-event
schemas and long-message recovery. There are 27 new tests in the two modules.

```powershell
python -m unittest tests.test_hook_diagnostics tests.test_hook_guidance -q
python -m unittest discover -s tests -q
python -m unittest scripts.test_audit_local_install -q
python -m compileall -q adhd tests scripts/benchmark_hooks.py
git diff --check HEAD
```

The checks run through the installed `adhd.py batch run` with explicit file
subjects and retained receipts/full stdout/stderr under `.adhd/checks/`. The
first full run found the packaging omission described above; that failure was
repaired without weakening the test. The isolated ZIP/venv/registered-hook smoke
then passed. Final batch and benchmark receipts are recorded below.

Confirmed scope: current Windows/Python subprocess hooks and isolated native
state-machine/regression/package behavior. Unverified scope: newly installed
real App/CLI compaction/resume, real model usage or response latency, token/cost
savings, Windows App GUI delivery beyond emission, remote CI and other OS/Python
versions. Actual user installation/auth/config/hooks/router/model settings were
not changed. No remote push, PR, release or persistent installation was performed;
the release smoke builds and installs only inside temporary test directories.

Follow-ups deliberately excluded: native-wide usage/hard token accounting,
automatic profile promotion, idle/non-MCP early returns, inbox pre-glob gating,
filelock replacement, dirty-state save skipping, skip-policy changes,
source-only installation, MCP default changes, protocol restructuring and weaker
review/acceptance gates. Any future performance claim needs new measurements.

## Final measured comparison

Final command (repaired A; B captured as exact WORKTREE source bytes):

```powershell
python -m scripts.benchmark_hooks --ref baseline=2d5c443 --ref A=adba2f5 --ref B=WORKTREE --rounds 5 --warmups 1 --out docs/hook-benchmark-results.json
```

Windows Windows-11-10.0.26200-SP0; Python 3.13.12; vendored filelock 3.29.0. Five measured repetitions and one discarded warmup per combination; 8 scenarios × 3 versions × 2 requested diagnostic modes = 240 measured hook processes. Setup/CLI queueing is outside measured intervals. No repository test batch ran concurrently with this final benchmark. Ordinary ambient desktop load was not controlled. With n=5, nearest-rank p95 is the maximum observed sample; it is a small-sample empirical tail, not a precise population estimate.

External subprocess times with diagnostics off, in milliseconds (median / p95):

| Scenario | Before | A instrumentation | B guidance | Emitted chars before / A / B |
| --- | ---: | ---: | ---: | ---: |
| idle_prompt | 404.042 / 572.224 | 456.126 / 645.079 | 347.447 / 420.823 | 1126 / 1126 / 0 |
| idle_cli_begin | 453.649 / 2050.044 | 513.921 / 912.179 | 500.113 / 780.750 | 261 / 261 / 261 |
| idle_post_tool | 346.442 / 366.453 | 285.544 / 389.509 | 288.820 / 334.758 | 0 / 0 / 0 |
| active_prompt | 296.877 / 335.415 | 315.565 / 321.030 | 289.235 / 317.709 | 1222 / 1222 / 515 |
| active_stop | 286.006 / 396.508 | 278.869 / 424.968 | 272.199 / 289.617 | 277 / 277 / 277 |
| active_compact_start | 290.301 / 337.542 | 305.402 / 322.862 | 259.337 / 291.842 | 1129 / 1129 / 690 |
| active_resume_start | 276.255 / 396.622 | 278.190 / 331.934 | 294.109 / 340.628 | 1129 / 1129 / 690 |
| active_post_compact | 305.116 / 319.441 | 296.266 / 313.666 | 273.990 / 474.727 | 0 / 0 / 0 |

Requested diagnostic on/off comparison, external milliseconds (off median → on median; signed observed difference):

| Scenario | A | B |
| --- | ---: | ---: |
| idle_prompt | 456.126 → 440.338; -15.787 | 347.447 → 340.608; -6.839 |
| idle_cli_begin | 513.921 → 493.084; -20.837 | 500.113 → 777.302; +277.189 |
| idle_post_tool | 285.544 → 330.935; +45.391 | 288.820 → 306.600; +17.780 |
| active_prompt | 315.565 → 291.520; -24.044 | 289.235 → 290.892; +1.658 |
| active_stop | 278.869 → 283.489; +4.620 | 272.199 → 302.045; +29.846 |
| active_compact_start | 305.402 → 237.157; -68.244 | 259.337 → 278.595; +19.258 |
| active_resume_start | 278.190 → 266.872; -11.319 | 294.109 → 318.395; +24.285 |
| active_post_compact | 296.266 → 297.836; +1.570 | 273.990 → 294.187; +20.197 |

The sign and magnitude vary, including negative differences from noise and a large positive B CLI-begin sample. These measurements do not establish a consistent process speedup or a stable causal overhead estimate. On/off modes preserve emitted characters/bytes for every version/scenario, and the runner verifies original intent, expected status and CLI processing. A preserves before-change emitted sizes; B reduces only selected guidance. Required CLI acknowledgements, Stop continuation and empty PostCompact processing retain their outputs. Real model usage is null; no token/cost savings are claimed.

The JSON retains every sample, envelope/context byte counts, internal phase observations, source/fixture hashes and command/environment. Baseline requested-on rows have no diagnostic implementation and correctly record no internal observation.

Fresh full batch before the final explanatory comment correction:
` .adhd/batches/b9618384cf3e446497b4193c0796e58b/report.json ` (4 passed checks). Regression receipt `.adhd/checks/1c2c22cce14948a49e33048c548c07ad/receipt.json`: 539 tests, 9 existing skips, zero failures; installation audit: 1 test passed; compile and diff passed. After this batch, only a source comment was corrected to distinguish the host token threshold from the independent character budget; the native compiler AST was compared with the hash-bound tested source and remained identical. Current targeted tests and final report/scope verification bind the final bytes separately.

Changed files: A uses `hook.py`, `adhd/native.py`, source-side `adhd/hook_diagnostics.py`, `scripts/benchmark_hooks.py`, `scripts/fixtures/hooks/events.json`, `tests/test_hook_diagnostics.py`, and the two report files. B changes guidance in `adhd/native.py`, the native skill/protocol, adds `tests/test_hook_guidance.py`, extends diagnostic regression coverage and updates measurements/report. Existing user `verification/` remains outside the staged files.
