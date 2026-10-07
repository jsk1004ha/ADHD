---
name: adhd-goal
description: Keep ADHD working toward a user-defined outcome through implementation, checks and repair until independently verified. Invoke with $adhd-goal or /goal and the target result.
---
# Work until the requested outcome is verified

Read the actual user goal, inputs and current native bridge view. Use the existing
ADHD owner and [native protocol](../adhd-native/references/protocol.md); do not
launch another loop. Preserve the selected model, effort, permissions and scope.

The native hook recognizes a message beginning with `/goal` or `$adhd-goal`,
followed by whitespace and the outcome. The host's built-in `/goal` bypasses
UserPromptSubmit: ADHD binds its real `thread_goal_updated` event from the
host-supplied, session-matched transcript instead. For example:

```text
$adhd-goal Fix the bug, pass the regression tests and produce the working result.
/goal Finish the report with all required sections and verified source evidence.
```

1. Translate the user's outcome into observable acceptance criteria while
   preserving their exact request. Read the relevant files and constraints.
   If no outcome is supplied, use the current task's preserved goal; if none
   exists, ask for the missing outcome. A factual research conclusion must follow
   the evidence; never manufacture a finding to match the user's expectation.
2. For an idle task, capture coding scope where required and submit `native begin`
   with `loop_mode: "goal"`, criteria, artifacts and a requirement-covered plan.
   Read the acknowledgement and confirm `loop_mode` is `goal` in the view.
   An observed leading command or active host thread goal authorizes this mode.
3. For an active task, reconcile the observed pending command using `sync-intent`:
   a bare `/goal` retains the current contract with `no_change`; changed outcome
   text needs the actual amendment or new-task transition. For the same active
   task, submit `native goal` with `{"source_turn_id":"OBSERVED_GOAL_TURN_ID"}`
   after reconciliation and read the acknowledgement. A distinct new task uses
   a new begin after old children stop. Never edit canonical state or the view.
4. Implement, run realistic checks, record evidence and repair unmet criteria.
   Update checkpoints with actual progress and the next unresolved action.
   After repeated failures change the hypothesis or method. Continue authorized
   reversible work without asking whether to proceed.
5. Submit a current candidate only after all criteria pass, then obtain the native
   independent verifier's review of that exact digest. A rejection returns to
   repair and fresh verification. A candidate, self-declared success or exhausted
   budget is not completion. Finish only when the view reports fresh acceptance.

Goal mode omits implicit continuation, elapsed-time, stagnation, epoch and child
call cutoffs. Explicit `native-policy.json` limits still apply, as do parallel
child limits, the Astra consultation cap, the single-writer invariant and all
acceptance gates. Stop on user cancellation, pause, exhausted explicit limits or
an essential blocker with no viable recovery path; state what remains unfinished.
Do not invent progress, reset limits or weaken tests to keep the loop alive.

The host must deliver Stop/lifecycle hooks and either the user prompt or its
session-matched goal transcript. `$adhd-goal TARGET` remains the direct entry point.
Host goal pause, clear or termination retains unfinished native work; a host goal
marked complete cannot replace independent ADHD acceptance.
Closed Apps, missing hooks and provider limits cannot be bypassed by this skill.
