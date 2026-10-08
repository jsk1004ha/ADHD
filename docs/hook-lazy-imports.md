# Hook lazy imports — separate local change

Correctness baseline: `f0087b0d798fdb79b14d2caf33acbf0e4b15b883`.
Frozen lazy code/tests/benchmark commit: `5849168b733c06eaa62923380b540a2a9c131ab3`.
This report is committed separately after measurement; runtime source bytes match the frozen tested/measured code commit.

Only snapshots, coding-scope and provenance validators moved to their existing first-use branches. FileLock, event dispatch, inbox processing, state persistence, memory/search, emitted guidance and error policy are retained. Operational AST equality after removing imports/import-only guards and exact context() AST equality are checked. The >1200 mandatory-guidance policy remains unchanged.

Across both diagnostic settings, emitted-context length and complete stdout JSON byte distributions are identical between versions for every scenario. The measured change does not obtain speed by shortening output.

Baseline full Windows regression: discovered/started 544/544; executed/pass 535/535; skip 9; failed 0.
Lazy full Windows regression: discovered/started 550/550; executed/pass 541/541; skip 9; failed 0.
Six new fresh-process load-boundary/first-use/error tests passed; they failed on the eager baseline. Existing full regression covers original prompts, begin acknowledgements/inbox, goal changes, stop/resume, independent verification, persistence and validation errors.

The existing Ubuntu WSL/Python 3.12.3 ran the Windows-skipped internal symlink rejection test successfully on the correctness baseline (1 pass, 0 skip/failure/error). After measurement, seven other Windows-skipped cases passed on that exact baseline; one Obsidian case initially skipped because the existing wiki helper was not located inside WSL. The original skipped-attempt log and nonzero all-executed helper receipt remain preserved. Using a byte-identical copy of the already available Windows wiki_context.py as an isolated test fixture, that case then actually ran and passed: all nine distinct skipped cases are covered on the baseline. All nine skipped cases ran and passed on the frozen lazy source (9 pass, 0 skip/failure/error): eight symlink cases and one POSIX-filename case. Linux source archives and native.py hashes bind these runs to their respective commits. This is a targeted Linux check, not the full Linux suite. The test-only ADHD_TEST_WIKI_CONTEXT path does not modify user configuration, installation or hook trust. The fixture SHA-256 is recorded with the observed checks. These are local checks, not remote CI. Actual App installation/compaction/resumption remains unverified; no user installation/configuration/trust changes.

## Measurement conditions

Same Windows runtime and executable for both frozen Git versions. 30 retained repetitions per version/event/diagnostics setting; two excluded warmup rounds. Version order and on/off order reverse on odd iterations. Fresh interpreter and temporary CODEX_HOME/ADHD_HOME/workspace per sample; filesystem/source caches warm. No outlier trimming. Setup hooks/CLI/checks are excluded. Whole-process time runs from before subprocess.run until exit/stdout collection, including interpreter/imports/processing/diagnostic append. Internal native import and first-use inbox phases come from diagnostics-on samples. A first-use candidate is genuinely acknowledged, snapshotted and persisted; a provenance candidate validates a current observed analysis receipt and numeric manifest. These phases include validation work, not just a microbenchmark of import syntax.

The observer limits one check to 1800 seconds. The initial monolithic run was interrupted and contributes zero final samples. The unchanged committed driver then ran one scenario per observed check. All eleven original JSON files and receipts are retained; the combined JSON concatenates their samples/results/order without resampling or relabeling. The Windows desktop was shared, without CPU affinity or a fixed power mode; process-time variability is retained.

Quartiles: statistics.quantiles(n=4, method="inclusive"); IQR=Q3−Q1. Each table cell is median ms (IQR ms). Internal timers and diagnostics-on whole-process times are also retained in raw JSON.

## Whole-process time, diagnostics off

