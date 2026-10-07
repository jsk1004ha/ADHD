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
