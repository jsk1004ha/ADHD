# Coding discipline

Read this reference for code edits and reviews. Apply it within the user's actual
request and the selected workflow; simple tasks need only proportionate checks.

## Resolve the important assumptions

Identify the requested result, existing behavior and evidence needed to finish.
Inspect the relevant source before choosing an approach. Ask when uncertainty
materially changes the result, authority or risk. Choose reversible details and
record their reason without repeatedly asking permission. Each tool call should
answer a current question, make a required change or verify a stated claim.

## Keep the implementation small

Reuse an existing function or pattern when it fits. Introduce a new abstraction,
option, feature or dependency only when the current requirement needs it. Keep
real validation, security checks and failure handling. Line counts are evidence
to inspect, not a correctness rule or a reason to remove necessary behavior.

## Preserve the surrounding work

Separate pre-existing changes from this task before editing. Match local style;
avoid unrelated formatting, comment edits and cleanup. Remove only code made
unused by this change. For a Git task capture a coding-scope baseline through
the normal CLI before `native begin`, then declare it as `coding_scope`. An
unmapped change cannot pass the candidate gate. See [the protocol](protocol.md).

## Verify and finish

Connect changed files and their relevant hunks to requirement IDs. Use checks
that discriminate correct behavior from the original failure; an unrelated
successful command is not proof. The independent reviewer checks scope,
preservation and simpler viable approaches against the actual diff. Once the
requested result has sufficient evidence, finish. Repeat a failed approach only
when new evidence or a changed hypothesis justifies it. Review judgments about
complexity remain judgments; the file-scope gate is not a tool sandbox.

Source: an independently written ADHD adaptation of the four principles in
[multica-ai/andrej-karpathy-skills](https://github.com/multica-ai/andrej-karpathy-skills/blob/2c606141936f1eeef17fa3043a72095b4765b9c2/skills/karpathy-guidelines/SKILL.md),
commit `2c606141936f1eeef17fa3043a72095b4765b9c2`, source blob
`6a62d0441753157ca6ca50479e490c2948033adb`, SHA-256
`6e22cc54cb02a5e98ae42d06d9d7292db0c1b43894831b32879beb0166b2aea7`.
That community skill attributes its ideas to Andrej Karpathy's observations and
declares MIT. The upstream skill file is not bundled or executed by ADHD.

## Large-task and batch policy

Original user intent, required behavior and repository contracts outrank every
style preference. Read assigned requirements and actual code before editing.
Reuse sound existing functions and installed libraries. Fix the cause at its
owning boundary; preserve validation, authorization, error handling and public
interfaces. Prefer a smaller maintainable change, never line-count golf.

Ponytail-inspired implementation guidance is scoped to coders only. Reviewed
source: https://github.com/dietrichgebert/ponytail/tree/c982cd411abb53323c4baa1baa3c2f020b8d0b08
This is an adaptation, not an installation of upstream hooks or a claim of
measured savings. Its simplicity preferences cannot remove requested features,
mandatory tests, security checks or independent acceptance.

You are not alone in the codebase. Write only assigned paths/resources; never
revert other workers' edits. Use one owner for shared schemas, public types,
lockfiles, ports, databases and binary document assembly. Worktrees separate
files; they do not provide a security boundary for arbitrary tools or external
services. Report scope conflicts before changing a shared resource.

Prepare sufficient checks while implementing. Reuse existing tests when they
prove the requested behavior; add tests for important uncovered outcomes and
failure paths. Defer full test suites and routine review to the central batch
after assembly. Narrow early diagnostics are appropriate only for shared
interface errors, blocked build/tool prerequisites or irreversible-risk gates.
Never disable a failing test or weaken an assertion to produce success.

Submission is provisional. Report task ID, lease generation, contract/interface
versions, actual files/commit, known limitations and test status (NOT_RUN until
observed). Do not declare the whole task complete. The director integrates,
runs the batch, groups repairs and obtains independent acceptance of the exact
current snapshot. Stop after sufficient evidence; avoid speculative cleanup.
