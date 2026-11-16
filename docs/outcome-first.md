# Outcome-first execution

User intent and result quality govern the workflow. Auto is the default for new
runs; already active runs retain their selected profile.

Auto continues until the requested outcome is delivered. Internal default round,
time and stagnation counters guide recovery rather than end the task. Explicit
limits and user stops remain binding. An unavailable method leaves room for other
authorized work; actual permission restrictions are unchanged. Known unfinished
work in `remaining_work` survives checkpoints and compaction and prevents completion
until cleared. Passing a partial check is progress toward the requested outcome.

Available MCP capabilities are preferred for retrieval and deterministic processing
when they fit the task. Discover only relevant tools and request compact outputs
or artifact references; avoid copying intermediate data into model context. Existing
CLI/scripts remain the fallback when MCP is absent or less efficient. This is a
tool preference, not a new installation or approval step, and does not imply zero
token cost for tool calls. Model reasoning stays focused on decisions and quality.

- Small bounded tasks: execute directly and check relevant behavior.
- Substantial tasks: record requirements, do the work, check the affected result,
  and submit a candidate. A formal plan and separate reviewer are optional.
- Auto coding/game completion requires current artifact-related execution or a
  declared browser/MCP observation. Failure, stale inputs and protected-file
  changes cannot pass. Documents require current all-page inspection records;
  parent inspection is labeled parent_checked, never independent approval.
- Request `independent_review: true` in a candidate when a second review is useful.
  The simple, standard and deep compatibility profiles always require it.
- Existing scoped approval persists. Repeating an approval preserves the original
  authorization and any actual host denial. Running/unknown responses are not
  recorded as success. Host restrictions remain separate from user authorization.
- Apply feedback to the active task. For criteria-only amendments, auto can reuse
  a previous candidate's evidence for unchanged requirements if its receipt,
  executable, logs and input bytes remain valid. Changed requirements need new
  evidence; coding/game amendments still need a current affected-result check.
  Artifact/document/metadata changes discard this cross-revision reuse.
- Repeated failures prompt a changed next action, not a mandatory plan rewrite.
  Do not skip meaningful tests or hide a failure to finish faster.
- Completion reports success and automatically stores a reusable project skill.
  Missing manual procedure steps fall back to actual artifacts, criteria and checks.
  The skill is discoverable only in its project and compatible environment.
  First observed failures are retained under `.adhd/failures/`, with later recovery
  or task completion attached. Future runs recall a bounded failure reminder.
  Learning-storage errors are reported separately and do not undo task success.

Shorter skill text and no mandatory review child reduce avoidable context/model
work. Actual token, latency and cost savings depend on the task and are not
inferred from instruction size. No model, effort, credential, plugin or host
permission change is part of the default profile.
