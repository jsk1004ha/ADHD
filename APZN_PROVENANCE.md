# ADHD v0.1.3 source notes

ADHD derives from Raibit Harness v1.2 and retains the original MIT notice in `LICENSE-RAIBIT-MIT`. The repository's current top-level license is Apache-2.0. Reused third-party code and instructions retain their own licenses and attribution in `THIRD_PARTY_NOTICES.md`, `third_party/`, and `research/REUSE_MANIFEST.json`.

The v0.1.3 code uses `adhd.py` and `$CODEX_HOME/adhd/releases` as the primary execution paths. The `apzn` Python module, `apzn.py` alias, role/skill IDs and existing `$CODEX_HOME/apzn/native` state remain for compatibility. The native installer checks exact installed release bytes and managed files before replacing a release. It preserves the user's existing Codex settings and does not manufacture hook trust.

This public source is an allowlisted distribution of the harness code, examples and tests. Local installation receipts, personal configuration, authentication files, private research notes and generated evidence are intentionally absent. Tests exercise local contracts and fixtures; they do not certify third-party model, document UI or MCP availability in another environment.
