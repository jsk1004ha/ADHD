# Hook follow-up fixes — 2026-10-08

This local follow-up is based on B `916456a4007809958df4bf5d4353a36883e4efde`.
The enclosing commit identifies the new source and this test report. B, its
measurement identity and original results remain historical records.

Only two defects are fixed:

- `adhd/native.py::context()` reserves status/profile/plan, SESSION, the complete
  view path and next action before fitting explanatory text into the remaining
  1200-character budget. Bootstrap and restoration use the same reserved fields.
  If those mandatory fields alone exceed 1200 characters, they remain complete
  and optional explanations are omitted. Goal skill paths follow recovery text.
- `scripts/benchmark_hooks.py::sample()` snapshots ledger file identities and byte
  offsets immediately before the measured invocation. It reads only newly
  appended records, or the new file after ledger rotation. Missing measured
  diagnostics remain `null`; equal setup/output byte counts cannot select setup
  diagnostics. External invocation timing and benchmark order are unchanged.

Regression tests cover combined long installation/workspace paths and long
goal/objective/feedback for bootstrap/restoration, an over-budget mandatory path,
missing measured diagnostics with identical setup/output sizes, present measured
diagnostics and ledger rotation. The diagnostic boundary tests mock invocation
with controlled ledger writes; existing hook tests exercise isolated subprocesses.

## New verification records

Python 3.13.12 on Windows, 2026-10-08:

- Before fixes: five new test methods executed against B implementation. The two
  reported defects and the mandatory-path edge case reproduced (five failure
  events including three long-path subtests); diagnostic positive controls passed.
  Receipt: `.adhd/checks/9f537afe3ac644579c81ed968ce9d6b0/receipt.json`.
- After fixes: **143 tests executed, 142 passed, one skipped, zero failures**.
  Modules: `tests.test_hook_guidance`, `tests.test_hook_diagnostics`,
  `tests.test_benchmark_hooks`, `tests.test_native`, `tests.test_native_goal`,
  `tests.test_native_termination`, `tests.test_native_progress` (`unittest -v`).
  Skip: `tests.test_native.NativeTests.test_internal_symlink_blocked`,
  reason `OS does not allow test symlink`.
  Receipt: `.adhd/checks/a9a4a0b34e18497fbf83c2d7d25540bf/receipt.json`.
- `py_compile` passed for both changed source files and both changed/new tests.
  Receipt: `.adhd/checks/2bb94ec27f2f4ffeb06c5efe46c06f21/receipt.json`.
- Central batch: `.adhd/batches/15d8d60819ad427c90b6868f6c92bdd6/report.json`,
  two checks passed, unchanged declared inputs. Complete logs remain beside each
  receipt. Source hashes below match that fresh successful regression receipt.

| File | SHA-256 of tested source bytes |
| --- | --- |
| `adhd/native.py` | `3fa5c3e20961f2b6fa168fd967116c36298b10078b113b97ff7fa182207892ff` |
| `scripts/benchmark_hooks.py` | `52d0eb90f5176818880d62beba3124dd3ce74c32a40da6f65570426d78652ef3` |
| `tests/test_hook_guidance.py` | `6bb5377a3fc74b1bbbd8934773783e5af8bca1922742cac7bec2e75b587cb74d` |
| `tests/test_benchmark_hooks.py` | `fc768056c4d13975d1f1790e50047b920aa0b99ebf4b4f01c1c31ebc73ef0d5f` |

## Preserved historical evidence and limits

- Raw `docs/hook-benchmark-results.json` SHA-256 remains
  `e0ce45de7eee066f2a1df6d5b3dc0f720ae4f8d680c6c6586167b0c667e8135d`.
  Its B identity remains `WORKTREE at adba2f5ae7d75a0fd5e4e299c3211c66a5b3d993`.
- Original `.adhd/hook-review-20261007/ADHD-hook-review.zip` SHA-256 remains
  `8dc29c4f85f4b8be933895e9353c658945c6d5ee1dcf13e4fad4dca9cf5a9294`.
- No full performance experiment or new performance claim; these changed source
  hashes are not relabeled as the previously measured B. No remote publication.
- Test installations use temporary roots. Actual App installation, compaction
  and resumption remain unverified. User installation/configuration/hook trust
  and the preexisting untracked `verification/` files are outside this change.
