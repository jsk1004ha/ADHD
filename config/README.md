# Configuration boundaries

`adhd.py upgrade` preserves the existing `$CODEX_HOME/config.toml` and authentication file. It does not select a model, change a router, install plugins, enable paid services, or write a personal configuration template over an existing file.

The installer adds managed entries to `$CODEX_HOME/hooks.json`, a marked guidance block in `$CODEX_HOME/AGENTS.md`, `apzn-*` skills and six `apzn-*` agent roles. New releases are copied to `$CODEX_HOME/adhd/releases`; backups are kept under `$CODEX_HOME/adhd/backups`. Old `$CODEX_HOME/apzn/native` state is retained during migration. Codex owns hook trust approval; inspect new commands with `/hooks`.

`native/agents/` contains example role pins for `gpt-6-sol`, `gpt-6-luna` and `gpt-6-astra`. These identifiers are defaults for this distribution, not an assertion that your account or router provides them. Existing roles are preserved unless `--migrate-existing-roles` is explicitly chosen. `config/AGENTS.compact.md` is an optional compact guidance template; `--compact` must be chosen explicitly.

For local testing, create a temporary Codex home with a minimal `config.toml` and pass its path using `--codex-home`. Never commit your working `$CODEX_HOME` or copied authentication and hook trust state.
