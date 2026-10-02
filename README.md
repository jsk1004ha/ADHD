# ADHD — Autonomous Delegation Harness Director

ADHD v0.1.3 is a local harness for Codex App and CLI. It adds planning, execution evidence, and review gates through Codex hooks while preserving your existing authentication, model selection, router, plugins, skills, and personal configuration.

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

## Privacy and licenses

The installer never needs your credentials in this repository. Keep local `config.toml`, `auth.json`, `hooks.json`, `.adhd/`, generated evidence and private project records out of Git. The `.gitignore` covers common local files; review `git diff --cached` before publishing changes.

The repository's `LICENSE` is Apache-2.0. The inherited Raibit glue carries its original MIT notice in `LICENSE-RAIBIT-MIT`; vendored components retain their own notices in `THIRD_PARTY_NOTICES.md` and `third_party/`.
