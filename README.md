<p align="right"><a href="README.md"><b>English</b></a> | <a href="README.zh-CN.md">简体中文</a></p>

# 🐾 CodeBuddy Usage Tracker (`cbut`)

![CodeBuddy](https://img.shields.io/badge/CodeBuddy-reads%20local%20logs-blue?logo=robotframework&logoColor=white)
![Python](https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/storage-SQLite-003B57?logo=sqlite&logoColor=white)
![Textual](https://img.shields.io/badge/TUI-Textual-ff69b4?logo=terminal&logoColor=white)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Platform: Linux](https://img.shields.io/badge/platform-Linux-lightgrey?logo=linux&logoColor=black)
[![Repository](https://img.shields.io/badge/repository-GitHub-black?logo=github)](https://github.com/SHADE-glitch/codebuddy-usage-tracker)

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
- [🙏 Attribution](#-attribution)
- [📄 License](#-license)

## ✨ Features

- 🧰 **Eight views, one command** — a dashboard, tools, skills, agents, plugins, MCP, tokens, usage.
- 📜 **Full history, instantly** — backfills every session already on disk.
- 🔑 **Keyed by name — nothing ever disappears** — every tab lists what you have **ever** used *and* what is installed, keyed by name alone: a renamed, updated or removed entity keeps its history, and the same name from two owners is one merged row.
- 🪶 **Passive & read-only** — no hook, no plugin, no CodeBuddy source changes.
- 🗃️ **One local SQLite file** — no network, no upload, no telemetry.
- ⚡ **Fast incremental sync** — ~5 s to index ~300 sessions / ~420 MB; only new bytes after that.
- 🖥️ **TUI + headless** — an eight-tab Textual UI for humans, JSON reports for scripts.
- 🔢 **Real model tokens** — per-model input / output / total plus cache hit / miss / write, straight from `providerData.rawUsage`; total prefers the provider's own reported value.
- 📊 **Calendar-day windows, per tab** — the top-bar range (Today / 2 days / 3 days / 7 days / 30 days / All time) is kept **per tab**, so changing one never moves another: entity tabs default to **7 days**, the Dashboard and the Usage page to **Today**, and Plugins is always all-time. Windows are whole local calendar days (00:00 → now), never "now minus N hours". A window only shrinks the **counts** — the list stays full.
- 🔐 **Metadata only** — names, timings, statuses, projects. Never message bodies or argument values.

## 🤔 Why this exists

CodeBuddy already **writes** the raw data you need — it just never **shows** it. It keeps:

- 📜 **session transcripts** in `~/.codebuddy/projects/<cwd>/<session>.jsonl`
  (every tool call is a `function_call` record with name, arguments, status, timestamps);
- 🔭 **OpenTelemetry spans** in `~/.codebuddy/traces/<pid>/trace_*.jsonl`
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
the Dashboard and the Usage page default to **Today**. The **Plugins** tab has no window
at all: its list is all-time, so the selector is hidden there. A window only changes the
**counts**; it never drops a row, because every tab is keyed by name.

| | Tab | What it shows |
|---|-----|---------------|
| 🧭 | **Dashboard** | a grid of panels, all for the Dashboard's own window: usage KPIs (tool/skill/agent/MCP/plugin counts), a token summary (requests, input/output, API total, usage total, cache hit rate), absolute cache hit/miss/write, runtime health (completed/incomplete, average tool ms, distinct sessions/projects), Top 5 models and Top 5 tools, and a per-day call sparkline |
| 🧰 | **Tools** | tool calls by name, completion, average duration, last used |
| 🎯 | **Skills** | skill invocations by name (merged across owners), last use |
| 🤖 | **Agents** | subagent types by name, call count, last use |
| 🧩 | **Plugins** | every plugin — installed **and** ever used — with its skills/agents/commands; all-time, no version column |
| 🔌 | **MCP** | MCP servers and tools, invocation counts |
| 🔢 | **Tokens** | per-model **usage total**, requests, input, output, **API total**, cache hit/miss/write |
| 📊 | **Usage** | request log with summary panels (cc-switch-style columns) |

Press <kbd>Enter</kbd> on any row for a per-entity history — recent calls for a tool,
skill, agent or MCP tool, and the per-response token detail for a model on the Tokens
tab. Every tab shows an explicit empty-state message when it has no data yet, instead
of a blank region.

**Every tab lists the union of "ever used" and "installed", keyed by name, sorted by call
count.** A deleted skill, an uninstalled plugin or a renamed agent keeps its row and its
count; a name that appears under two plugins is merged into one. **Plugins** counts every
invocation attributed to a plugin — its skills, agents and slash commands — so a plugin
whose skill was used no longer reads `uses = 0`. Attribution is evidence-based: a name is
credited to a plugin only when the plugin inventory maps that name to it.

> **API Total** = the provider's reported total when present, otherwise Input + Output.
> **Usage Total** is a display-only metric (`Input + Output + cache hit`) that
> **re-adds cache hit** — which is already inside Input — so it lands at ~2× the API
> Total. Never read it as the API total. Cache hit/miss/write are reported
> **separately** and are never added into the API total. Fields absent from a response
> are shown as `-`, never as a fabricated `0`.

### 📊 The Usage page

The Usage tab mirrors the style of a session-usage panel, but every number comes from
the transcripts on disk:

- **The Usage page keeps its own range** (default **Today**), shown in the shared top
  bar while the Usage tab is active; every other tab keeps its own too, so moving one
  never moves another. Options are Today / 2 days / 3 days / 7 days / 30 days / All time —
  **whole local calendar days** ending at the current time (00:00 → now), **not**
  "now minus N hours" and **not** from when the tracker first ran. Its **Window /
  Requests** panel shows the dates of the active range.
- **One Request Logs list**, newest first. The columns follow cc-switch's request
  records — time · model · usage total · input · output · API total · cache hit (read) ·
  cache miss · cache write · cache hit rate. cc-switch's provider, cost, duration and
  HTTP-status columns have no transcript source, so they are omitted rather than
  estimated. The priority columns sit leftmost, and on a narrow terminal a column plan
  drops the tail instead of cutting it mid-label — the line above the table then names
  what is hidden and how wide the terminal has to be to show all of it.
- **Same accuracy rules as Tokens** — **API Total** = provider total when present, else
  `Input + Output`. A separate **Usage Total** (`Input + Output + cache hit`) is shown for
  reference, explicitly labelled as re-adding cache hit (~2× the API total). Cache
  hit/miss/write are never folded into the API total, and missing values stay `-`.
- **Cache hit rate** = `cache hit / (cache hit + cache miss + cache write)` — the
  cacheable input, matching cc-switch. It shows `-` when a row has no cache data.
- **The summary panels are height-capped and scroll internally**, so the Request Logs
  table below them stays visible even on a small terminal. Measured at 80×24: the panels
  go two-up and stay capped, the table keeps 9 rows (a test holds the floor at 6), and the
  column plan shows the first **5 of its 10** columns with a note above the table naming the
  other five. Which five depends on the data — the widths are measured from the rows, not
  assumed — so both counts here are readings from this machine's own database at 80×24, and
  yours will differ. Nothing is cut mid-label.
- **The plan covers every table, not the two that were complained about** — the seven tables
  that live in the eight tabs (Dashboard draws panels, not a table) and both detail screens
  (a row's history, a model's responses): nine in all, counted out of the source rather than
  estimated, and a test fails if a table ever appears without a plan. A name long enough to push
  a column off the screen therefore costs columns, not legibility. Two columns is the floor: when
  even a name and its first number do not fit an 80-cell terminal, the note says
  `even these are cut` rather than implying the table fits.
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
`cbut sync --quiet` once a day — these are the same four lines `install.sh` prints at the
end of a successful install:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/cbut-sync.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cbut-sync.timer
```

`Persistent=true` catches up a run missed while the machine was off, the wake-up is
jittered by up to 15 minutes, and the service runs at `Nice=10` — so it never competes
with your session for CPU. Check it with `systemctl --user status cbut-sync.timer`.

## 🧭 Usage

```bash
cbut                     # 🖥️  interactive TUI (eight tabs)
cbut sync                # 🔄  incremental index of new log data
cbut sync --full         # ♻️  rebuild the database from scratch
cbut stats               # 📊  overview of all categories
cbut tools|skills|agents|plugins|mcp
cbut models              # 🔢  per-model tokens: in/out/total + cache hit/miss/write
cbut show tool Bash      # 🔍  recent calls for one entity
cbut recent              # 🕒  latest tool calls
cbut inventory           # 📦  what is installed, used vs unused
cbut export              # 💾  dump every table as JSON
cbut health              # 🩺  database + data-source check
cbut backup              # 💾  snapshot before a risky step (sync --full does this)
cbut restore             # ⏪  list snapshots; `cbut restore NAME` rolls one back
cbut format              # 🧾  the CodeBuddy fields and paths this tool depends on
```

| Variable | Default | Purpose |
|---|---|---|
| `CBUT_DB` | `~/.local/share/codebuddy-usage-tracker/usage.db` | database location |
| `CBUT_CODEBUDDY_DIR` | `~/.codebuddy` | source log directory |

## ⚙️ Configuration

Every knob has the value the app already shipped with, and the file is optional: `cbut` behaves
exactly as documented until you add one.

`~/.config/cbut/config.toml` — or point `CBUT_CONFIG` somewhere else:

```toml
top_n = 5            # rows in the Dashboard's Top models / Top tools
log_limit = 100      # request rows on the Usage page
detail_limit = 200   # rows on a history / model-response screen
name_cap = 28        # widest cell a table renders before it cuts the value
refresh_secs = 5     # TUI redraw timer
sync_secs = 30       # TUI auto-sync timer
```

- **Precedence is environment > file > default.** Each key also reads from `CBUT_TOP_N`,
  `CBUT_LOG_LIMIT`, `CBUT_DETAIL_LIMIT`, `CBUT_NAME_CAP`, `CBUT_REFRESH_SECS` and `CBUT_SYNC_SECS`.
- **A broken file stops the TUI** with the path and the reason rather than quietly starting on
  defaults — being ignored while you edit it is the worst outcome a settings layer can offer.
  `cbut health` prints the same verdict, including which file it read.
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
python3 -m unittest discover -s scripts/tests     # Ran 326 tests ... OK
```

The suites are plain `unittest` (stdlib only, so `pytest` discovers them too). Each one
builds its own throwaway database in a temp directory — **your real `usage.db` and your
`~/.codebuddy` logs are never opened**, and no code path in this repository reaches the network
(`test_privacy.py` holds the import allowlist that keeps it that way).

| Suite | Tests | Covers |
|---|---:|---|
| `test_sync.py` | 82 | transcript parsing, tool classification, incremental vs `--full` re-sync, crash recovery, unparsed-record accounting |
| `test_tui.py` | 86 | tab wiring and report shapes through Textual's own `run_test` harness, narrow-terminal layout and the column plan that replaces a clipped tail on **every** table, the cell cap and the setting behind it, status-line truthfulness |
| `test_usage.py` | 61 | the calendar-day windows (Today / 2 / 3 / 7 / 30 days / all), the token totals, the Dashboard queries, the schema gate, and which indexes earn their keep |
| `test_config.py` | 21 | the settings layer: defaults unchanged, file, env precedence and bounds, a broken file that stops `main()` before the TUI starts, the keys the app actually reads, and every key `cbut health` reports — with the environment hidden, so a configured shell cannot turn these into false failures |
| `test_dispatcher.py` | 12 | `bin/cbut`: subcommand routing, venv resolution, help without a resolvable install |
| `test_format_registry.py` | 10 | the CodeBuddy format registry and the parser agree, both directions |
| `test_privacy.py` | 11 | import allowlist **plus the calls that bypass it** (`__import__`/`eval`/`import_module`), no free-text value in any column, CodeBuddy's own files untouched |
| `test_hermetic.py` | 10 | a runtime tripwire: the suite never opens the real `usage.db` — no in-process `sqlite3.connect`, and no child process that would resolve it without `CBUT_DB` or `--db` |
| `test_snapshots.py` | 9 | automatic snapshots before destructive steps, listing, restore, pruning |
| `test_maintenance_docs.py` | 9 | the generated format doc cannot drift from the code |
| `test_readme_counts.py` | 5 | the suite table above is the suite table the runner would print, and both pages document every setting the loader knows |
| `test_record_coverage.py` | 6 | every covered commit is recorded in `CHANGELOG.md` |
| `test_readme_bilingual.py` | 4 | the two READMEs stay one document in two languages, and every table-of-contents link has a heading to land on |

`test_readme_counts.py` is what keeps this table honest: a suite added without a row, or a row left
at an old number, fails the suite.

## 📁 Repository layout

| Path | What it is |
|---|---|
| `bin/cbut` | bash entry point. Bash on purpose: it must still print a useful error when the TUI venv is missing, so it cannot depend on Python. Prefers `./.venv/bin/python`, falls back to `python3` |
| `install.sh` | creates the venv (`uv` when available) and symlinks `~/.local/bin/cbut`. Idempotent; refuses to replace a non-symlink unless `--force` |
| `scripts/cbut_db.py` | SQLite schema and the query helpers every entry point shares |
| `scripts/cbut-sync.py` | log parser and incremental indexer (`--full`, `--quiet`) |
| `scripts/cbut-stats.py` | headless reports: `stats` · `tools` · `skills` · `agents` · `plugins` · `mcp` · `models` · `show` · `recent` · `inventory` · `export` · `health` · `backup` · `restore` · `format` |
| `scripts/cbut-tui.py` | the eight-tab Textual UI |
| `scripts/tests/` | the 326 tests above |
| `docs/maintenance/` | what to re-check when CodeBuddy changes: the generated format-dependency surface, the version/compatibility matrix, and the size & performance baseline with the commands that produced it |
| `systemd/` | optional daily sync service + timer |
| `requirements.txt` | `textual>=8.2,<9` — TUI only; everything else is stdlib |

Both paths are overridable for a sandboxed install: `CBUT_SCRIPTS` and `CBUT_VENV` for the
launcher, `CBUT_DB` and `CBUT_CODEBUDDY_DIR` for the data (see [Usage](#-usage)).

## ⚠️ Limitations & roadmap

- 🔌 **MCP usage is sparse** until you actually invoke an MCP tool; the tab is populated from
  real calls (`mcp__<server>__<tool>`), including calls wrapped by the deferred-tool
  mechanism (`DeferExecuteTool`).
- 🤖 **Internal agents** (`autoModeClassifier`, `summaryGenerator`, …) live in the OTel
  traces; trace ingestion is planned but not yet wired into v1.
- 🏷️ **No provider field** — CodeBuddy transcripts record no provider / account / site /
  endpoint, so the Usage page has no provider column at all rather than guessing one
  from the model name.
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

## 🙏 Attribution

Built in the spirit of
[`opencode-skill-tracker`](https://github.com/SHADE-glitch/opencode-skill-tracker),
which tracks the same five categories for OpenCode. The difference is architectural:
OpenCode has no persisted tool log, so that project needs a live hook plugin; CodeBuddy
does, so `cbut` reads instead. 💡

## 📄 License

MIT — see [LICENSE](LICENSE). © 2026 SHADE-glitch
