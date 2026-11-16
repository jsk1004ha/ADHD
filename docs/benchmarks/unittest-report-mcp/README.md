# Reusable unittest report tool

A small experimental dedicated MCP used in the
[real-task comparison](../2026-11-16-dedicated-mcp.md). The same implementation
runs as a direct CLI. Python standard library only; no package installation or
global MCP configuration changes are required.

Prepare `manifest.json` with an `inputs` array. Each entry contains `file`
(a unique basename), `bytes` and `sha256`. Place the original log files in an
`inputs` directory next to the manifest. Inputs are read and verified, never
edited. The output directory must be outside `inputs`.

```json
{"inputs": [{"file": "tests.log", "bytes": 123, "sha256": "actual-sha256-of-tests.log"}]}
```

The example digest and byte count must be replaced with actual input identities.

Run the dedicated MCP through the supplied client:

```powershell
python -X utf8 mcp_client.py --manifest manifest.json --output report
```

Or reuse the identical functionality directly:

```powershell
python -X utf8 report_tool.py --manifest manifest.json --output report
```

Both write `report.json`, Korean `report.md`, `generate_report.py` and
`replay.json`. Replay the standalone generated script while the original inputs
remain available:

```powershell
python -X utf8 report/generate_report.py
```

The MCP client additionally saves `mcp-transcript.json`. It launches the local
server, initializes the protocol, lists and invokes `generate_unittest_report`,
checks the response, then shuts the child process down. These local receipts
contain paths; review them before sharing.

For an MCP client that supports manual stdio configuration, run
`python -X utf8 report_tool.py --stdio`. The tool's arguments are `manifest_path`
and `output_dir`, both strings. The server exposes this report operation only,
with a limited MCP method surface; it is not a general-purpose MCP framework.

The parser selects the last complete `Ran N test(s) in Xs` plus `OK`/`FAILED`
summary, preserves failure/error/skip counts, handles UTF-8 BOM and UTF-16 BOM,
and retains unrecognized files with null test/time values. A historical failed
test log is report data; successfully generating a report from it returns
success. Invalid hashes, missing inputs and decoding errors fail the operation.
