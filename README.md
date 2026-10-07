# ADHD — Autonomous Delegation Harness Director

Large tasks can use the [durable DAG and isolated CLI worker workflow](docs/large-tasks.md).
The `large` command connects scheduling, workspaces, assembly and current batch evidence;
`batch` also supports post-assembly checks for ordinary tasks. Existing App single-writer
admission and user configuration are preserved.

ADHD v0.1.6 is a local harness for Codex App and CLI. It adds planning, execution evidence, and review gates through Codex hooks while preserving your existing authentication, model selection, router, plugins, skills, and personal configuration.


[0.1.6 release notes](docs/releases/0.1.6.md) describe persistent goal execution, the host goal bridge and opt-in Obsidian context and experience records. Source inventory and managed storage changes remain documented in [0.1.5](docs/releases/0.1.5.md), and large-task and lifecycle changes in [0.1.4](docs/releases/0.1.4.md).
This repository contains source code and generic defaults. It contains no account credentials, user configuration, run history, or personal wiki files. The Python package, skills, roles, hooks and install paths use the ADHD name; there is no legacy command alias. Previous native session state is reused in place only when its schema identifies one unambiguous state directory.

[한국어 안내](README.ko.md) · [Getting started](시작하기.md) · [Source and licensing notes](ADHD_PROVENANCE.md)

## Requirements

- Python 3.11 or later and an existing Codex App or CLI installation.
- Available models for the optional pinned helper roles: Sol and Luna at `max`, Astra at `low`. Check availability in your own account and router; configuration alone does not prove it.
- Optional document features use separately installed tools listed in `requirements-documents.txt`. No paid service signup or download is performed by the installer.

## Install or upgrade

Review the source and hook commands before installing. From a PowerShell terminal in this checkout:

```powershell
py -3 .\adhd.py doctor
py -3 .\adhd.py upgrade
```

On macOS/Linux, use `python3 adhd.py upgrade`. `install.ps1` and `install.sh` run the same upgrade command. The installer copies an immutable release into `$CODEX_HOME/adhd/releases`, adds nine hook groups to `hooks.json`, installs `adhd-*` skills and roles, and includes the default bundle below. Upgrade validates prior release hashes and backups before migration.

Codex may ask you to trust the new hook commands. Inspect them in `/hooks` and approve them there; ADHD does not create or bypass trust. Start a new App/CLI session after an upgrade so that the loaded hook and role profiles match the installed release.

```powershell
py -3 .\adhd.py doctor
py -3 .\adhd.py rollback-native
```

`rollback-native` checks for later edits before restoring managed files. Use `--codex-home PATH` to target an isolated Codex home for evaluation.

From this checkout or an installed release directory, preview old managed releases with `python -m adhd.storage scan`. `python -m adhd.storage prune --apply` explicitly removes only validated, unreferenced older releases. It keeps the current release, a recent rollback release and all known session/configuration references; backups and task history remain preserved. `--keep-releases N` retains at least two releases, plus referenced versions. Upgrade itself does not delete old releases.

## Helpers for ADHD work

Use these skills where they resolve a specific need in the current ADHD run.
They do not impose a mandatory sequence or create another execution owner.

| Invocation | Result |
| --- | --- |
| [`$adhd-goal`](skills/adhd-goal/SKILL.md) | Keep implementing, checking and repairing until the requested outcome is independently verified |
| [`$adhd-shape`](skills/adhd-shape/SKILL.md) | Turn a vague idea into a problem, scope and completion criteria |
| [`$adhd-challenge`](skills/adhd-challenge/SKILL.md) | Check consequential assumptions and risks against evidence |
| [`$adhd-decide`](skills/adhd-decide/SKILL.md) | Compare real options and record a choice with revisit conditions |
| [`$adhd-steer`](skills/adhd-steer/SKILL.md) | Apply new user feedback to retained requirements and active work |
| [`$adhd-unblock`](skills/adhd-unblock/SKILL.md) | Diagnose a concrete blocker and verify a bounded recovery |
| [`$adhd-optimize`](skills/adhd-optimize/SKILL.md) | **Optimize code**, preserving behavior and checking performance, resources or structure |
| [`$adhd-retro`](skills/adhd-retro/SKILL.md) | Connect actual results and feedback to corrections and grounded lessons |

For example: `$adhd-shape Make this research-record tool idea concrete`.
Clear small requests take a direct path. Analysis-only requests stay read-only;
already authorized implementation continues with the current owner.
The [shared handoff reference](skills/adhd-native/references/skill-handoff.md)
distinguishes descriptive notes from actual native bridge commands. Long-term
memory writes require an explicit user request.

