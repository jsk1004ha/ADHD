---
name: adhd-unblock
description: Diagnose stalled ADHD progress from actual failures and choose the smallest test that distinguishes causes. Use for 반복 실패 or 막힘 진단; preserve authority boundaries and return to the existing plan after recovery.
---
# Change the hypothesis when progress stalls

Read the original objective, current plan, actual failed checks, attempted approaches
and last known good conditions. For handoff read
[the shared contract](../adhd-native/references/skill-handoff.md).

Describe expected versus observed behavior and reproduce or locate the concrete
failure. Distinguish requirement conflicts, missing knowledge, environment/dependency
issues, authority denial, approach failure and bad verification. Inspect normal
cases and recent changes; do not guess a fix from the last error line alone.

Record the approaches already disproved. Select one current hypothesis, the result
that would refute it and the smallest feasible discriminating check. Run an
authorized check through the existing owner and change the next hypothesis based
on its real result. For time stalls, separate execution, waiting and repeated work
using observation rather than treating all elapsed time as CPU cost.

Return failed approaches, evidence, hypothesis, next check, alternative-approach
condition and recovery evidence. Use existing failure/check/checkpoint records;
prose-only checkpoint changes are not progress. Do not introduce another retry
loop or override the owner's stagnation, time or child budget.

Finish when the original plan can resume, or when a specific essential external
condition prevents meaningful progress. Report unavailable tools/inputs honestly.
Access denial must not be bypassed with a new account, endpoint or weaker policy.
An actual goal change belongs to steer; a working code performance issue belongs
to optimize. If only diagnosis was requested, deliver findings and the next check.

Example: “같은 파일 이름 규칙을 고쳐도 계속 실패해.” Compare a small normal and
failing input to distinguish an absent naming convention from a parser defect.
Do not simply retry another guessed pattern.

## Method inputs

Adapt root-cause/single-hypothesis tests from [Systematic Debugging](https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/skills/systematic-debugging/SKILL.md)
and problem/workload/time decomposition from [Gregg's methods](https://www.brendangregg.com/methodology.html).
