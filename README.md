# ADHD — Autonomous Delegation Harness Director

ADHD v0.1.3 is a local harness for Codex App and CLI. It adds planning, execution evidence, and review gates through Codex hooks while preserving your existing authentication, model selection, router, plugins, skills, and personal configuration.

This repository contains source code and generic defaults. It contains no account credentials, user configuration, run history, or personal wiki files. The internal `apzn` package and `apzn-*` skill and role IDs remain for compatibility; `adhd.py` and `$CODEX_HOME/adhd/releases` are the primary command and installed code paths.

[한국어 안내](README.ko.md) · [Getting started](시작하기.md) · [Source and licensing notes](APZN_PROVENANCE.md)

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

On macOS/Linux, use `python3 adhd.py upgrade`. `install.ps1` and `install.sh` run the same upgrade command. The installer copies an immutable release into `$CODEX_HOME/adhd/releases`, adds nine hook groups to `hooks.json`, and installs `apzn-*` skills and roles. It does not rewrite `config.toml` or `auth.json`. Existing unmanaged hooks and instructions are retained. For an older managed APZN release, upgrade validates its release and managed files before migrating. Backups and existing `$CODEX_HOME/apzn/native` run state remain available.

Codex may ask you to trust the new hook commands. Inspect them in `/hooks` and approve them there; ADHD does not create or bypass trust. Start a new App/CLI session after an upgrade so that the loaded hook and role profiles match the installed release.

```powershell
py -3 .\adhd.py doctor
py -3 .\adhd.py rollback-native
```

`rollback-native` checks for later edits before restoring managed files. Use `--codex-home PATH` to target an isolated Codex home for evaluation. `apzn.py` remains a compatibility alias.

## What is included

| Path | Purpose |
| --- | --- |
| `adhd.py`, `hook.py`, `apzn/` | CLI, hook entry point, controller, installers and local capability code |
| `skills/apzn-*`, `native/agents/` | Harness instructions and helper role defaults |
| `schemas/`, `config/`, `examples/` | Contracts, optional guidance and generic examples |
| `tests/`, `scripts/` | Regressions and a read-only local installation audit |
| `third_party/`, `research/REUSE_MANIFEST.json` | Vendored dependencies, license texts and source attributions |

The [configuration notes](config/README.md) explain which settings are defaults and which remain under your control. The wiki router is optional: if you have one, pass its actual `skill_wiki.py` path to `adhd.py route --configure-wiki-router`. Document tools and models must be checked in the environment where they will run.

## Verify this checkout

```powershell
py -3 -m unittest discover -s tests -q
py -3 -m unittest scripts.test_audit_local_install -q
py -3 .\adhd.py --help
```

Tests include fixtures and process-level checks. They do not prove your account's model access, hook trust, Office/Hancom rendering, or external MCP access. Run a small real task after installation and confirm the hook status in a fresh Codex session.

## Privacy and licenses

The installer never needs your credentials in this repository. Keep local `config.toml`, `auth.json`, `hooks.json`, `.apzn/`, generated evidence and private project records out of Git. The `.gitignore` covers common local files; review `git diff --cached` before publishing changes.

The repository's `LICENSE` is Apache-2.0. The inherited Raibit glue carries its original MIT notice in `LICENSE-RAIBIT-MIT`; vendored components retain their own notices in `THIRD_PARTY_NOTICES.md` and `third_party/`.
