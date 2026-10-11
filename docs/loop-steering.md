# Loop steering and read budgets

Goal runs retain their existing stopping policy. Without an explicit stagnation
limit, repeated no-progress rounds request a changed plan instead of ending the
run. The default stage boundaries are 3, 6 and 9 rounds. Each new stage emits one
intervention; fresh outcome progress resets the stage. Explicit limits still take
precedence. Waiting for a running child or independent reviewer does not add
stagnation or consume pending loop warnings; time and round limits still apply.

Artifact-byte changes have three grace rounds since the last verified outcome.
After that, edits alone cannot reset stagnation. A repeated tool failure contributes
at most one extra no-progress round; fresh verified progress wins over that signal.
Within the artifact-change grace, a pending warning preserves the prior stagnation
and adds that one round instead of resetting the counter. The first baseline Stop
also counts a pending warning, without adding an ordinary no-progress round.
A redelivered Stop does not consume the warning twice.

PostToolUse observations distinguish terminal success, terminal failure and unknown
results. Known exit codes take precedence over words such as `failed` in stdout.
Unknown/in-progress results do not erase prior failures. A command receives warnings
at its third and fifth matching failure; terminal success rearms its warnings.
Exact tool-use IDs are deduplicated when available. Different calls without IDs
are not deduplicated merely because their contents match. Alternating A/B failures
are handled by the same per-fingerprint threshold, not a second warning counter.
Warnings cannot forcibly stop a model inside a turn.
Diagnostic commands redact secret flags before serialization and truncation,
including quoted values and PowerShell quote/backtick escapes.

Only a valid, contract-bound reviewer rejection contributes to repeated-finding
steering. Broken JSON, stale candidates and profile mismatches retain their own
repair paths. Similar free-text findings are a heuristic, not proof that the
reviewer raised precisely the same defect. A changed plan hash is likewise not
proof of a changed implementation strategy: cite failed evidence, change an
assumption in `approach`, and name the smallest discriminating check in `verification`.

The verifier reads `skills/adhd-native/references/verifier-card.md`; document,
deep and coding-scope gates remain conditional requirements. The director begins
with `protocol-quickstart.md` and follows its references when applicable. View
hints compare decision-relevant fields, not every timestamp or counter, and
compaction/resume restores the instruction to read current state.

## Optional role effort

The default role pins remain unchanged. Only `adhd-scout` and `adhd-light` accept
`low` or `max` in `native-policy.json`:

```json
{"role_effort":{"adhd-scout":"low","adhd-light":"low"}}
```

The policy is frozen at begin. Installation renders that policy into the native
agent profiles. After changing it, run upgrade and start a new session; doctor
reports invalid policy, missing/invalid profiles and effort drift separately.
Existing runs never silently adopt a newly edited policy. Parent, verifier,
planner and implementer effort cannot be lowered through this option.

## Verification and measurement boundaries

Unit and subprocess hook tests establish controller behavior; isolated installation
tests establish rendering/upgrade consistency. They do not establish actual model
token savings. Compare identical model/effort/input/environment runs using
`scripts/compare_delivery_efficiency.py`. Preserve its quality gate and missing-data
`inconclusive` verdict. A changed-effort experiment belongs in a separate report.
The comparator requires at least three pairs per task type and observed usage for
its exploratory savings result. Loop counts and repeated review findings need
separate case-linked observations; do not invent unavailable provider usage.