| Event/condition | Correctness baseline | Lazy imports | Median change | Quartile overlap |
| --- | --- | --- | --- | --- |
| idle_startup | 301.602 (64.399) | 302.583 (104.788) | +0.33% | yes |
| idle_prompt | 293.408 (105.075) | 282.304 (76.874) | -3.78% | yes |
| idle_cli_begin | 412.080 (87.657) | 403.982 (71.830) | -1.97% | yes |
| idle_post_tool | 288.122 (40.862) | 292.588 (36.802) | +1.55% | yes |
| active_prompt | 327.634 (146.470) | 298.263 (56.301) | -8.96% | yes |
| active_stop | 807.842 (942.251) | 899.427 (792.683) | +11.34% | yes |
| active_compact_start | 268.984 (29.051) | 264.263 (36.984) | -1.76% | yes |
| active_resume_start | 275.360 (28.148) | 259.526 (35.010) | -5.75% | yes |
| active_post_compact | 283.330 (72.396) | 270.743 (41.536) | -4.44% | yes |
| first_snapshot_candidate | 310.835 (45.867) | 326.823 (50.798) | +5.14% | yes |
| first_provenance_candidate | 346.892 (86.242) | 344.318 (65.790) | -0.74% | yes |

## Internal native import, diagnostics on

| Event/condition | Correctness baseline | Lazy imports |
| --- | --- | --- |
| idle_startup | 156.362 (52.760) | 150.369 (30.881) |
| idle_prompt | 137.733 (28.173) | 133.005 (37.687) |
| idle_cli_begin | 139.433 (16.113) | 140.906 (31.799) |
| idle_post_tool | 150.845 (39.035) | 133.340 (29.726) |
| active_prompt | 160.582 (77.194) | 138.830 (70.025) |
| active_stop | 428.895 (515.713) | 482.277 (363.527) |
| active_compact_start | 133.618 (17.860) | 125.675 (13.740) |
| active_resume_start | 131.034 (18.152) | 134.638 (23.446) |
| active_post_compact | 135.217 (27.704) | 129.436 (26.802) |
| first_snapshot_candidate | 150.127 (20.556) | 142.876 (23.038) |
| first_provenance_candidate | 141.487 (28.580) | 131.217 (30.299) |

## First use: inbox processing, diagnostics on

| Deferred feature | Correctness baseline | Lazy imports |
| --- | --- | --- |
| idle_cli_begin | 102.005 (14.653) | 106.314 (28.080) |
| first_snapshot_candidate | 15.186 (4.479) | 17.921 (2.568) |
| first_provenance_candidate | 50.939 (6.881) | 59.368 (9.700) |

## Interpretation and provenance

A lower internal native-import time alone is not a whole-process speed claim. First-use inbox work can increase because deferred import cost is paid there. Quartile overlap is a descriptive variability flag, not a significance test or confidence interval.
Whole-process median worse in: idle_startup, idle_post_tool, active_stop, first_snapshot_candidate.
Overlapping middle 50% ranges: idle_startup, idle_prompt, idle_cli_begin, idle_post_tool, active_prompt, active_stop, active_compact_start, active_resume_start, active_post_compact, first_snapshot_candidate, first_provenance_candidate.
Retention decision: retain the requested avoidance of unused module loading, verified by fresh-process tests, with unchanged operational behavior. This run does not establish a general whole-process latency improvement. All eleven process-time quartile ranges overlap; four medians are worse and first-use inbox work is higher. No additional optimization or repeat experiment was undertaken to hide that result.

Raw measurement JSON: `.adhd/hook-lazy-20261008/lazy-benchmark-results.json`; SHA-256 `f1efa6e5c59f02f7977172fb73a32220e6135e1d87af8d03c10671b507f585f4`.
Actual retained samples: 1320; version/event/mode conditions: 44.
Full logs/counts/source hashes: `.adhd/hook-lazy-20261008/correctness-full/` and `lazy-full/`.
The original B JSON and review ZIP were not modified or relabeled. No remote push/PR/CI or live installation. Historical native gate status is separate from local correctness and performance evidence.
