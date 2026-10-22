# Workspace storage

`adhd.workspace_storage` limits new working copies without treating user files or
existing verification artifacts as disposable. It uses the ADHD central store's
`workspace-storage.sqlite3` and a file lock to count outstanding reservations by
volume. `reserve(owner, bytes, minimum_free=2 GiB)` checks free space minus all
cooperating reservations, returns an opaque token, and `release(token)` returns
the budget. It does not claim an operating-system disk quota: unrelated writers
can still consume space, so producers must enforce their limit while streaming.

`WorkspaceStorage(workspace).scratch(owner, reserved_bytes=..., minimum_free=...)`
creates `workspace/.adhd/scratch/<uuid>` with an ownership record and yields its
path. Callers must stop their child processes and publish required evidence
before leaving the context. Normal exit and exceptions remove that exact owned
tree. References added with `add_reference(path, name)` retain it; remove them
with `remove_reference` before `sweep()` can reclaim it. Register subprocesses
with `register_child(path, pid)` so sweep protects them while their original
process identity is live or unknown. A crash recovery sweep uses the central
ownership rows and process creation identity. It never selects a directory by
age alone; unknown directories and links are left for inspection. Failed
cleanup is recorded as `cleanup_pending` for another safe sweep.

## Diagnostic commands without a separate payload file

```powershell
python adhd.py workspace-storage run --workspace PROJECT --max-bytes 33554432 --timeout 120 --owner diagnostic --argv TOOL --out "{scratch}/generated"
```

Put `--argv` last: all following arguments belong to the producer. This form runs
with its working directory in registered scratch, so ordinary relative outputs
are temporary too. `{scratch}` and `{results}` expand only as entire path arguments
or path prefixes, for example `"{results}/verification.json"`. Escaping `..`
suffixes are rejected; script bodies and shell expressions are never interpolated.
Use absolute paths for a producer script stored in the project. No shell is
started implicitly.

Every runner invocation sets `ADHD_SCRATCH`, `ADHD_RESULTS`, `TEMP`, `TMP` and
`TMPDIR` for its producer only. A cooperating tool must use those paths; this is
not an OS sandbox for a tool that hardcodes an unrelated location. Publish required
reports to `{results}` before exit. Full generated schemas, extracted media and
temporary profiles belong in `{scratch}`. Context exit cleans that exact registered
tree after owned descendants stop, including producer failure, launch errors and
timeouts. The retained `report.json` includes `cleanup.status`: `cleaned`,
`cleanup_pending`, or `not_created`. A pending cleanup is never reported complete.

Existing `--payload-file` calls remain compatible and default to the project cwd.
They may request `"cwd": "scratch"` and use the same path arguments. Direct
`--argv` and `--payload-file` are mutually exclusive. `--max-bytes`, `--timeout`
and `--owner` are direct-form options; payload calls declare them in JSON.

## Native completion and crash leftovers

`Stop`, `SessionEnd`, `Interrupt`, and the final `SubagentStop` queue metadata for
up to eight registered entries once native work is terminal and every child has
confirmed termination. Hooks perform no recursive filesystem walk/deletion or
reservation sweep. The next owned runner automatically rechecks and recovers
eligible leftovers before starting its producer. A forcibly killed invocation
therefore leaves visible queued work until the next owned command, rather than
blocking a host termination hook on a large directory. Normal, failed and timed-out
producers continue to clean immediately when their context exits.

No catalog means no storage import/database creation. Hook lock/SQLite waits use
0.1 seconds; the metadata SELECT has a 0.05-second SQLite progress deadline and a
limit of nine rows (eight plus a `more` indicator). No full catalog count or sort
is needed. These are cooperative limits, not an OS hard deadline for a filesystem
or lock operation. Active work/review and unconfirmed child termination skip even
this queue inspection.

The native exported view records `scratch_cleanup`, including queued entry IDs,
reference indicators, `more`, or a typed error on failure. Original inputs, final results, references, live
or unknown producer identities, reparse points, and unregistered folders remain
protected by the existing ownership checks. Only a fresh invocation may create a
registered scratch tree: this does not provide an adoption or arbitrary-delete API.