Normal `upgrade` installs these skills, UI metadata and local references. Existing
same-name user folders are preserved whole, with the immutable release providing
a fallback for ADHD local discovery. Existing wiki-router selection and host
skill catalogs remain authoritative. Load a fresh Codex session after upgrading.
`builtin apply` handles only the public bundle and MCP entries below.

Start a goal loop with `/goal TARGET OUTCOME` or `$adhd-goal TARGET OUTCOME`.
Goal mode retains the original request and independent acceptance gates, and
continues after unmet criteria or a rejected review. It omits implicit round,
elapsed-time, no-progress, epoch and child-call cutoffs; explicit limits from
`native-policy.json` at begin, concurrency limits and the Astra cap still apply.
User stop/cancel, pause, essential blockers and session end stop execution.
Repeated failures require a changed approach, not fabricated progress. ADHD
connects the host's built-in `/goal` through its session-matched goal event;
`$adhd-goal` is also available as a direct entry point. Execution requires an
open host with working lifecycle hooks.

## Obsidian context and experience

Connect an existing Markdown vault and its `wiki_context.py` engine through the
[Obsidian guide](docs/obsidian.md). Configuration is local to each workspace and
disabled by default. Enable only the read/privacy/write roots needed for the task.
`wiki context`, `project` and `read` return bounded, revision-checked source data;
the current user request and contract remain authoritative.

Selected decisions, explicit positive/negative feedback and reproducible successes
become review candidates. Templates and Bases organize review and procedures;
successful code and documents retain hash-bound references. Procedure adoption
requires controller evidence. Storage inventory and archive/restore protect active
work. Total model tokens, retrieval speedups and user satisfaction must be measured
separately; this release does not claim improvements from diagnostic fixtures alone.

## Default skills and MCPs

