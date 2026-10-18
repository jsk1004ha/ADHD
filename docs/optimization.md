# Context, resource and storage improvements

Context and storage commands use local, opt-in sidecars. An active native contract also exports event-driven progress files in the new release. Native state remains schema 2. Existing model, effort, authentication, integrations, release retention (two rollback versions), and independent acceptance remain unchanged.

## Use and rollback

Run `python adhd.py context locate --workspace PATH --query QUERY --sources code,memory,wiki`. The default is six cards and 6,500 characters. `--complete` reconciles sources; it may be expensive. An incomplete response cannot establish absence. Keep its continuation and warnings. `context read --payload-file REF.json --revision REVISION` checks current source and scope; stale references require another locate. References contain revision/hash and parser/policy identities. Code graphs contain Python AST facts only.

`context read --session SESSION --actor parent --epoch 0` records delivery metadata. A repeat for the same actor, epoch, revision and excerpt can return a reference. Change the epoch after compaction; a new actor receives the actual excerpt again. `context trace` describes observed deliveries only; host system prompts, tool catalogues and unknown tokens are not measured. `context handoff` retains original requests, requirement IDs, preservation conditions, stage, changes, checks, failures and next action. Oversized required intent is referenced explicitly, never silently shortened.

For an active native contract, state export refreshes `.adhd/context-status.json` and `.adhd/context-status.md` when its visible goal, phase, checks, failures or next action changes. An unchanged projection neither invokes Git nor rewrites files. The bounded diff snapshot records that transition; use the Codex review panel for the current full diff. `view.json.status_panel` supplies the Markdown path and `unstaged` review target. Open these once with the supported Codex file/review tools; the same file receives subsequent updates. CLI hosts without these tools retain the local files. `context status --write` is still available for an explicit snapshot. These derived files do not grant authority or acceptance, and a panel error is exposed without changing native state. An already installed, pinned older hook gains this behavior only after a normal upgrade.

Automatic coding status uses the captured Git scope; a native coding contract without a Git scope emits an explicit incomplete diff reference instead of repeatedly spawning Git. An explicit `context status --write` reconciles the diff on request and retains the returned source hash. Automatic and explicit snapshots share the same bounded on-disk schema. GC accepts these files only when their fixed fields and source agree with the authoritative owner/run and their Markdown matches the JSON representation; both file hashes remain in the closure. Unknown fields, wrong owners and mismatched files still block GC. Markdown line endings are normalised for display while the original goal text remains intact in JSON.

Stop using the new commands to disable them. Existing memory/wiki/resume commands remain available. Keep sidecar data when resuming; indexes can be rebuilt but original evidence cannot be recreated by rebuilding an index. Do not delete original logs, old release rollback copies or user configuration as part of disabling a feature.

Native installation writes `CODEX_HOME/adhd/native-install-recovery.json` before publishing managed files. Success or complete exception rollback removes this marker. An incomplete rollback retains the release, backups, quarantined files and the marker's `remaining` entries, and blocks subsequent install, upgrade and rollback commands. Inspect those entries before resolving the recovery state; there is no automatic cleanup command for unknown or concurrently edited files. Rollback verifies the moved file with a separate 64 KiB streaming reader and publishes restored bytes without overwriting an occupied destination. Persistent total memory/OS failure and arbitrary noncooperating writes during final quarantine disposal cannot be guaranteed recoverable.

## Resource and evaluation boundaries

For the bounded startup path, explicitly set `.adhd/context-options.json` to `{"schema_version":1,"wiki_startup_hints":true}` after deploying the new source. Existing selected wiki sources are still checked for current hashes and policy. A missing index returns `wiki-reconciliation-required`; perform full reconciliation outside the hook. Hints stay incomplete. Removing this option restores the legacy full context path. Source changes do not update an already pinned installed hook runtime.

SQLite caches use 8 MiB; Python parser cache is bounded at 16 MiB, streaming buffers at 1 MiB, maintenance at one operation with eight pending slots. These are component budgets, not a total host/model RAM guarantee. No resident service is required. New central multiprocess WAL writing is not enabled. Local rollback-journal sidecars use file locks and short transactions. Keep network/synchronised database paths out of the store.