A shell tool rejected with `blocked by policy` before its process starts is a
host approval decision, not a Windows ACL failure or an ADHD permission setting.
The host may not disclose its detailed classifier rule. ADHD does not disable
that policy or retry the same denied target through another command. Instead,
new work declares creation/use/cleanup together in the owned runner, avoiding a
separate broad recursive-delete request after diagnostics. Previously denied
legacy files stay outside automatic recovery. OS cleanup errors inside an
already registered lifecycle remain visible as `cleanup_pending`.

`bounded_copy(sources: dict[str, Path], destination: Path, *, workspace: Path,
max_bytes=512 MiB, minimum_free=2 GiB) -> {"bytes": int, "files": int}` expects
an already resolved mapping from workspace-relative POSIX destination names to
the final source paths. The caller determines overlap and last-source precedence
before calling it. Both source and destination must be within the workspace;
links, junctions, and reparse points are refused. It preflights total bytes,
reserves the budget, streams with a growth limit, preserves source metadata, and
publishes without replacing a changed destination. The reported byte count is
newly copied logical bytes, not reclaimed or allocated disk space.

`extract_zip_selected(archive, members, namespace=..., tool_version=...,
options=..., max_bytes=512 MiB, max_cache_bytes=2 GiB)` extracts only named files
under `workspace/.adhd/cache/zip`. The key includes the source content hash,
selected names, namespace, tool version, and options. Reuse verifies cached
content; path escapes, ZIP symlinks, missing entries, and expansion above the
reservation fail. The payload cache is capped across registered entries on the
volume; controller/manifests have additional small metadata costs. Prefer
`with manager.selected_zip(...) as path:` to pin an entry for a consumer and
release it after that consumer stops. Unreferenced verified entries from the same
workspace are evicted oldest-first when capacity is needed. The plain
`extract_zip_selected()` path stays pinned until `release_cache(path)`; CLI callers
use `workspace-storage release-cache` once per successful extraction call after use.
Repeated plain acquisitions are reference-counted. Referenced, modified, unknown or
other-project entries are not evicted to make room. Cache paths are read-only inputs.

`python adhd.py workspace-storage run --workspace PROJECT --payload-file request.json`
accepts an explicit `argv` array, `reserved_bytes`, optional `minimum_free`,
`timeout`, and `owner`. The producer must use `ADHD_SCRATCH` for copies/extracts
and `ADHD_RESULTS` for final evidence. Wrapping a command does not relocate its
hardcoded output paths. The runner uses an owned Windows Job Object or Linux
process group, sweeps registered recoverable scratch before starting, caps each
stdout/stderr capture at 10 MiB, observes scratch growth, and retains a small
result report. The additional 20 MiB log allowance is included in the volume
reservation. Only the latest captures per owner are retained by default; old
unchanged owned log files are retired while reports and final artifacts stay.
Set `retain_logs: true` to pin captures needed as long-term evidence. Modified or
unknown files are not trimmed. External writes are observed, not an OS hard quota. Cooperating
streaming copy/extraction APIs enforce exact payload byte limits themselves.

`adhd.documents.inspect_document` streams binary media hashes while retaining
XML text/object/part evidence; it does not unpack images to disk. Full package
edits retain their existing preservation checks. The resource benchmark under
`scripts/benchmark_workspace_resources.py` compares the original inspection code
against representative fixtures, not live model token use or the entire workload.

Native upgrade retention runs only after the new installation record commits.
The active release and one verified active original backup remain. A historical
backup can be removed only when its exact recorded files still match and each
original is present with the same hash in the active backup. Unique, unknown,
modified, or referenced data stays. A cleanup failure is reported as
`cleanup_pending` and does not roll back the committed installation. `adhd.storage`
defaults to one current release; `prune_native_backups()` provides an explicit
preview/apply path for recorded backups. Neither operation scans or removes
unrelated legacy project data.

Successfully integrated large-task workers are compacted after their release
commits. Git retains a pinned commit for replay; non-Git runs retain only changed
files and deletion metadata. The final staging tree, reports, failed/modified/live
workers and recovery evidence remain. Cleanup denial is recorded instead of
retrying deletion through another tool. This bounds successful checkout copies
without discarding the patch needed to rebuild an assembly.