A normal `upgrade` installs **50 complete, licensed public skill trees** (30 from OpenAI and 20 from [K-Dense Scientific Agent Skills](https://github.com/K-Dense-AI/scientific-agent-skills)), including each `SKILL.md` and its required local references, scripts, and assets. The [skill manifest](config/builtin-skills.json) records the source, pinned commit, and file hashes; [third-party notices](THIRD_PARTY_NOTICES.md) describe the licenses. An existing skill folder with the same name is preserved, and the bundled copy remains in the managed release. Skill-specific Python/Node packages and external services are not installed automatically.

The [MCP catalog](config/mcp-selection.json) adds 16 connection definitions by default. Existing connections with the same name or URL are preserved; only missing entries are added to `config.toml`. Existing model, provider, plugin, and authentication settings are preserved.

| Initial readiness | MCP entries |
| --- | --- |
| Anonymous remote endpoints: enabled by default | `firecrawl`, `exa`, `openai-docs` |
| Local launchers: enabled when the required CLI, runtime, and engine are available | `aside`, `chrome-devtools`, `arxiv`, `godot`, `drawio` |
| Authentication required: disabled by default | `tavily`, `brave-search`, `jupyter`, `figma`, `sentry`, `notion` |
| Separate engine integration required | `blender`, `unity` |

Aside uses `aside mcp` from its separately installed [official CLI](https://docs.aside.com/help/developers). Version-pinned `npx` and `uvx` server packages are not bundled as executables; they may be downloaded when Codex starts those connections. Registration and launcher readiness alone do not prove authentication or a successful MCP connection.

To add only the bundle to an existing ADHD installation while keeping its hooks and roles, use the commands below. Replace `KEY` with an MCP name from the table. `builtin enable` requires the managed entry's current prerequisites; `builtin rollback` undoes the standalone application. Open a new Codex App/CLI session to load the changed tools.

```powershell
py -3 .\adhd.py builtin apply
py -3 .\adhd.py builtin status
py -3 .\adhd.py builtin enable KEY
py -3 .\adhd.py builtin rollback
```

`builtin status` reports separate stages: `registered`, `dependencies_ready`,
`auth_integration_verified`, `connected`, and `read_verified`. `usable` becomes
true only after a successful reviewed read; enabling a definition alone does not
establish usability. OAuth and engine integrations can now advance through an
explicit probe using the existing extension read-only validation:

```powershell
py -3 .\adhd.py builtin probe KEY --consent --probe-file read-probe.json
py -3 .\adhd.py builtin probe KEY --consent --probe-file read-probe.json --oauth-token-env MCP_ACCESS_TOKEN
py -3 .\adhd.py builtin enable KEY
```

The probe file contains `{"tool":"get_status","arguments":{},"read_only":true,
"purpose":"Check the connection with a minimal read"}`; choose a real read tool
exposed by that server. A handshake without a read remains unverified for use.
The optional official Python MCP SDK must already be available. The OAuth option
names an environment variable containing an already authorized access token;
ADHD does not initiate OAuth or store the token. Enabling an OAuth connection
persists only that variable's name so Codex can use the same authentication.
Probe evidence expires after one hour and is invalidated by connection, recipe,
runtime or credential changes. A failed probe revokes prior successful evidence.
Consent covers starting the configured server (including any launcher downloads)
and the specified read; tool annotations remain advisory.

## Execution intensity and progress

For a durable task, select an explicit profile in the begin payload's
`execution_profile` field or with `native begin --profile NAME`:

| Profile | Planning and delegation | Acceptance |
| --- | --- | --- |
| `simple` | Direct work or one limited work child; plan optional | Independent review |
| `standard` (default) | Brief requirement-covered plan and bounded delegation | Automated checks and independent review |
| `deep` | Deep plan and bounded delegation | Fresh successful execution evidence covering every target, then independent review |

Small explanations can be answered directly without beginning a durable run.
Profiles change execution intensity, preserve selected models and effort, and
honor explicit local policy values. They never silently substitute a model.
`adhd.py eval --out .adhd/evaluation.json --profile deep` records the selected
intensity and fixture results; it does not measure live profile quality or cost.

Checkpoints may report fresh `criterion_results` and `completed_steps` with
execution receipt references. Progress counts newly verified requirements,
fewer failing tests, completed known plan steps, and actual target content changes
before candidate submission. Timestamp-only output, duplicate results and stale
evidence do not reset stagnation. Identical failures have separate repetition
counts; continuation and time limits remain enforced. Pause, cancellation and
budget exhaustion retain workspace ownership until the final running child stops.

## What is included

| Path | Purpose |
| --- | --- |
| `adhd.py`, `hook.py`, `adhd/` | CLI, hook entry point, controller, installers and local capability code |
| `skills/adhd-*`, `native/agents/` | Harness instructions and helper role defaults |
| `bundled/skills/`, `config/builtin-skills.json`, `config/mcp-selection.json` | Full 50-skill trees, license/source manifest and 16 MCP connection recipes |
| `schemas/`, `config/`, `examples/` | Contracts, optional guidance and generic examples |
| `tests/`, `scripts/` | Regressions and a read-only local installation audit |
| `third_party/`, `research/REUSE_MANIFEST.json` | Vendored dependencies, license texts and source attributions |

The [configuration notes](config/README.md) explain which settings are defaults and which remain under your control. The wiki router is optional: if you have one, pass its actual `skill_wiki.py` path to `adhd.py route --configure-wiki-router`. Document tools and models must be checked in the environment where they will run.

## Verify this checkout

Coding tasks use a short [Karpathy-inspired discipline](skills/adhd-native/references/coding-discipline.md)
for assumptions, minimal implementation, focused changes and sufficient-evidence stopping.
The planner, implementer and verifier share it. For Git coding tasks, the CLI captures
allowed paths and existing dirty/index state before work. Candidate admission requires an
observed scope-verification receipt and a requirement mapping for every changed file.
The hooks recheck source hashes, file inventory and captured Git metadata without executing
commands. The [native protocol](skills/adhd-native/references/protocol.md#coding-scope)
describes the commands and limits. This gate does not prove semantic simplicity or sandbox tools.

Existing native runs retain their old contract; the scope requirement applies to new coding
runs after installing this source and starting a fresh Codex session.

```powershell
py -3 -m unittest discover -s tests -q
py -3 -m unittest scripts.test_audit_local_install -q
py -3 .\adhd.py --help
```

Tests include fixtures and process-level checks. They do not prove your account's model access, hook trust, Office/Hancom rendering, or external MCP access. Run a small real task after installation and confirm the hook status in a fresh Codex session.

CI runs on Windows and Ubuntu with Python 3.11 and 3.13. The release smoke test
builds a real ZIP, checks its checksums, extracts it into a fresh offline venv,
executes the extracted CLI, installs it into an isolated Codex home, and runs the
exact commands registered in `hooks.json`. These synthetic hook events test the
installed files; they do not grant hook trust or establish live account access.

## Privacy and licenses

The installer never needs your credentials in this repository. Keep local `config.toml`, `auth.json`, `hooks.json`, `.adhd/`, generated evidence and private project records out of Git. The `.gitignore` covers common local files; review `git diff --cached` before publishing changes.

The repository's `LICENSE` is Apache-2.0. The inherited Raibit glue carries its original MIT notice in `LICENSE-RAIBIT-MIT`; vendored components retain their own notices in `THIRD_PARTY_NOTICES.md` and `third_party/`.
