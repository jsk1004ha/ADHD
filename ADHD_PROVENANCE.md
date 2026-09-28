# ADHD v0.1.3 source notes

ADHD derives from Raibit Harness v1.2 and retains the original MIT notice in `LICENSE-RAIBIT-MIT`. The repository's current top-level license is Apache-2.0. Reused third-party code and instructions retain their own licenses and attribution in `THIRD_PARTY_NOTICES.md`, `third_party/`, and `research/REUSE_MANIFEST.json`.

The v0.1.3 code uses `adhd.py`, the `adhd` Python package and `adhd-*` skill and role IDs throughout. There is no old command alias. The native installer locates earlier managed installations from their records, verifies the recorded release file manifest and managed backups, and preserves existing Codex settings. Prior session state is reused in place only when its schema identifies one unambiguous state directory. Hook trust is never manufactured or bypassed.

This public source is an allowlisted distribution of the harness code, examples and tests. Local installation receipts, personal configuration, authentication files, private research notes and generated evidence are intentionally absent. Tests exercise local contracts and fixtures; they do not certify third-party model, document UI or MCP availability in another environment.