Index refresh, pinned-source reuse, immutable-body maintenance/export and evidence-log archive creation enforce `MaintenanceBudget` independently of model slots. Defaults per operation are 10 seconds of calling-thread CPU and 512 MiB each of processed read/write content. The Python APIs accept a narrower budget. Checks occur at bounded work boundaries, including hashing/compression chunks; a parser call or OS operation can overshoot before the next check. These are soft process-local budgets, not physical I/O counters, aggregate host quotas or child-process CPU quotas. Git subprocesses retain their existing wall timeouts. Exhaustion raises an explicit failure, rolls back index changes and removes only the operation's new temporary output; authoritative originals and mandatory evidence validation remain protected. Existing raw/gzip descriptors remain readable.

`experiment start/finish/summary/preregister/compare` retains every start, including failed, blocked, timeout, cancelled and unfinished work. Do not overwrite a failed trial; repeats do not increase independent task count. Missing usage remains unknown. Process-tree metrics are partial samples and identify processes by PID plus creation identity; CPU, I/O, private memory and working set are separate. Logical bytes, allocated bytes and attributed reclaimed disk space are separate; unmeasured reclamation is null.

New trial-enabled checks stop their sampler before a bounded post-task observation window (default 200 ms). `resources.post_task` records remaining tracked identities, CPU delta, unavailable/reused identities, scope and coverage. At most 32 remaining identities are listed with an explicit full observed count and truncation flag. The observer/root PID is identified because a check's controller may remain alive after its child exits. A vanished known tree can have zero remaining CPU; missing counters or a reused PID do not establish idle CPU. These observations do not describe the whole host and are never filled retroactively into older trial records.

`scripts/evaluate_context_components.py --out NEW_DIRECTORY` measures fixed code, Korean memory, long-log and delivery fixtures. This is not the 12 actual agent pilot tasks or 24 held-out tasks required by `plan.md`. The broad-read fixture baseline is stated explicitly. It does not justify production token or time claims. Preserve task inputs, source snapshots, model/effort, environment, tool and permission settings, cache condition, alternating order, failures and human interventions for real trials. A metadata metric gate cannot attest native acceptance or change defaults. No independent receipt means no adoption.

## Output collection and diagnostics

Native checks retain their full existing logs. Optional host textual-result collection requires `.adhd/tool-output-policy.json`: schema_version 1, enabled true, a policy_revision and exact allowed_tools list. It is disabled when absent. Binary, oversized and recognised sensitive results remain uncollected with a reason. A regex is not a comprehensive personal-data classifier; enable capture only for a reviewed non-sensitive tool/result class. Results retain policy/hash provenance and reported truncation/exit status; missing status is unknown. Never put authentication, personal data or full prompts in trial metadata.

`diagnose capabilities` distinguishes configuration, exposure and observed successful calls from unknown/authentication/policy failures; it grants no authority. `diagnose recovery` extends an existing failure receipt with verified facts, hypotheses, a changed condition, a minimal check and conditional next actions. `diagnose manifest` supplies static hints and explicit unknowns; it does not prove all test inputs. `diagnose calibration` reports both false acceptance and false rejection and rejects stale or repeated reviews. Existing failure identities, progress rules, child drain, generation checks and final independent acceptance remain authoritative.

## Snapshot analysis

`snapshot capabilities` reports whether concurrent analysis is actually supported. This host has no verified adapter blocking writes to the source, configuration and external tools, so concurrent execution stays disabled. Serial analysis of an isolated snapshot is available. A caller's read_only boolean cannot enable concurrency.

