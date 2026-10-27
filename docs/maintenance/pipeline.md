# The data pipeline

A number on a screen — `1,204` calls, `18.4M` tokens, one row in the Tokens tab — is the end of a
four-stage chain. This document names the code at each stage so a wrong number can be traced instead
of guessed at. It is the route, not the rules: what each stage *must* do lives in
[`AGENTS.md`](../../AGENTS.md), the record shapes it reads in
[`codebuddy-format.md`](codebuddy-format.md), and the sizes and timings it costs in
[`measurements.md`](measurements.md).

| Stage | Code | Decides |
|---|---|---|
| 1. Read | `scripts/cbut-sync.py` | which files, how far into each, what one record means |
| 2. Store | `scripts/cbut_db.py` (schema half) | table shape, migrations, what a row is keyed by |
| 3. Query | `scripts/cbut_db.py` (query half) | windows, the installed ∪ used union, display totals |
| 4. Present | `scripts/cbut-tui.py`, `scripts/cbut-stats.py` | one row per tab, one line per report |

Stages 3 and 4 reach stage 1 through `cbut_db.load_sync()` (`scripts/cbut_db.py:136`): a hyphenated
filename cannot be imported, so the module is loaded once and cached in `sys.modules`. There is no
second copy of the parser, and there must not be — a test that monkeypatches `db.q_tools` and a TUI
that reads `tab.query` only agree because both resolve the function at call time.

## 1. Read — `cbut-sync.py`

### One run

`run()` (`:1697`) takes an exclusive lock (`:1664`), opens one read-write connection and closes it in
`finally`; the TUI calls this from a worker thread, so a leaked connection would be one per failed
run rather than one per process. `_run()` (`:1712`) then:

1. **Schema first** — `db.ensure_schema()` (`:1713`), so the writer never meets a table it does not
   know.
2. **`--full`** — snapshot, then `db.reset()` (`:1714-1720`); a snapshot of an empty database is
   skipped on purpose.
3. **Inventory** — `scan_inventory()` (`:1722`) records what is installed *right now*:
   `collect_inventory` (`:552`) reads the disk, `replace_inventory` (`:659`) swaps the snapshot in
   one transaction, so a failed scan leaves the previous one intact.
4. **Discovery** — `projects/**/*.jsonl` for transcripts, plus `traces/*/trace_*.jsonl` and
   `traces/*/trace_*.json` (`:1726-1733`).
5. **Stale-offset pre-pass** (`:1746-1775`) — if *any* file's stored offset is stale (shrunk,
   rewritten, no longer on a line boundary), the whole database is rebuilt: snapshot first, carry
   the sessions no surviving file can rebuild (`:1841`), re-read every file from zero. This is the
   only destructive step a plain `cbut sync` can take. The predicate is `_needs_reset` (`:1637`),
   which leans on `_starts_on_line_boundary` (`:1622`).
6. **Main loop** (`:1777`) — per file: read the new bytes, index them, write
   `sync_state(file_path, size, mtime, offset)` (`:1822`) and commit. `size` is the file's real
   size, not the offset, because that is what the next run compares to notice a rewrite.
7. **Whole-trace files** (`:1834`) — re-read entirely every run, keyed by `spanId`, so the re-read
   is idempotent and needs no `sync_state` row.
8. **Backfill** — `backfill_plugin_usage` (`:1869`) attributes skill/agent/command rows to a
   plugin once the inventory can name one; it runs after the reads because the ownership tables are
   built from the inventory.
9. **Report** — the dict at `:1870` is what `cbut sync` prints, what the TUI's status line reads,
   and what `cbut health` elaborates. An unrecognised record type is counted and printed loudly
   (`:1908`) because a silent format change is the one failure every panel shares.

### One file

`_read_incremental` (`:810`) is the only reader of the newline-framed tail, shared by transcripts
and trace streams so the two cannot disagree about where an offset may resume. It consumes only
complete lines; a half-written final line is retried next run unless it already parses as complete
JSON. `index_file` (`:859`) then hands each record to `_handle_record` and returns the new offset.
It never touches `sessions` — one session spans its main transcript *and* every subagent
transcript, so a single file's contribution to the accumulated totals cannot be subtracted in
isolation.

