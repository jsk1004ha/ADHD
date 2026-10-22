# Automatic project coordination

ADHD coordinates cooperating runs through one local SQLite registry. It shares
project identity across Git worktrees and uses the actual checkout identity for
ordinary command serialization. No additional model service or dependency is used.

Large-task dispatch automatically registers requirement-bound read/write scopes
and shared resources, acquires them atomically, and queues conflicts. Separate
assigned worker workspaces can run independent tasks concurrently. Submission
checks a private owner capability and generation. A project integration lock
serializes staging; a durable local pending marker is cleared only after the
coordination release commits. Repeating a release after a crash is idempotent.
Cancel/failed-launch handling preserves ownership until the producer has stopped.

For explicit commands, use `python adhd.py coordinate run --workspace PROJECT
--payload-file request.json`. The request contains:

```json
{
  "card": {
    "task_id": "check-api",
    "contract_revision": 1,
    "requirements": ["R1"],
    "read_paths": ["src/api.py"],
    "write_paths": ["tests/test_api.py"],
    "resources": ["build:shared-target"],
    "base_revision": "verified-input-revision"
  },
  "argv": ["python", "-m", "unittest", "tests.test_api"],
  "wait_seconds": 10,
  "timeout": 900
}
```

The command gate always reserves the checkout writer resource, including when a
card omits write paths. Its owned Windows Job Object or Linux process group must
finish before release. A still-active native/legacy workspace owner is respected;
such commands return `waiting`. This does not replace or remove that owner's lease.
Isolated large workers retain their existing native-parent contract checks.
Scope declarations are semantic guards, not an OS filesystem sandbox. Uncooperative
Linux programs that leave their process group are outside the lifecycle guarantee.

`coordinate register`, `acquire`, `finish`, `message`, and `status` expose the
same protocol for an agent. Only a successful new acquire returns the private
`owner` capability; retain it privately with `task_id` and `generation` for finish
or recovery. Status and duplicate acquire do not return that capability.
Card changes need a new revision and cannot amend a running owner. Read-only
consumers may name an explicit verified `snapshot_revision`; live readers wait
for conflicting writers. A queued large worker inherits matching predecessor
outputs from the same input revision, including recorded deletions. Cross-run
inheritance waits for the producer's current central validation; same-run staged
dependencies remain provisional until their complete assembly is validated. Dependencies
and predecessor proofs are rechecked against producer generation, revision and
current file hashes before transfer into the assigned staging tree. A changed
staging file is never silently overwritten. Reconcile retries pending releases;
freeze rejects an unfinished release.
An invalidated or failed newer producer blocks stale output at acquisition;
consumers do not enter a running state with an unusable old proof. Repair or a
validated replacement publication is needed before that dependent scope resumes.

Rules decide independent execution, reader sharing and serial ordering without
model calls or repeated user approval. Optional `message` payloads contain a
`conflict_id` and a bounded `message` such as
`{"action":"sequence","first":"task-a"}`. At most two messages are accepted
per unchanged conflict revision; stale replies are rejected. Proposals cannot
steal a running writer, expand scope, remove requirements or bypass host denial.
Repartitioning work requires validated replacement cards at a safe inactive
boundary. Unsupported or unconfirmed proposals retain deterministic sequencing.
No hook launches a separate model to negotiate. The host agent can provide the
two short proposals when semantic judgment is needed; provider tokens remain
unknown when the host does not report them.

Explicit command execution reuses the scratch runner and its bounded captures;
stdout and stderr are each limited to 10 MiB. Result reports stay under `.adhd/results`.

Cards are limited to 2 KiB and messages to 1 KiB of UTF-8 JSON. Active/dependency
lookup is bounded; status returns active tasks plus the last 20 terminal rows,
without full conversations or file bodies. A missing reply does not stop unrelated
ready work. Fixed ports, build targets, schemas and DBs can be named resources;
an adapter that uses port zero must report its actual bound address itself.

Use the workspace-storage byte reservations for large copies/extractions. A
coordination decision is not permission to delete an input, rewrite credentials,
publish, or restart another user's chat. User/host permissions still apply.
