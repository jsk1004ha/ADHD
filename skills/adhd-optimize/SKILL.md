---
name: adhd-optimize
description: Optimize code performance, resource use or structural complexity while preserving specified behavior. Use for 코드 최적화, 메모리 절감 or 동작 보존 리팩터링; diagnose defects first and route requirement changes to steer.
---
# Optimize code with evidence

Read the target code, callers, dependencies and existing tests before changing it.
Use [the shared handoff](../adhd-native/references/skill-handoff.md) to connect scope,
checks and the patch to the current owner. Reuse a relevant installed performance
skill when the actual stack needs specialist guidance.

## Select the requested optimization

- **Performance:** latency or throughput of an actual workload.
- **Resources:** CPU, peak memory, I/O or allocation cost with real constraints.
- **Structure:** specific duplication, nesting, coupled responsibilities or
  unnecessary abstraction; clarity is more important than minimum line count.

Start in the user-specified module/flow. For a finishing pass on recent work,
bound it to that work; do not infer a repository-wide rewrite. If the problem is
incorrect behavior or failed execution, diagnose it before optimization.

## Baseline, one change, regression

1. State the target metric or structural issue, allowed files and invariant APIs,
   outputs, order, error behavior, side effects, precision and authorization.
2. Measure a representative performance/resource baseline and profile its likely
   cause. For structural cleanup, write a short cleanup plan identifying the
   concrete smell. If meaningful behavior coverage is missing, lock it with
   regression tests before editing. Never weaken existing tests to gain speed.
3. Select one high-impact bottleneck or structural issue and a falsifiable change
   hypothesis. Prefer deletion/reuse/existing patterns to new layers. Do not add
   dependencies without a request. Record cache, concurrency or approximation
   tradeoffs against correctness, memory, ordering and failure recovery.
4. Apply the smallest useful patch inside the assigned ownership boundary.
5. Compare the same workload/environment before and after; report warm/cold
   conditions, input size, concurrency and observed variation when relevant.
   Verify representative, boundary and failure cases; run the repository's
   applicable tests, type/lint/build and static checks. Exercise real UI flows
   when optimizing user-visible UI behavior.
6. Adopt only when the evidence supports the requested improvement and invariant
   behavior. Revert just this patch if regression or unacceptable resource cost
   outweighs improvement, preserving unrelated edits. A verified lack of useful
   gain is an honest outcome. Record a scoped recovery method.

Return changed files, preserved behavior, baseline or cleanup evidence, chosen
issue/hypothesis, patch, measured comparison where available, regression results,
tradeoffs and adopt/revert decision. An analysis-only request gets a reviewable
proposal rather than edits. Missing workload/profiler means performance remains
unverified; do not claim speedup from shorter code or fewer agent calls.

In ADHD, attach the scope/invariants to the plan, actual commands to check receipts
and patch/results to the candidate. Keep required independent review. Feature
deletion, API change or weaker accuracy is a requirement-change proposal for steer,
not successful optimization. Separate an encountered defect fix from the optimization.

Example: for “기록이 많을 때 목록이 느려. 기능은 유지하고 최적화해 줘”, measure
and profile first. A parsing cache must handle changed/deleted files and memory
limits as well as source links and date uncertainty.

## Method inputs

Use [Gregg's diagnostic and timing methods](https://www.brendangregg.com/methodology.html),
[Code Simplifier's behavior/scope/clarity principles](https://github.com/anthropics/claude-plugins-official/blob/d182ca456ca09d31d139f7d3818d1d333b103cce/plugins/code-simplifier/agents/code-simplifier.md)
and [single-hypothesis investigation](https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/skills/systematic-debugging/SKILL.md).
For React/Next only, consult [Vercel's high-impact priorities](https://github.com/vercel-labs/agent-skills/blob/063bee94c3f4df8453406c830b0a7df0f2860278/skills/react-best-practices/SKILL.md);
do not apply that stack's rules to unrelated code or claim those guidelines measured a gain.