`snapshot prepare` defaults to an immutable commit. Dirty mode separates staged bytes and working bytes and includes only explicitly named untracked files. Credentials, active state, Git internals, generated inputs and detected secrets are excluded and listed as limits. Scratch must be outside both the source and active state; links and junctions cannot escape. Pre-copy, copied and post-copy manifests are checked; after two retries, a moving source falls back to the fixed commit. Failed owned copies are removed. Per-file agreement does not prove a simultaneous multi-file dirty view: require a fixed commit for that guarantee. Reads bind snapshot/hash/line ranges. Writer transition obtains the existing lease and checks HEAD, worktree and index again; changed/unknown state and running children never silently release a native lease.

## Storage and preservation

Immutable object storage is restricted to verified completed owners and versioned scoped references; a content hash is not permission. Current access/policy/privacy and tombstones are checked on each read/export. Known secrets and authentication files are excluded. Mutable source and active release files are never hardlinked. Originals remain until old/new readers, standalone export and exact-byte restore have been verified. A forgotten reference cannot hide or delete another valid reference's body. Metadata overhead counts against storage benefit.

First explicitly configure the workspace-owned `.adhd/immutable-policy.json`, for example:

```json
{"schema_version":1,"policy_revision":"policy-1","allowed_privacy":["private"],"allowed_actions":["capture","read","export","restore","forget"]}
```

This local opt-in is required and is read from disk for every operation. Missing, linked, oversized or unknown policy schemas deny access. Request `policy_revision` is an expected revision and `allowed_privacy` only narrows the current file; neither grants permission. A changed policy can revoke existing references. To stop new capture while retaining reads and standalone restoration, remove `capture` from `allowed_actions`, retaining the other required actions and current revision. This is a workspace policy boundary, not an OS permission system against someone who can already edit that workspace's policy and files.

Collection is explicit: `python adhd.py objects capture --workspace PATH --payload-file REQUEST.json`. A capture request supplies `owner_key`, `policy_revision`, and optional `privacy`, `retention` and `source`. The default source is the actual completed native run's completion receipt. The native owner must authoritatively match the current workspace and completed run; a caller's status declaration is insufficient. Explicit sources are restricted to that exact completed run's evidence. No hook automatically moves or removes existing archives. References retain the verified completion identity when the same native session later begins another run; current project/policy/privacy checks still apply.

Capture rechecks the completed owner and current policy before publishing a reference. If DB registration fails after a new blob is published, the same writer guard removes only that call's blob after rollback proves it has no reference or ownership record. Ambiguous storage failures remain protected; no legacy orphan is silently adopted or deleted.

Filesystem errors from audit and GC expose stable error codes without internal object paths or digests. A blocked operation or nonzero CLI exit remains a failure; redaction does not turn it into a successful collection.

If rollback cleanup itself fails, a retry rejects that unclassified object rather than publishing a new reference to it. Historical runs in the same session remain readable and can be captured separately (at most 64 registered roots per workspace/key); GC stays blocked if a previously registered authoritative state has changed or cannot be classified. This conservative limit can retain extra bytes.

For `objects read`, the payload contains `ref_id`, `policy_revision`, `allowed_privacy` and optional byte `offset`/`limit`. The CLI returns base64 with byte counts and a continuation offset, preserving UTF-8 fragments and exact bytes. `objects export` adds a new `destination` ZIP path; `objects forget` tombstones that reference. `objects restore` accepts `export_path`, a new `destination` with the original filename, and current access fields. A standalone export contains the full body and can be restored without opening the store. Export files are separate preservation copies; forgetting a reference does not revoke an already authorised standalone export. Neither export nor restore overwrites an existing destination.

`objects gc --root STORE` persists a dry-run operation. Inspect its protection reasons and keep the operation ID; only `objects apply --root STORE --operation-id ID` requests removal. `objects recover --root STORE` resolves an interrupted operation before another plan/apply. Fresh stores register native/workspace roots before publishing immutable references and require explicit manifests for exports/backups. Unknown legacy ownership, unsupported files or unregistered roots block collection. Dry-run candidates expose opaque IDs and aggregate bytes rather than cross-project content hashes. The public API cannot read a body by hash alone. Raw source logs remain untouched and reclaimed disk bytes remain unknown.

