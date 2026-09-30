# Third-party components — ADHD v0.1.3

The native execution engine is the user's existing Codex installation, not redistributed here.
This bundle directly reuses five components: tomlkit, filelock, an adapted smolagents function,
an adapted Superpowers verification instruction module, and a spec-kit template.

Exact origins, versions or source blob IDs, modifications, uses and packaged SHA-256 digests
are in `research/REUSE_MANIFEST.json`. Licenses are included alongside the components.
Other harnesses and papers are research references only. They are not claimed as installed,
vendor-copied, or benchmark-reproduced. No credentials, models, proprietary application binaries,
font files, original user settings, or remotely executable installer downloads are bundled.

smolagents adaptation: Copyright 2024 The HuggingFace Inc. team. All rights reserved.
Licensed under Apache License 2.0 (`third_party/Apache-2.0.txt`).
Superpowers: Copyright (c) 2025 Jesse Vincent. MIT (`third_party/superpowers/LICENSE`).
spec-kit: Copyright GitHub, Inc. MIT (`third_party/spec-kit/LICENSE`).
Other distribution notices: `third_party/tomlkit-LICENSE`, `third_party/filelock-LICENSE`.

Coding discipline: independently rewritten principles inspired by the community skill
`multica-ai/andrej-karpathy-skills`, pinned at `2c606141936f1eeef17fa3043a72095b4765b9c2`.
The upstream skill and README declare MIT and attribute the observations to Andrej Karpathy.
The upstream text is not bundled. The adaptation, source blob and checksum are recorded in
`skills/adhd-native/references/coding-discipline.md` and `research/REUSE_MANIFEST.json`.

Document backends (lxml/PyMuPDF/python-docx/python-pptx/hwpkit/Hancom) and MCP SDK are optional existing-install integrations, not vendored binaries or new full document engines. Referenced external memory systems are not bundled.