### One record

`_handle_record` (`:1243`) is a dispatch on `rec["type"]`, and every branch either writes or
explicitly claims the record:

| `type` | Effect | Line |
|---|---|---|
| `function_call` | `tool_calls`, plus `skill_usage` / `agent_usage` / `mcp_usage` by classification | `:1252` |
| `function_call_result` | status + duration onto the four call tables; a refused call is withdrawn | `:1299` |
| `session-meta`, `ai-title` | claimed, nothing stored (the title is prose) | `:1330` |
| `turn-metrics` | `sessions.tokens` / `duration_ms` — the context metric, never model tokens | `:1340` |
| `message` | the session's model, and slash-command names from the text blocks | `:1375` |
| `model-usage` | claimed; `_record_model_response` already stored the same numbers | `:1388` |
| anything else | `unparsed` count, keyed `type:<name>` | `:1395` |

`_record_model_response` (`:1099`) runs before the dispatch, because a model response can arrive on
a record type that also carries other payload. It is the only writer of `model_responses`, and it
reads `providerData.rawUsage` only: no credit arithmetic, no estimate, no `tokenDelta`.

Classification (`_classify`, `:751`) decides which of the four call tables a `function_call` lands
in; a tool name that no table can render is rejected as `invalid_tool_name` rather than stored.

### What is not read

- `~/.codebuddy/**` is never written — the index is a separate SQLite file.
- Prompt and response text, and tool argument *values*, are never stored; the one exception is the
  `<command-name>` marker (`_record_commands`, `:1211`).
- Traces are read for **agent spans only** (`index_trace_stream` `:933`, `index_final_trace`
  `:956`). A trace never supplies a model response or a token count, which is why the internal
  agents that appear only there read `0` by construction.

## 2. Store — `cbut_db.py`, schema half

`SCHEMA_VERSION = 13` (`:25`); 12 tables and 5 views (`TABLES_SQL` `:155`, `VIEWS_SQL` `:585`).
`ensure_schema` (`:1021`) is additive and runs on every open; `migrate` (`:1051`) applies the
per-step functions above it (`:783-1018`), each of which returns whether it changed anything.
`reset` (`:1070`) empties the derived tables for `--full` without touching `inventory` or `meta`.
The five views are not written out by hand: `_entity_aggregate` (`:544`) holds each kind's
aggregate once, and the same text backs the view and that kind's `q_*` function — the view leaves
out only the time window (a view cannot be parameterized) and the `ORDER BY`.

Two rules are worth knowing before reading a query:

- **Name is the key, except Tokens.** `_names_cte` (`:394`) builds the installed ∪ ever-used union
  per entity kind; `entity_state` (`:562`) turns `(installed, ever_used, calls)` into the `state`
  cell, and it takes the *windowed* call count so a row never reads `in use` beside a `0`.
- **A snapshot is the way back.** `snapshot` (`:657`) copies the database beside itself before
  every destructive step (`--full`, pre-rebuild, `restore`, `vacuum`).

## 3. Query — `cbut_db.py`, query half

The windowed queries take `(start_ts, end_ts)` in epoch milliseconds and filter with `_time_filter`
(`:1797`). Windows are whole local calendar days: `window_bounds` (`:1740`) starts at local 00:00,
and `CustomWindow` (`:1614`) carries an absolute span from the date picker, optionally with its end
pinned to `now`.

The one expression worth naming is the display total. `_USAGE_TOTAL_EXPR` (`:1606`) is
`prompt + completion + cache hit`; because `prompt_tokens` already contains the cache hit, this is
deliberately *not* the provider's total. `q_model_tokens` (`:1477`) returns both: `total_tokens`
(the API total, provider-reported where present) and `usage_total_tokens` (the display figure). The
Tokens tab reads the second; the model-response drill-down also carries the first, labelled
`reported total`, which is why the two screens can quote different numbers for one model.

