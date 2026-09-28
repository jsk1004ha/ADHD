---
name: adhd-extensions
description: Act on requests to add a skill, MCP or document adapter to this harness. Reuse existing installations, inspect source, stage exact versions, apply minimal changes, probe capability, report auth/reload requirements and preserve rollback.
---
# Add, verify, then use
The user's ordinary request authorizes the requested reversible installation, not arbitrary paid accounts, external writes or permission bypass. Search installed skills/MCP first. If present, verify availability and use it rather than reinstall. A README/catalog entry alone is not an authenticated runtime.

For a new skill, inspect the upstream source/license/dependencies using existing trusted tools. Fetch into a local staging directory at a recorded version/commit; read SKILL.md and scripts. No curl|sh, mutable @latest or install-time blind script execution. Record a manifest:
```json
{"id":"my-new-skill","kind":"skill","source":"exact upstream URL + commit/version + license","local_path":"actual reviewed local folder"}
```
Run `ADHD_PYTHON ADHD_ROOT/adhd.py extension stage --payload-file MANIFEST.json`, then `extension apply --id ID --reviewed`. A later modification stops hash verification. Existing skills are not overwritten. Refresh Codex skill discovery or open a new session when required; perform a small real task. The registry says installed_reload_required, not working.

For local stdio MCP, inspect/download the exact server through Codex's existing approvals. Use an absolute executable path and pinned local scripts, not a shell expression:
```json
{"id":"example-mcp","kind":"mcp-stdio","source":"upstream + version + license","execution":{"kind":"script","command":"ABSOLUTE_BINARY","entrypoint":"ABSOLUTE_SERVER_SCRIPT","cwd":"ABSOLUTE_DIRECTORY","runtime_root":"ABSOLUTE_REVIEWED_SOURCE_DIRECTORY","args":[],"runtime_files":["ABSOLUTE_SERVER_SCRIPT","ABSOLUTE_LOCAL_HELPER"]},"env_vars":["EXAMPLE_API_KEY"],"probe_call":{"tool":"read_status","arguments":{},"read_only":true,"purpose":"Check a harmless status response"}}
```
Never store actual secrets. The complete local file set beneath `runtime_root` must be listed in `runtime_files`; added, removed or modified files fail pin validation before apply/probe/launch. Script servers currently require the pinned ADHD Python executable and `.py` entrypoint, run with `-S`, and cannot load ordinary system/user site packages. Python interpreter control variables (any `PYTHON*` name, including `PYTHONPATH`) are rejected in `env_vars` and excluded from inherited process environment; module mode sets its own reviewed `PYTHONPATH`. For external packages, use a module execution manifest whose `package_root` and `installed_files` cover the entire reviewed dependency tree; that tree is rechecked on every launch. A pinned lock file records package selection when provided. Stage/apply records the server disabled. `extension probe --id ID` uses the official optional MCP Python SDK to initialize, list tools and call only the exact manifest-reviewed read-only probe tool. It also requires the live tool annotation `readOnlyHint=true`; a server's annotation is an additional check, not permission. Missing SDK/auth, absent/mutating tool annotation, error result or a missing probe call leaves the extension disabled with a precise status. Only the observed call is verified; reload host tool discovery and verify the requested user task separately.

Remote/OAuth MCPs: use Codex's official existing add/login flow and actual current documentation instead of forcing the local stdio adapter. Aside Windows now exists; use its Windows installer/installed CLI, not Unix curl|bash. Do not infer the installed binary path or bypass browser login. `extension rollback --id ID` refuses to erase later edits. HTTP/OAuth services are not automatically probed by this local adapter. Do not edit unrelated model/provider/trust settings.