GC starts with dry-run. Unknown owners, inaccessible or unregistered reference roots, active/resumable owners, children, leases, candidates, pending batches/writes, worktrees, exports and backups protect bodies. Removal requires a stable authoritative reference closure under the generation lock, quarantine and verified restoration; a refcount alone is insufficient. No user-data GC is run by the regression suite. FTS migration must first prove shadow search/integrity/space equivalence and current-generation stability. Rollback reconstructs from current records, including writes after cutover. If a gate is unavailable or inconclusive, retain the existing search path and originals.

The FTS maintenance API is `adhd.fts_migration.proposal(db_path, golden_queries)`, then an explicit `cutover(..., allow_cutover=True)` only for a quiescent local DELETE-journal database whose proposal passes. Savings are required by default. Preserve the non-overwritten backup; `rollback(db_path)` rebuilds the legacy search structure from current records, retaining writes and forgets made after cutover. This is an opt-in maintenance operation, not a new default memory schema. Rebuild or migrate a disposable copy first.

`adhd.source_cache.SourceCache(cache_root).snapshot(repository, remote, full_commit, paths)` reuses only local pinned Git objects and records remote, commit and exact needed paths. It neither fetches lazily nor follows credential-bearing URLs. Partial clone controls transferred Git objects; sparse checkout controls checked-out paths. They are recorded separately and neither is assumed to imply the other. Cached source is reconstructible; immutable evidence and independent exports are preservation data.

## Research-to-implementation map

| Plan | Implementation | Evidence boundary |
| --- | --- | --- |
| P0 | trial_registry, resource_metrics, optional check observations | Actual all-start observations; no inferred usage or acceptance |
| P1 | context_search, context_index, bounded Memory recall | Golden answers, eligibility/privacy, stale references and fallback regressions |
| P2 | incremental code index, scoped wiki hints, bounded caches | Hints are incomplete; complete wiki reconciliation can still be expensive |
| P3 | context_session, explicit host-result capture | Epoch/actor delivery, handoff intent and full source references |
| P4 | capability inventory, recovery diagnosis, manifest hints, review calibration | Advisory information; existing independent acceptance retained |
| P5 | completed_archive, immutable_store, immutable_gc, source_cache, fts_migration, package allowlist guard | Native-completed capture, exact-byte preservation and strict migration/GC gates |
| P6 | snapshot_reader and existing leases | Fixed/dirty inputs, stale transitions; unsupported concurrency disabled |

The research and actual-user observations are planning evidence, not causal performance proof. Local implementation, default adoption, deployment and destructive data cleanup are separate outcomes. See the dated implementation report for the checks actually executed and remaining gaps.
## 작업 시간 제한과 빠른 실패 확인

새 버전 설치 후 새 managed session에서 native begin에
`--time-limit-seconds 900 --review-reserve-seconds 180`을 주면 총 15분 중
마지막 3분을 검증·리뷰에 예약한다. 명시적 시간 제한은 goal에서도 적용하며
resume으로 만료 시각을 초기화하지 않는다. 숫자를 지정하지 않으면 기존 동작을 유지한다.

검사 spec의 `time_limit_seconds`는 전체 검사 묶음에 공유되고, 남은 시간보다
긴 자체 timeout을 가진 자식 프로세스도 남은 예산으로 제한한다. 만료 후 새 검사를
시작하지 않으며 미실행·timeout을 성공으로 바꾸지 않는다. 호스트/model 호출 중간의
강제 중단과 물리 OS quota는 지원하지 않으며 종료 정리·해시 처리의 추가 시간은 남을 수 있다.

선택 옵션 `scheduling: "shortest_first"`, check별 `estimated_seconds`,
`fail_fast: true`로 작은 판별 검사를 먼저 돌려 실패 원인을 일찍 확인할 수 있다.
실패 회차에서 미실행한 필수 검사는 수정 후 반드시 통과 증거를 확보해야 한다.
기존 영향 범위·환경·입력 hash에 근거한 재사용과 의존성·자원 충돌 규칙은 유지한다.
자세한 사용법과 경계는 [검증 시간 가이드](verification-efficiency.md)에 있다.
