# Third-party components — ADHD v0.1.3

The native execution engine is the user's existing Codex installation, not redistributed here.
The runtime directly reuses five components: tomlkit, filelock, an adapted smolagents function,
an adapted Superpowers verification instruction module, and a spec-kit template.

Exact origins, versions or source blob IDs, modifications, uses and packaged SHA-256 digests
are in `research/REUSE_MANIFEST.json`. Licenses are included alongside the components.
Other harnesses and papers remain research references only unless listed below. No credentials, models, proprietary application binaries,
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

The default distribution also contains 50 complete public skill trees under `bundled/skills/`:
30 from `https://github.com/openai/skills` at commit `49f948faa9258a0c61caceaf225e179651397431`,
and 20 from `https://github.com/K-Dense-AI/scientific-agent-skills` at commit
`154988403bb5a18e9d3c0ce4e6d5e2e4b184a298`. The per-skill origin, exact paths,
file hashes, license, and any local edits are recorded in `config/builtin-skills.json`.
Each copied skill directory contains its applicable upstream license text. The eight
Figma Developer Terms skill trees and the Cloudflare skill with missing local references
were excluded from the OpenAI selection. Scientific skill citation directives were
removed; the scientific critical thinking skill's unavailable optional schematics
command was replaced with local diagram guidance. The original commits remain named
for comparison. These skills may mention external tools, accounts or packages; bundling
their instructions does not bundle those dependencies or authorize their use.

`config/mcp-selection.json` records 16 reviewed MCP connection recipes. Their server
binaries, hosted services, accounts, API keys and the proprietary Aside CLI are not
redistributed. Launch packages are pinned in the recipes; install-time registration
does not itself download or execute them.
