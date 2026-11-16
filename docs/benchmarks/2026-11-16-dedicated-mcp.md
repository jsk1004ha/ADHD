# Dedicated MCP comparison on the same real task

The dedicated MCP completed the same twelve-log report with identical JSON and
correct Markdown. Once prepared, it used fewer tokens than writing the report
generator again. Reusing the identical implementation through a direct CLI used
slightly fewer total tokens than MCP. This measures reusable task capabilities;
it does not establish an inherent token advantage of the MCP protocol.

## Experiment

The inputs and output contract are unchanged from the
[earlier general Node MCP experiment](2026-11-16-mcp-first.md): twelve frozen real
unittest logs, 19,807 bytes, SHA-256 checked, and JSON/Korean Markdown plus a
runnable report generator. Three fresh executor agents used `gpt-5.6-sol`,
`medium`, with no inherited conversation. Each had its own output directory and
was instructed not to read another lane or the independent oracle.

No suitable installed or researched dedicated unittest-summary MCP was found.
The previous Python parsing approach was adapted into a small local
`generate_unittest_report` tool. It performs manifest verification, last-complete
summary parsing, aggregation and artifact generation server-side, returning only
totals, paths and hashes. It does not accept arbitrary code. The ready CLI
control uses the exact same implementation. Source hashes were frozen before
the workers started. There are no additional package dependencies.

This App session could not register a new native MCP tool dynamically. The MCP
worker invoked a Python stdio client through the shell. Its receipt records real
`initialize`, `notifications/initialized`, `tools/list` and `tools/call` messages
and responses. The adapter invocation is included in worker usage. Native App
registration and tool-discovery overhead were not measured. This is a custom
dedicated MCP, not a benchmark of an existing third-party product.

## Observed usage

| Metric | Fresh Python implementation | Ready dedicated MCP | Ready identical CLI |
| --- | ---: | ---: | ---: |
| Input tokens | 634,070 | 230,465 | 227,099 |
| Cached input tokens | 594,432 | 223,488 | 222,208 |
| Uncached input tokens | 39,638 | 6,977 | 4,891 |
| Output tokens, including reasoning | 6,451 | 1,303 | 1,300 |
| Total tokens, including cached input | 640,521 | 231,768 | 228,399 |
| Uncached input plus output | 46,089 | 8,280 | 6,191 |
| Agent tool calls | 13 | 5 | 5 |
| Wall seconds | 179.640 | 59.751 | 57.849 |

These are final provider-reported rollout counters for completed workers, not
text-length estimates or price estimates. Cached input is included in input;
reasoning is included in output. All attempts and checks remain counted.

Against the fresh implementation, dedicated MCP reduced total tokens **63.8%**,
uncached input plus output **82.0%**, and output **79.8%**. Against ready CLI, MCP
used **1.5% more total tokens** and **33.7% more uncached input plus output**.
Different cache hits substantially affect the latter; this is not a controlled
measurement of protocol overhead. Relative to the earlier historical baseline,
the MCP's total and uncached-plus-output counts were 49.3% and 76.1% lower,
respectively; the new three-way comparison is the primary evidence.

## Quality

An independent oracle predating this dedicated tool checked all JSON values and
all 84 Markdown table cells. All three JSON files were byte-identical. Ready
CLI and MCP Markdown were also byte-identical; fresh Python Markdown used
different prose but satisfied the same contract. All contained the failed-file
list, unrecognized status and historical/duplicate-execution caveat.

The preserved historical totals were 1,396 test executions, 8 passed logs,
4 failed logs, 9 failures, 5 errors and 16 skips. These are recorded historical
executions, not 1,396 newly executed tests. Original input hashes and prepared
tool hashes remained unchanged. The parent replayed all three delivered
generators; both report hashes remained unchanged in every lane.

Six dedicated-tool preflight tests passed, covering last complete summaries,
incomplete logs, the previously observed UTF-16 failure, manifest identity/path
validation, real MCP generation against the old oracle, and an MCP tool error.

## Preparation cost and limits

Ready-tool worker usage excludes preparation. At the snapshot immediately after
all workers completed, the coordinator's common preparation, orchestration and
independent checks used **55,687 uncached-input-plus-output tokens**. The MCP
candidate researcher used **61,700**, for **117,387** combined. Later report
writing, publication and the final response are excluded from that snapshot.
The common cost is not attributable exclusively to MCP construction. **A lower
end-to-end cost for this single task, including preparation, is not established.**

This is one task and one run per arm. Cache state was not forced equal. The fresh
implementation included parser authoring and additional self-checks. Reduced
model work from a prepared capability is supported; intrinsic MCP savings and
general results across tasks or providers are not.

Use an appropriate ready MCP when it provides useful domain operations and
compact results. Reuse an existing verified CLI when it already provides the
same operation efficiently. Avoid rewriting the same processing code each time.

## Reuse and evidence

The [standalone tool and client](unittest-report-mcp/README.md) are included for
reuse. Local evidence retains the frozen experiment design, measured counters,
independent quality and replay checks, and actual MCP wire transcript. Private
logs, raw agent transcripts and personal paths are not published.

Implementation references: [official MCP stdio specification](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/transports/stdio.mdx)
and [official Filesystem MCP](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem).
The latter exposes file access, not this task's unittest summary aggregation.
