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
- **Transcript record fields**: `_meta`, `arguments`, `callId`, `content`, `cwd`, `durationMs`, `id`, `name`, `providerData`, `sessionId`, `status`, `timestamp`, `tokenDelta`, `type`
- **`providerData` fields**: `conversationRequestId`, `messageId`, `model`, `rawUsage`
- **`providerData.rawUsage` fields**: `cache_creation_input_tokens`, `cache_read_input_tokens`, `completion_tokens`, `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens`, `prompt_cache_write_tokens`, `prompt_tokens`, `total_tokens`
- **`rawUsage` nested cache path (fallback for the hit above)**: `cached_tokens`, `prompt_tokens_details`
- **`_meta` fields**: `baggage`
- **message content-block fields**: `text`
- **tool argument names**: `agent_type`, `args`, `command`, `skill`, `subagent_type`, `toolName`, `tool_name`
- **inventory JSON keys**: `installPath`, `mcpServers`, `plugins`, `version`
- **Tool names that drive classification**: `Skill`, `Agent`, prefix `mcp__` with separator `__`
- **Meta tools (wrap another tool)**: `DeferExecuteTool`, `ListMcpResources`, `ReadMcpResource`, `ToolSearch`, `WaitForMcpServers`
- **Tolerant name tables** (an unknown name still indexes): 35 builtin tools, 17 builtin agents, default agent type `general-purpose`
- **Markers parsed out of text**: `<command-name>\s*/?([^<\s]+)\s*</command-name>` · `codebuddy\.session_id=([^,\s]+)`
- **On-disk layout, relative to `~/.codebuddy`**:
  - `projects` / `**/*.jsonl` — transcripts, the only ingested log tree
  - `skills` / `**/SKILL.md` — a skill is the parent of a manifest
  - `agents` / `*.md` — user agents
  - `mcp.json` — MCP servers
  - `plugins` / `installed_plugins.json`, keys split on `@`
  - a name starting with `.` is never an installed item
- **plugin subdirectories (dir → kind)**:
  - `skills` → `skill`
  - `agents` → `agent`
  - `commands` → `command`
- **Transcript framing**: one JSON record per line, separated by a newline byte (`b'\n'`)
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
