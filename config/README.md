# Configuration boundaries

`adhd.py upgrade` adds only absent reviewed MCP entries to `$CODEX_HOME/config.toml`, preserving existing MCP names and URLs and unrelated model, router, provider, plugin, permission and comment settings. It never writes credentials or replaces `auth.json`. The 16 connection recipes are in `mcp-selection.json`; the 50 complete skill trees and their exact source/license/file hashes are in `builtin-skills.json` and `bundled/skills/`.

The installer adds managed entries to `$CODEX_HOME/hooks.json`, a marked guidance block in `$CODEX_HOME/AGENTS.md`, `adhd-*` skills, the 50 bundled skills and six `adhd-*` agent roles. Existing same-name public-bundle and workflow-helper folders are preserved whole; the full immutable release keeps a routable fallback copy. The four original native capabilities retain their unmanaged-file conflict checks. New releases are copied to `$CODEX_HOME/adhd/releases`; backups are kept under `$CODEX_HOME/adhd/backups`. Codex owns hook trust approval; inspect new commands with `/hooks`.

The seven workflow helpers (`adhd-shape`, `adhd-challenge`, `adhd-decide`, `adhd-steer`, `adhd-unblock`, `adhd-retro`, `adhd-optimize`) are installed by normal `upgrade`, separately from the 50 public skills. Their status and immutable fallback paths are recorded in `native-installation.json` under `workflow_skills`. Rollback removes only managed helper files and empty added folders, preserving user additions. Upgrade reinstalls previously managed helpers alongside retained user files; it refuses new-file collisions and does not transfer that ownership to a different agents home. ADHD local discovery also sees the current source/release helpers; configured disabled paths and host-provided catalogs still apply, and a valid wiki-router result remains authoritative. `adhd-optimize` is for code optimization. See the [shared handoff](../skills/adhd-native/references/skill-handoff.md) for existing-owner and native API boundaries.

`adhd.py builtin apply` adds only the skill bundle and MCP definitions to an existing installation, with its own checked `builtin rollback`. `builtin status` reports prerequisites and registration; `builtin enable KEY` checks readiness before enabling a managed entry. Three anonymous HTTP endpoints are enabled by default. Local stdio recipes are enabled only when their launchers and required engines are available; OAuth, missing credentials and unmet integration prerequisites remain disabled. Node/uvx packages and proprietary engines are launch-time dependencies, not bundled binaries. The official Aside CLI is an external prerequisite. A definition or launcher check does not prove a live handshake. Inspect the actual OS shell and skill-specific dependencies before following a bundled command.

The local metadata router and `adhd.py skills` search share the same candidate
filter: an existing same-name user, project, configured or plugin skill takes
precedence over ADHD release and bundled-fallback copies. Both remain
discoverable; the shipped fallback is selectable when no enabled existing
definition is present. A valid result from the configured wiki router still owns
selection.

`native/agents/` contains example role pins for `gpt-6-sol`, `gpt-6-luna` and `gpt-6-astra`. These identifiers are defaults for this distribution, not an assertion that your account or router provides them. Existing roles are preserved unless `--migrate-existing-roles` is explicitly chosen. `config/AGENTS.compact.md` is an optional compact guidance template; `--compact` must be chosen explicitly.

For local testing, create a temporary Codex home with a minimal `config.toml` and pass its path using `--codex-home`. Never commit your working `$CODEX_HOME` or copied authentication and hook trust state.

Native begin accepts `execution_profile: "simple" | "standard" | "deep"` (default
`standard`), also exposed by `--profile`. Profiles vary plan, delegation and review
intensity; they do not rewrite role model or effort pins. Explicit fields in
`adhd/native-policy.json` take precedence over profile defaults. Time and round
limits still apply, and a durable deliverable always needs independent acceptance.

`builtin probe KEY --consent --probe-file FILE` runs one reviewed read with the
optional official MCP SDK. Its JSON uses the extension `tool`, `arguments`,
`read_only`, and `purpose` schema. `--oauth-token-env NAME` names an already
authorized access-token environment variable; only its name can enter the managed
configuration on enable. Separate readiness stages and `usable` distinguish
registration from live read success. Local evidence expires after one hour and
is bound to the recipe, connection, executable identities and credential values
by a digest. Probes never create or copy OAuth credentials or hook trust.
