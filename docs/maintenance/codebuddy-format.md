# CodeBuddy format dependency

The complete upstream surface `cbut` reads, in one place. The single source of truth is the registry
at the top of [`scripts/cbut-sync.py`](../../scripts/cbut-sync.py); the block below is **generated
from it** so this page cannot describe a coupling the code no longer has.

```bash
cbut format              # print it
cbut format --write      # refresh the block in this file after changing the registry
```

## The surface, as the code reads it

<!-- BEGIN generated: cbut format -->
- **Record types a handler claims**: `ai-title`, `function_call`, `function_call_result`, `message`, `model-usage`, `session-meta`, `turn-metrics`
- **Record types treated as a model response**: `function_call`, `message`, `model-usage`
- **Transcript record fields**: `_meta`, `arguments`, `callId`, `content`, `cwd`, `durationMs`, `id`, `name`, `output`, `providerData`, `sessionId`, `status`, `timestamp`, `tokenDelta`, `type`
- **`trace` stream record types claimed by span handlers**: `span_end`, `span_start`
- **`trace` stream record types declined (trace envelope)**: `trace_end`, `trace_start`, `trace_update`
- **`trace` final-file keys**: `span`, `spans`, `trace`
- **`trace` span fields**: `agentName`, `duration`, `spanId`, `startedAt`
- **agent-span roots never stored (infrastructure)**: `cli`, `ptc`
- **span outcome → status**:
  - `error` → `incomplete`
  - `ok` → `completed`
- **`providerData` fields**: `conversationRequestId`, `messageId`, `model`, `rawUsage`, `requestModelId`, `toolResult`
- **`providerData.rawUsage` fields**: `cache_creation_input_tokens`, `cache_read_input_tokens`, `completion_tokens`, `credit`, `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens`, `prompt_cache_write_tokens`, `prompt_tokens`, `total_tokens`
- **`rawUsage` nested cache path (fallback for the hit above)**: `cached_tokens`, `prompt_tokens_details`
- **`_meta` fields**: `baggage`
- **message content-block fields**: `text`
- **`providerData.toolResult` fields**: `content`, `error`
- **tool argument names**: `agent_type`, `args`, `command`, `skill`, `subagent_type`, `toolName`, `tool_name`
- **inventory JSON keys**: `agents`, `commands`, `id`, `installPath`, `mcpServers`, `models`, `name`, `plugins`, `tools`, `url`, `version`
- **Tool names that drive classification**: `Skill`, `Agent`, prefix `mcp__` with separator `__`
- **Meta tools (wrap another tool)**: `DeferExecuteTool`, `ListMcpResources`, `ReadMcpResource`, `ToolSearch`, `WaitForMcpServers`
- **Tolerant name tables** (an unknown name still indexes): 35 builtin tools, 17 builtin agents, 0 builtin models, 0 builtin commands, default agent type `general-purpose`
- **Tool-name validity**: empty, over 200 characters, or containing whitespace/control characters is counted as `invalid_tool_name` and not stored as a call. A name the CLI refused as "not a direct tool" is withdrawn the same way when the installed catalogue does not hold it either — nothing ran, and the row read `removed` for a tool that never existed
- **Builtin catalogue source**: fallback constants (product.json unresolved) — the installed `codebuddy` package's `product.json`, found by resolving the executable on `PATH` and walking up; `CBUT_PRODUCT_JSON` overrides the search. Membership follows the package, so a name it drops stops being inventoried; `kind` (active/internal) stays this tool's taxonomy
- **Markers parsed out of text**: `<command-name>\s*/?([^<\s]+)\s*</command-name>` · `codebuddy\.session_id=([^,\s]+)`
- **On-disk layout, relative to `~/.codebuddy`**:
  - `projects` / `**/*.jsonl` — transcripts, the usage record
  - `traces` / `*/trace_*.jsonl` — agent-span streams, read incrementally like transcripts
  - `*/trace_*.json` — the whole trace written at completion, the only record carrying `trace.sessionId`: a stream-only span stays session-less
  - `skills` — a direct child holding `SKILL.md` is a skill; one category level below is still read, as is `**/SKILL.md` for anything deeper. Symlinked entries are followed exactly one level: `glob("**")` does not cross a symlink, which is how the whole symlinked tree went missing at once
  - `agents` / `*.md` — user agents
  - `mcp.json` — MCP servers
  - `plugins` / `installed_plugins.json`, keys split on `@`
  - a name starting with `.` is never an installed item
- **plugin subdirectories (dir → kind)**:
  - `skills` → `skill`
  - `agents` → `agent`
  - `commands` → `command`
  - a plugin holding `SKILL.md` at its **install root** is a skill under the plugin's own name: the directory there is the version, and the frontmatter `name:` is not read (a plugin here declares a different one)
- **Transcript framing**: one JSON record per line, separated by a newline byte (`b'\n'`)
- **`inventory` is a snapshot of what is installed now**: rebuilt on every sync, deleted and re-inserted in one transaction. A name that leaves it stays listed by the usage tables with `installed = 0`, which is the only thing that distinguishes "removed" from "never installed"
- **Not inventoried from a plugin**: tools and MCP servers. No installed plugin ships either (`plugin.json` declares neither, and none of the nine on this machine has a tool or MCP file), so there is no rule to read and no rule to guess at. If a plugin ever does, a name it adds would come back from the usage tables alone and read as *removed* — the same failure a root `SKILL.md` had, so this line is the place to look first
<!-- END generated -->

## Deliberately not read

- **Message bodies and tool argument values.** Slash-command names are recovered by scanning
  `<command-name>` markers inside text blocks; only the captured name is stored. `Agent`'s
  `description` and the `aiTitle` / `session-meta` titles were read by earlier versions and are
  gone as of schema v5 — they are prose derived from the conversation. See `AGENTS.md`
  ("Metadata only, never content") and `test_privacy.py`, which scans every text column of every
  table for sentinel values.
- **`~/.codebuddy/traces`** (OTel spans). Not ingested: that is where internal agents
  (`autoModeClassifier`, `summaryGenerator`, …) live, and their counts stay at zero until trace
  ingestion is designed rather than bolted on. This is a decision, not an oversight — the
  alternative is guessing an entity's usage from a format we do not parse.

## Known record types with no handler

Measured on this machine on 2026-10-09, over ≈ 500 transcripts: `reasoning` ≈ 15k records,
`file-history-snapshot` ≈ 6k, `summary` ≈ 350 (exact counts are personal usage volume — reprint them
with `cbut health`). These are **counted, not silenced** — they appear in
the `unparsed` table, in `cbut health`, in the sync summary and on the TUI status bar. An allowlist
quieting them would also hide the day one of them starts carrying something we index.

## When a panel goes empty

1. `cbut health` — if `unparsed` gained a reason, upstream renamed a record type or a field. The
   reason strings are `type:<name>` (a record no branch claimed), `unparseable_line` (a complete
   line that is not JSON), `unreadable_file:<OSError>`.
2. If `unparsed` did **not** move but a panel did, the change is inside a record we already claim:
   a renamed *field* reads as `NULL`, not as an error. Compare the generated block above against a
   real transcript line (`~/.codebuddy/projects/**/*.jsonl`, read-only — this tool never writes
   there).
3. Fix the registry and the handler in the same change. `test_format_registry.py` fails if you do
   only one half, and names the call sites the rename still owes.
4. Re-index: `cbut sync --full` (a snapshot is taken first and the last five are kept; `cbut
   restore` lists and rolls back to them).
