<p align="right"><a href="README.md"><b>English</b></a> | <a href="README.zh-CN.md">简体中文</a></p>

# 🐾 CodeBuddy Usage Tracker (`cbut`)

![CodeBuddy](https://img.shields.io/badge/CodeBuddy-reads%20local%20logs-blue?logo=robotframework&logoColor=white)
![Python](https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/storage-SQLite-003B57?logo=sqlite&logoColor=white)
![Textual](https://img.shields.io/badge/TUI-Textual-ff69b4?logo=terminal&logoColor=white)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Platform: Linux](https://img.shields.io/badge/platform-Linux-lightgrey?logo=linux&logoColor=black)
[![Repository](https://img.shields.io/badge/repository-GitHub-black?logo=github)](https://github.com/SHADE-glitch/codebuddy-usage-tracker)

**What it is** — A passive, read-only tracker for [CodeBuddy Code](https://cnb.cool/codebuddy/codebuddy-code): an eight-tab TUI and headless JSON reports over every built-in tool, skill, agent, plugin and MCP tool you use.

**What it solves** — CodeBuddy keeps its history in local logs shaped for resuming sessions; `cbut` indexes them into one local SQLite file so you can see what you have used and what is installed.

**How to run** — `./install.sh`, then `cbut sync`.

**Where to look** — See [AGENTS.md](AGENTS.md) and [MAINTENANCE.md](MAINTENANCE.md).

> 🔭 See how you **actually** use [CodeBuddy Code](https://cnb.cool/codebuddy/codebuddy-code) —
> every 🧰 built-in tool, 🎯 skill, 🤖 agent, 🧩 plugin and 🔌 MCP tool.

---

## 📖 Table of Contents

- [✨ Features](#-features)
- [🤔 Why this exists](#-why-this-exists)
- [🏗️ How it works](#️-how-it-works)
- [📊 The eight tabs](#-the-eight-tabs)
- [📋 Requirements](#-requirements)
- [🚀 Install](#-install)
- [🧭 Usage](#-usage)
- [⚙️ Configuration](#️-configuration)
- [🔒 Privacy](#-privacy)
- [🧪 Testing](#-testing)
- [📁 Repository layout](#-repository-layout)
- [⚠️ Limitations & roadmap](#️-limitations--roadmap)
- [🧹 Uninstall](#-uninstall)
- [🤝 Contributing](#-contributing)
- [📄 License](#-license)

## ✨ Features

- 🧰 **Eight views, one command** — a dashboard, tools, skills, agents, plugins, MCP, tokens, usage.
- 📜 **Full history, instantly** — backfills every session already on disk.
- 🔑 **Keyed by name — nothing ever disappears** — every tab lists what you have **ever** used *and* what is installed, keyed by name alone: a renamed, updated or removed entity keeps its history, and the same name from two owners is one merged row. The TUI shows the used half by default; <kbd>a</kbd> brings the installed-but-unused rows back, per tab.
- 🪶 **Passive & read-only** — no hook, no plugin, no CodeBuddy source changes.
- 🗃️ **One local SQLite file** — no network, no upload, no telemetry.
- ⚡ **Fast incremental sync** — ~5 s to index ~300 sessions / ~420 MB; only new bytes after that.
- 🖥️ **TUI + headless** — an eight-tab Textual UI for humans, JSON reports for scripts.
- 🔢 **Real model tokens and credits** — per-model input / output / total plus cache hit / miss / write, straight from `providerData.rawUsage`, with a **Provider** column (Built-in, or a third-party label from `providerData.requestModelId`); total prefers the provider's own reported value. The provider's own **`credit`** is stored verbatim beside the tokens and shown on the Dashboard, Tokens, Usage, Top models, the project board and the model drill-downs — never converted into tokens.
- 📊 **Calendar-day windows, per tab** — the top-bar range (Today / 2 days / 3 days / 7 days / 14 days / 30 days / All time) is kept **per tab**, so changing one never moves another: entity tabs default to **7 days**, the Dashboard and the Usage page to **Today**. Plugins has a window too — its list is all-time, its counts are not. Windows are whole local calendar days (00:00 → now), never "now minus N hours". A window only shrinks the **counts** — the list stays full.
- 📅 **Custom date range** — the top bar's 📅 control (or **Custom…** in the range list) opens a picker with shortcut chips, an editable **START TIME** and **END TIME** (a date plus a 24-hour time), and a month calendar: ‹ › step the month and, at the year's ends, the year. Under each card a quick-time row puts the day's edges one click away — the start offers `00:00`/`06:00`/`12:00`/`18:00`/`23:59`, the end adds **now** — and a 24-cell hour grid under the calendar sets the hour of whichever card is active, so a date and its hour are picked the same way. Clicking a card aims both grids, so the next day *or hour* you click lands in that card; ↑/↓ and the wheel nudge the focused field by the step you pick (`1m` / `10m` / `1h`). Dates and times may be typed loosely — `10-1` is `10/01`, `9:5 AM` and `21:05` are both `21:05`. The choice is kept per tab, and the top bar always shows the span it is showing.
- 🔎 **Two-level drill-down** — <kbd>Enter</kbd> or double-click a row on the Tools, Skills, Agents, Plugins, MCP or Tokens tab: the first screen summarises that entity (its counts and totals **in the tab's own window**) above its recent calls; <kbd>Enter</kbd> a call to see **every field of that single call**, laid out to fill the screen.
- 🔐 **Metadata only** — names, timings, statuses, projects. Never message bodies or argument values.

## 🤔 Why this exists

CodeBuddy already **writes** the raw data you need — it just never **shows** it. It keeps:

- 📜 **session transcripts** in `~/.codebuddy/projects/<cwd>/<session>.jsonl`
  (every tool call is a `function_call` record with name, arguments, status, timestamps);
- 🔭 **OpenTelemetry spans** in `~/.codebuddy/traces/<pid>/trace_*.jsonl`
, plus the whole-trace `trace_<id>.json` written when a trace
  completes
  (`function` / `agent` / `generation` spans);

…yet only exposes session-level `/cost` and `/context`. There is **no** cross-session,
per-tool / per-skill / per-plugin breakdown. That gap is what `cbut` fills.

## 🏗️ How it works

```text
   ~/.codebuddy/                         cbut                          ~/.local/share/
 ┌──────────────────┐          ┌─────────────────────┐          ┌────────────────────────┐
 │ projects/        │          │  cbut-sync.py       │          │ codebuddy-usage-       │
 │   **/*.jsonl  ───┼── read ──▶  parse + classify   │          │   tracker/usage.db     │
 │                  │  (only)  │  incremental,       │  write   │                        │
 │ traces/          │          │  idempotent         ├─────────▶│  SQLite  (tools ·      │
 │   **/*.jsonl     │          └─────────────────────┘          │  skills · agents ·     │
 │                  │                    ▲                       │  plugins · mcp)        │
 │ mcp.json         │                    │                       └───────────┬────────────┘
 │ settings.json    │          ┌─────────┴───────────┐                       │
 │ plugins/         │          │  cbut-tui.py (TUI)  │◀────── query ─────────┘
 └──────────────────┘          │  cbut-stats.py (CLI)│
      untouched                └─────────────────────┘
```

`cbut` is a **passive reader**: it parses the logs CodeBuddy already writes and builds an
index. It never touches CodeBuddy's files, installs no hook, and adds no runtime overhead.

## 📊 The eight tabs

The **Dashboard** opens first: an at-a-glance summary of the important numbers, so you
know where to look before drilling in. A shared top bar above the tabs carries the range
selector, and **each tab keeps its own range** — changing one tab's window never moves
another's. The entity tabs (Tools, Skills, Agents, MCP, Tokens) default to **7 days**;
the Dashboard and the Usage page default to **Today**. The **Plugins** tab works the
same way: its list is all-time — installed ∪ ever used — while `uses` and `last used`
count inside its window. A window only changes the **counts**; it never drops a row,
because every tab is keyed by name.

| | Tab | What it shows |
|---|-----|---------------|
| 🧭 | **Dashboard** | three rows for the Dashboard's own window: Usage (requests, credits, sessions, projects, tool/skill/agent/MCP/plugin counts), Tokens (usage total, input/output, credits, cache hit rate), absolute cache hit/miss/write, Top 5 models and Top 5 tools, and the **Projects board** (requests · usage total · credits · sessions · last used). The board grows with its rows and takes the height the rows above leave, so the dashboard ends at the status line; the pane scrolls once a long project list no longer fits |
| 🧰 | **Tools** | tool calls by name, completion, average duration, last used |
| 🎯 | **Skills** | skill invocations by name (merged across owners), last use |
| 🤖 | **Agents** | subagent types by name, call count, last use |
| 🧩 | **Plugins** | every plugin — installed **and** ever used — with its skills/agents/commands; all-time list, windowed counts, no version column |
| 🔌 | **MCP** | MCP servers and tools, invocation counts |
| 🔢 | **Tokens** | per-model **usage total · credits**, requests, input, output, cache hit/miss/write, cache hit rate and last used — one row per (model, provider) that answered in the tab's window |
| 📊 | **Usage** | request log with summary panels: window/requests/sessions/projects, model tokens **+ credits**, cache breakdown, runtime |

Press <kbd>Enter</kbd> (or double-click) on any row for a **two-level drill-down**.
The first screen summarises the entity — its counts and totals **in that tab's own
window** — above its recent calls; press <kbd>Enter</kbd> on a call to open **every
field of that single call** (timestamp, project, session, status, duration, ids, and
the token/cache detail for a model response). It covers every entity tab except
Dashboard and Usage — tools, skills, agents, plugins, MCP tools and models. Every tab
shows an explicit empty-state message when it has no data yet, instead of a blank
region.

**Every tab lists the union of "ever used" and "installed", keyed by name, sorted by call
count.** A deleted skill, an uninstalled plugin or a renamed agent keeps its row and its
count; a name that appears under two plugins is merged into one. **Plugins** counts every
invocation attributed to a plugin — its skills, agents and slash commands — so a plugin
whose skill was used no longer reads `uses = 0`. Attribution is evidence-based: a name is
credited to a plugin only when the plugin inventory maps that name to it.

The TUI shows that union **used-only by default** — a row with 0 calls in the tab's
window is hidden, and <kbd>a</kbd> toggles the installed-but-unused half back in,
per tab (Tokens is usage-only and has no toggle). <kbd>/</kbd> opens a filter that
narrows as you type and matches the rendered values (a name, a category or a state).
While a filter is applied its bar stays visible and the summary line names the match
count and the active sort; each edit starts the list at its first match, so the page
never scrolls away from it. <kbd>Enter</kbd> keeps the filter and hands focus back to
the table, <kbd>Esc</kbd> clears it. The Usage request log shows the newest 100 rows
and says so when the window holds more; there is no page key.

The tab bar and footer are the key surface: <kbd>q</kbd> quits, <kbd>r</kbd> syncs and
reloads, <kbd>s</kbd> cycles the active tab's sort, <kbd>a</kbd> toggles the full list,
<kbd>/</kbd> filters, <kbd>d</kbd> opens the date picker, <kbd>tab</kbd>/<kbd>shift+tab</kbd>
switch tabs, <kbd>Enter</kbd> drills in, and <kbd>Esc</kbd>/<kbd>q</kbd> goes back from a
detail screen. The TUI syncs once when it opens and then stays still — no background
refresh timer; new rows arrive on <kbd>r</kbd>.

The last column of every entity tab is **state** — the one thing that separates "still
here" from "never left": `in use` (installed, and called **inside this tab's window**),
`installed` (on disk right now, no call in this window), `removed` (gone from disk, kept
for its history, drawn dim). The label follows the same window as the row's `calls`
cell, so a 0-call row never says `in use`; the all-time history stays in the counts and
in `ever used`, not in this label. The installed side is a **snapshot rebuilt from disk
on every sync**, so uninstalling something drops it from the installed column on the
next sync without touching the counts it already earned. On a narrow terminal the plan
drops `last used` or `avg` before it drops this column, and never drops the name.

**Tokens records what answered, not what a catalogue offers.** One row per
(model, provider) with at least one response. The installed package's `product.json`
turns over with every upgrade and an account's region, and a `~/.codebuddy/models.json`
entry is a route id (`ark-code-latest`) that answers as a different model
(`deepseek-v4-1-flash`) — listing either produced zero-request rows naming something
nobody had called. History is the durable side: a model that leaves the catalogue
(`glm-5.1`) keeps every row and total its own responses earned. A response whose
`requestModelId` is absent reads as `Built-in`, so one model never splits into
`Built-in` and `-` rows.

**Credits are billing, not tokens.** When a response carries `providerData.rawUsage.credit`, the
value is stored verbatim in its own column and shown beside the token totals on every surface that
can carry it. It is never added into a token count, and no token number is ever derived from it. A
route that reports no credit leaves `-` (NULL), never a fabricated `0`; an aggregate reads `-` when
none of its contributing rows carried the field. An older index upgrades in place (schema v13):
the next `cbut sync` re-reads the transcripts and backfills the credit column from responses it
already knows.

> **One token total: Usage Total.** Every surface that shows a token number without
> naming a part shows **Usage Total** — a display-only `Input + Output + cache hit`
> that **re-adds the cache hit** (already inside Input), so it lands at ~2× the
> provider's own total. That provider total (`providerTotalTokens`, what an
> earlier release called *API Total*) is no longer printed anywhere: two unlabelled
> totals on one screen invited the reader to pick the wrong one. Cache hit/miss/write
> are still reported **separately** and are never folded into a total. Fields absent
> from a response are shown as `-`, never as a fabricated `0`.

**A malformed name cannot break a row.** A tool name that is empty, over 200
characters, or contains whitespace or a control character is counted as
`invalid_tool_name` and is not stored as a call. Values that predate that rule are
flattened for display in both the TUI and the headless reports, so a newline or escape
sequence from a broken model call cannot split a table row or move the cursor.

**A malformed record cannot break the run.** A line that parses as JSON but is not a
record (`null`, a bare list, a string) — or a record holding a list or object where a
scalar belongs — is counted as `unusable_record` and skipped. One unreadable record
costs that record, visible in `cbut health`, instead of stopping the index for every
project.

### 📊 The Usage page

The Usage tab mirrors the style of a session-usage panel, but every number comes from
the transcripts on disk:

- **The Usage page keeps its own range** (default **Today**), shown in the shared top
  bar while the Usage tab is active; every other tab keeps its own too, so moving one
  never moves another. Options are Today / 2 days / 3 days / 7 days / 14 days / 30 days / All time —
  **whole local calendar days** ending at the current time (00:00 → now), **not**
  "now minus N hours" and **not** from when the tracker first ran. Pick **Custom…** (or
  the 📅 control) to choose any start/end down to the minute. Its **Window /
  Requests** panel shows the dates of the active range, plus the window's sessions and
  projects.
- **One Request Logs list**, newest first. The columns follow the family layout —
  time · model · provider · usage total · **credits** · input · output ·
  cache hit (read) · cache miss · cache write · cache hit rate. cc-switch's cost,
  duration and HTTP-status columns have no transcript source, so they are omitted rather
  than estimated; the provider and the credit are real transcript fields. The priority
  columns sit leftmost, and on a narrow terminal a column plan drops the tail instead of
  cutting it mid-label — the line above the table then names what is hidden and how wide
  the terminal has to be to show all of it.
- **Same accuracy rules as Tokens** — the token column is **Usage Total**
  (`Input + Output + cache hit`), the same number the Tokens tab and the dashboard
  print, so a row here and a row there never disagree. Cache hit/miss/write are never
  folded into it, and missing values stay `-`.
- **Cache hit rate** = `cache hit / (cache hit + cache miss + cache write)` — the
  cacheable input, matching cc-switch. It shows `-` when a row has no cache data.
- **The summary panels are height-capped and scroll internally**, so the Request Logs
  table below them stays visible even on a small terminal. Measured at 80×24: the panels
  go two-up and stay capped, the table keeps 9 rows (a test holds the floor at 6), and the
  column plan keeps a **prefix of its 12** columns with a note above the table naming the
  rest. Which prefix depends on the data — the widths are measured from the rows, not
  assumed — so the row count is a reading from this machine's own database at 80×24, and
  yours will differ. Nothing is cut mid-label.
- **The plan covers every table, not the two that were complained about** — the eight tables
  that live in the eight tabs (the Dashboard's Projects board included) and both detail screens
  (a row's history, a model's responses): ten in all, counted out of the source rather than
  estimated, and a test fails if a table ever appears without a plan. A name long enough to push
  a column off the screen therefore costs columns, not legibility. The name/key column(s) and the
  entity `state` column are protected: a narrow terminal drops `last used` or `avg` first, so a
  row never loses the answer to "is this still installed". Two columns is the floor: when even a
  name and its first number do not fit an 80-cell terminal, the note says `even these are cut`
  rather than implying the table fits.
- **A cell that is far longer than it earns gets capped, at 28 characters by default** — the
  number is a setting (`name_cap`), and the cap is measured on this machine's data, where every
  entity name fits (tool 22, model 24, agent 20, plugin 19, skill 28)
  and the one column that runs past it is the project path (162 of 482 rows over 24, longest 74).
  A path is capped from the **left** (`…/work/backend-service`) because its beginning is
  the prefix every row shares and its end is what distinguishes them; anything else is capped from
  the right, keeping the readable head. Nothing is silently shortened: the note prints the number in
  force (`cells capped at 28`), and the cap only engages when a column would otherwise be dropped —
  a wide terminal shows the whole value. Two rows whose names share the visible prefix still open their
  own detail screen, because a row is keyed by the uncapped value. On this machine the cap buys
  back three columns on the History screen at 80×24 (2 of 5 became 5 of 5) and changes nothing else.

## 📋 Requirements

| | |
|---|---|
| 🐧 OS | Linux — developed and verified on Ubuntu; other distributions **unverified** |
| 🐾 CodeBuddy | CodeBuddy Code — the schema is verified against **the logs on your machine**, not against a version number: no transcript line carries one, and the CLI here is launched through a shell alias, so its version is not machine-confirmable. See [`docs/maintenance/compatibility.md`](docs/maintenance/compatibility.md#codebuddy) |
| 🐍 Python | 3.11+ — the full suite run green on **3.11, 3.12, 3.13 and 3.14** (CI runs 3.11 and 3.14). Headless commands need only the stdlib |
| 🖥️ `textual` | only for the interactive TUI (installed by `install.sh`) |

## 🚀 Install

```bash
git clone https://github.com/SHADE-glitch/codebuddy-usage-tracker.git
cd codebuddy-usage-tracker
./install.sh          # creates .venv with textual, links ~/.local/bin/cbut
cbut sync             # build the index from your existing CodeBuddy logs
cbut                  # launch the TUI
```

Prefer no venv? The headless reports work with plain `python3`:

```bash
python3 scripts/cbut-sync.py
python3 scripts/cbut-stats.py stats
```

### 🔄 Optional: keep the index fresh without opening the TUI

Two user units ship in [`systemd/`](systemd). Installing them runs
`cbut sync --quiet` every 15 minutes — these are the same four lines `install.sh` prints at the
end of a successful install:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/cbut-sync.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cbut-sync.timer
```

`Persistent=true` catches up a run missed while the machine was off, and the
service runs at `Nice=10` — so it never competes with your session for CPU. Check it with `systemctl --user status cbut-sync.timer`.

## 🧭 Usage

```bash
cbut                     # 🖥️  interactive TUI (eight tabs)
cbut sync                # 🔄  incremental index of new log data
cbut sync --full         # ♻️  rebuild from the transcripts still on disk (see below)
cbut stats               # 📊  overview of all categories
cbut tools|skills|agents|plugins|mcp
cbut models              # 🔢  per-model tokens: in/out/total + cache hit/miss/write
cbut show tool Bash      # 🔍  recent calls for one entity
cbut sessions            # 🗂  one row per session, newest first
cbut show model <name>   # 🔍  recent responses for one model
cbut recent              # 🕒  latest tool calls
cbut inventory           # 📦  what is installed, used vs unused
cbut export              # 💾  dump every table as JSON
cbut health              # 🩺  database + data-source check
cbut backup              # 💾  snapshot before a risky step (sync --full does this)
cbut restore             # ⏪  list snapshots; `cbut restore NAME` rolls one back
cbut vacuum              # 🧹  compact the file, reporting what came back (snapshot first)
cbut format              # 🧾  the CodeBuddy fields and paths this tool depends on
```

`cbut sync --full` rebuilds the index from the transcripts that are still in `~/.codebuddy`,
and nothing else. CodeBuddy prunes old transcripts, and a pruned one cannot be re-read, so the
sessions, calls and responses that lived only in it are gone from the rebuilt database. It
snapshots first (`cbut backup` does the same, and `cbut restore` lists what is there), and an
incremental `cbut sync` never drops what it has already indexed — it keeps a session whose
transcripts have all been pruned, and only a transcript rewritten in place makes it rebuild.

| Variable | Default | Purpose |
|---|---|---|
| `CBUT_DB` | `~/.local/share/codebuddy-usage-tracker/usage.db` | database location |
| `CBUT_CODEBUDDY_DIR` | `~/.codebuddy` | source log directory |
| `CBUT_PRODUCT_JSON` | located from the `codebuddy` executable on `PATH` | the installed package's tool/agent catalogue. Unset, `cbut` walks `PATH` for `codebuddy` and reads `product.json` above it; with no CodeBuddy installed it falls back to a checked-in list and says so in `cbut health`. Pin it to test a catalogue, or to read one the `PATH` cannot find. |

## ⚙️ Configuration

Every knob has the value the app already shipped with, and the file is optional: `cbut` behaves
exactly as documented until you add one.

`~/.config/cbut/config.toml` — or point `CBUT_CONFIG` somewhere else:

```toml
top_n = 5            # rows in the Dashboard's Top models / Top tools
log_limit = 100      # request rows on the Usage page
detail_limit = 200   # rows on a history / model-response screen
name_cap = 28        # widest cell a table renders before it cuts the value
```

- **Precedence is environment > file > default.** Each key also reads from `CBUT_TOP_N`,
  `CBUT_LOG_LIMIT`, `CBUT_DETAIL_LIMIT` and `CBUT_NAME_CAP`.
- **A broken file stops the TUI** with the path and the reason rather than quietly starting on
  defaults — being ignored while you edit it is the worst outcome a settings layer can offer.
  A path that exists but is not a regular file (a directory, a FIFO, one you cannot even stat) is
  refused the same way; only a *missing* file means defaults. `cbut health` prints the same
  verdict, including which file it read.
- **Unknown keys are refused**: a typo'd setting would otherwise do nothing, silently, forever.
  Values are type- and range-checked (`top_n = 0` is a blank panel, not a preference).
- Parsed by the standard library's `tomllib`, so this adds **no dependency** and no network path.
  Nothing ever writes this file, and no setting is stored in the database.

## 🔒 Privacy

`cbut` reads only the **structured metadata** fields of CodeBuddy's own logs and stores
counts, timings, statuses and identifiers. It does **not** read or store prompt/response
text, tool argument values, or file contents. 🔒 Nothing leaves the machine.

## 🧪 Testing

```bash
python3 -m unittest discover -s scripts/tests     # Ran 645 tests ... OK
```

The suites are plain `unittest` (stdlib only, so `pytest` discovers them too). Each one
builds its own throwaway database in a temp directory — **your real `usage.db` and your
`~/.codebuddy` logs are never opened**, and no code path in this repository reaches the network
(`test_privacy.py` holds the import allowlist that keeps it that way).

| Suite | Tests | Covers |
|---|---:|---|
| `test_sync.py` | 155 | transcript parsing, tool classification, the route-origin column (`requestModelId` → built-in / vendor / host / endpoint), incremental vs `--full` re-sync, crash recovery, unparsed-record accounting, the whole-run sync lock, the
  OTel agent-span trace reader (stream and final files merge on the spanId key;
  session link, status upgrades) |
| `test_traces.py` | 12 | the trace reader end to end: span
  outcomes stored once, `cli`/`ptc` declined, a
  running placeholder upgraded and never downgraded on replay, the final file's
  `trace.sessionId` linking what the stream could not, re-reads
  idempotent, the full `_run` integration |
| `test_hostile_inputs.py` | 4 | generated hostile input to both readers: every field of a real record shape replaced by each bad value, deleted, or the record replaced whole, one level inside its structures too — no shape may end the run, and a record that cannot be read is counted as `unusable_record` instead of dropped |
| `test_tui.py` | 145 | tab wiring and report shapes through Textual's own `run_test` harness, the per-tab date picker (calendar month/year stepping, the active-card target, preset and quick-time chips that fit their column, the 24-hour clock the fields read and write, the hour grid, the nudge-step selector, the top-bar span surviving a tab switch), the two-level drill-down (the windowed summary header, Enter opening a call's full detail, a long value folding instead of clipping, the plugin path), narrow-terminal layout and the column plan that replaces a clipped tail on **every** table, the cell cap and the setting behind it, status-line truthfulness, the used-only default with the per-tab `a` toggle, the `/` name filter (Enter applies, Esc cancels),
  the Usage log's newest-100 cap with no page key, provider suffixes on shared model names,
  the Dashboard's Projects board and the credit cells that read `-` when a route reported none |
| `test_cross_repo_golden.py` | 17 | the cross-repo golden fixture: one fixed input set (NULL / partial / cache-missing token rows, model spellings, local-midnight and cross-hour boundaries, both 2026 US DST days) run through this repo's own Usage Total expression, window aggregates, model rollup, typed date entry and calendar-day windows, pinned to the canonical file shared with ocut and qcut |
| `test_usage.py` | 121 | the calendar-day windows (Today / 2 / 3 / 7 / 14 / 30 days / all) and their custom-range labels, the typed date/time normalisation (24-hour display, 12-hour still accepted on input), the token totals and provider grouping, the Dashboard queries, the drill-down queries (windowed per-entity summaries, plugin use counts, a single call's full row), the schema gate, and which indexes earn their keep, the headless session list (window,
  newest-first order, limit) |
| `test_indexes.py` | 4 | the rule the two earlier index cuts relied on a hand-taken reading for: every statement the report surface really runs is captured and planned with `EXPLAIN QUERY PLAN`, and a declared index no plan names fails — plus the structural rule that caught the v9 cut, a key that is a strict prefix of a wider declared key |
| `test_stats.py` | 45 | the headless report CLI (`cbut stats`): the overview and the per-category tables, the model/token report and the cache columns it drops on a narrow terminal, `show`/`recent`/`inventory`/`export`, `health` (schema version, settings line, unparsed accounting), the `backup`→`restore` round trip, `vacuum` (snapshot first, reclaimed bytes, rows unchanged), the generated `format` surface, and the empty-state `(none)` notice |
| `test_config.py` | 30 | the settings layer: defaults unchanged, file, env precedence and bounds, a broken file that stops `main()` before the TUI starts, the keys the app actually reads, and every key `cbut health` reports — with the environment hidden, so a configured shell cannot turn these into false failures |
| `test_dispatcher.py` | 12 | `bin/cbut`: subcommand routing, venv resolution, help without a resolvable install |
| `test_format_registry.py` | 10 | the CodeBuddy format registry and the parser agree, both directions |
| `test_privacy.py` | 11 | import allowlist **plus the calls that bypass it** (`__import__`/`eval`/`import_module`), no free-text value in any column, CodeBuddy's own files untouched |
| `test_hermetic.py` | 13 | a runtime tripwire: the suite never opens the real `usage.db` (no in-process `sqlite3.connect`, no child that would resolve it without `CBUT_DB` or `--db`), and never reads the real host footprint (`~/.codebuddy` — no `open`, directory walk or child without `CBUT_CODEBUDDY_DIR`) |
| `test_snapshots.py` | 9 | automatic snapshots before destructive steps, listing, restore, pruning |
| `test_maintenance_docs.py` | 30 | the generated format doc cannot drift from the code, no document may state a CI version the workflow does not run, a timer the TUI never sets, a schema version or table/view count the code does not build, or a trace tree it does not read, every `cbut health` reason is named on the page that lists them, and every `file:line` citation in `pipeline.md` still lands on the symbol it names |
| `test_readme_counts.py` | 7 | the suite table above is the suite table the runner would print, every total repeated in prose is the same number, and both pages document every setting the loader knows |
| `test_record_coverage.py` | 6 | every covered commit is recorded in `CHANGELOG.md` |
| `test_readme_bilingual.py` | 5 | the two READMEs stay one document in two languages, and every table-of-contents link has a heading to land on |
| `test_conventions.py` | 9 | the dimension-A root layer is present at the root, AGENTS.md names its dimension in one line instead of quoting the standard's body, and no runtime state the ignore list covers is tracked |

`test_readme_counts.py` is what keeps this table honest: a suite added without a row, or a row left
at an old number, fails the suite.

## 📁 Repository layout

| Path | What it is |
|---|---|
| `bin/cbut` | bash entry point. Bash on purpose: it must still print a useful error when the TUI venv is missing, so it cannot depend on Python. Prefers `./.venv/bin/python`, falls back to `python3` |
| `install.sh` | creates the venv (`uv` when available) and symlinks `~/.local/bin/cbut`. Idempotent; refuses to replace a non-symlink unless `--force` |
| `scripts/cbut_db.py` | SQLite schema and the query helpers every entry point shares |
| `scripts/cbut-sync.py` | log parser and incremental indexer (`--full`, `--quiet`) |
| `scripts/cbut-stats.py` | headless reports: `stats` · `tools` · `skills` · `agents` · `plugins` · `mcp` · `models` · `sessions` · `show` · `recent` · `inventory` · `export` · `health` · `backup` · `restore` · `vacuum` · `format` |
| `scripts/cbut-tui.py` | the eight-tab Textual UI |
| `scripts/tests/` | the 645 tests above |
| `docs/maintenance/` | what to re-check when CodeBuddy changes: the generated format-dependency surface, the version/compatibility matrix, the size & performance baseline with the commands that produced it, and the `file:line` map from a log record to the number on screen |
| `systemd/` | optional 15-minute sync service + timer |
| `requirements.txt` | `textual>=8.2,<9` — TUI only; everything else is stdlib |

Both paths are overridable for a sandboxed install: `CBUT_SCRIPTS` and `CBUT_VENV` for the
launcher, `CBUT_DB` and `CBUT_CODEBUDDY_DIR` for the data (see [Usage](#-usage)).

## ⚠️ Limitations & roadmap

- 🔌 **MCP usage is sparse** until you actually invoke an MCP tool; the tab is populated from
  real calls (`mcp__<server>__<tool>`), including calls wrapped by the deferred-tool
  mechanism (`DeferExecuteTool`).
- 🤖 **Internal agents** (`autoModeClassifier`, `summaryGenerator`, …) come from the OTel
  traces under `~/.codebuddy/traces/` — the per-process span streams and the whole-trace file
  written when a trace ends — and are recorded with `source='span'`.
- 🏷️ **Provider is read, not guessed.** The Usage and Tokens tabs carry a Provider column:
  `Built-in`, or a third-party label derived from `providerData.requestModelId` — the vendor name
  of a known endpoint url, else that host, else the endpoint tag. It is never inferred from the
  model name, which a custom endpoint's alias can make look built-in.
- 🔢 **`context tokens` vs model tokens.** The headless `cbut stats` prints a session-level
  `context tokens` figure (turn-metrics `tokenDelta`) next to the per-model token totals;
  the two are deliberately kept apart. The `context tokens` sum is idempotent — repeated
  syncs and schema migrations no longer inflate it.
- 🔄 Sync is manual (or via the optional systemd timer); there is no live hook.

## 🧹 Uninstall

`cbut` touches exactly three places outside the repository, and removing them leaves
nothing behind:

```bash
systemctl --user disable --now cbut-sync.timer 2>/dev/null          # only if you installed it
rm -f ~/.config/systemd/user/cbut-sync.service ~/.config/systemd/user/cbut-sync.timer
systemctl --user daemon-reload
rm -f ~/.local/bin/cbut                                            # the symlink install.sh made
rm -rf ~/.local/share/codebuddy-usage-tracker                      # the SQLite index
```

Deleting the repository directory (with its `.venv`) finishes it. CodeBuddy's own files
were only ever read, so there is nothing to restore there. 🗑️

## 🤝 Contributing

This is a personal tool, not a project with a roadmap and a review queue. Bug reports and patches
are still welcome: open an issue with the output of `cbut health` and the version you run. Read
[AGENTS.md](AGENTS.md) before sending code — the invariants there (read-only access to CodeBuddy's
files, no second parser, the eight tabs and their keys) are what a patch has to keep.

## 📄 License

MIT — see [LICENSE](LICENSE). © 2026 SHADE-glitch