`overview` (`:2051`) is the status bar's source; `q_usage_summary` (`:1805`) and `q_usage_kpi`
(`:1836`) back the Dashboard and the Usage page.

## 4. Present — `cbut-tui.py` and `cbut-stats.py`

Eight panes, in this order (`TAB_IDS`, `:1544`):

| # | Pane | Title |
|---|---|---|
| 1 | `tab-dashboard` | Dashboard |
| 2 | `tab-tools` | Tools |
| 3 | `tab-skills` | Skills |
| 4 | `tab-agents` | Agents |
| 5 | `tab-plugins` | Plugins |
| 6 | `tab-mcp` | MCP |
| 7 | `tab-tokens` | Tokens |
| 8 | `tab-usage` | Usage |

The six middle rows are declared once in `ENTITY_TABS` (`:384`) — pane id, table id, columns,
query name, row builder, empty text, key — so a new entity kind is one entry, not six edits.
Dashboard and Usage are not in that registry because they draw panels, not a table. This table is
asserted against the code by `test_tui.py`, so adding, renaming or reordering a pane fails the suite
instead of quietly editing this page.

- `on_mount` (`:1767`) builds every table's columns from `WIDE_TABLES` (`:2129`), migrates the
  schema, does one full refresh, then requests one incremental sync.
- `refresh_data` (`:1949`) fills every tab, each with its own window from `_bounds_for` (`:2168`);
  `_refresh_active_tab` (`:2048`) re-queries only the visible pane. Hidden tabs are populated when
  opened, not polled.
- `_fill_entity` (`:1818`) resolves `tab.query` on `db` per call, caches the unfiltered rows, and
  `_render_entity_rows` (`:1838`) applies sort, the used-only default and the text filter before
  `_fill_wide` (`:1123`) keeps the longest column prefix that fits.
- The sync worker (`_request_sync` `:2792`, `_do_sync` `:2801`, `_on_sync_done` `:2813`) runs on
  launch and on `r`, in a thread, and refreshes every tab only when it indexed a file.
- `cbut-stats.py` reads the same `db` functions through a read-only connection (`cbut-stats.py:694`)
  and prints the full union; `cmd_stats` (`cbut-stats.py:118`) is the report a headless run gets.

## Following one number

The Tokens tab, window `7d`, the row for one model:

1. `_bounds_for("tab-tokens")` (`cbut-tui.py:2168`) → `window_bounds("7d", now)` (`cbut_db.py:1740`).
2. `_fill_entity` (`cbut-tui.py:1818`) → `db.q_model_tokens(conn, start_ts, end_ts)` (`cbut_db.py:1477`).
3. That query groups `model_responses` by `(model, provider)` and computes `usage_total_tokens`
   with `_AGG_USAGE_TOTAL_EXPR` (`cbut_db.py:1611`).
4. The rows come from `_record_model_response` (`cbut-sync.py:1099`) reading
   `providerData.rawUsage` out of a `message` record.
5. `_token_row` (`cbut-tui.py:368`) formats the cells; `_fill_wide` (`cbut-tui.py:1123`) decides
   which columns survive the terminal width.

A disagreement between two screens is therefore usually a different window (step 1) or a different
expression (step 3). The one case that is neither: the context-token total comes from
`turn-metrics` (`cbut-sync.py:1340`), not from a model response, and summing it with these is wrong by design.

## When a number looks wrong

| Symptom | First place to look |
|---|---|
| A panel is empty that used to have rows | `cbut health` → `unparsed` reasons (`cbut-stats.py:388`); a new `type:` is a format change |
| Rows exist but a count is `0` | the window (`_bounds_for`) before the query |
| Two screens disagree | API total vs Usage Total (`cbut_db.py:1606`) — they are different measurements |
| A model has two rows | `(model, provider)` keying (`cbut_db.py:1477`); one model served by two routes is two rows |
| Totals doubled after a rebuild | `carried` sessions (`cbut-sync.py:1841`) — the path that must not re-add them |
| Numbers stop moving | the sync worker only runs on launch and on `r` (`cbut-tui.py:2792`) |
