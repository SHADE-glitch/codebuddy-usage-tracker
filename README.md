# 🐾 CodeBuddy Usage Tracker (`cbut`)

![CodeBuddy](https://img.shields.io/badge/CodeBuddy-2.16x-blue?logo=robotframework&logoColor=white)
![Python](https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/storage-SQLite-003B57?logo=sqlite&logoColor=white)
![Textual](https://img.shields.io/badge/TUI-Textual-ff69b4?logo=terminal&logoColor=white)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Platform: Linux](https://img.shields.io/badge/platform-Linux-lightgrey?logo=linux&logoColor=black)
[![Repository](https://img.shields.io/badge/repository-GitHub-black?logo=github)](https://github.com/SHADE-glitch/codebuddy-usage-tracker)

> 🔭 See how you **actually** use [CodeBuddy Code](https://cnb.cool/codebuddy/codebuddy-code) —
> every 🧰 built-in tool, 🎯 skill, 🤖 agent, 🧩 plugin and 🔌 MCP tool.

**English** · [简体中文](README.zh-CN.md)

---

## 📖 Table of Contents

- [✨ Features](#-features)
- [🤔 Why this exists](#-why-this-exists)
- [🏗️ How it works](#️-how-it-works)
- [📊 The eight tabs](#-the-eight-tabs)
- [📋 Requirements](#-requirements)
- [🚀 Install](#-install)
- [🧭 Usage](#-usage)
- [🔒 Privacy](#-privacy)
- [🧪 Testing](#-testing)
- [📁 Repository layout](#-repository-layout)
- [⚠️ Limitations & roadmap](#️-limitations--roadmap)
- [🧹 Uninstall](#-uninstall)
- [🤝 Contributing](#-contributing)
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
| 🧭 | **Dashboard** | two panels — usage KPIs (tool/skill/agent/MCP/plugin counts) and a token summary (requests, input/output, API total, usage total, cache hit rate) — each for the Dashboard's own window |
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
  estimated. The priority columns sit leftmost; the cache detail is
  reachable by horizontal scroll.
- **Same accuracy rules as Tokens** — **API Total** = provider total when present, else
  `Input + Output`. A separate **Usage Total** (`Input + Output + cache hit`) is shown for
  reference, explicitly labelled as re-adding cache hit (~2× the API total). Cache
  hit/miss/write are never folded into the API total, and missing values stay `-`.
- **Cache hit rate** = `cache hit / (cache hit + cache miss + cache write)` — the
  cacheable input, matching cc-switch. It shows `-` when a row has no cache data.
- **The summary panels are height-capped and scroll internally**, so the Request Logs
  table below them stays visible even on a small terminal (verified down to 80×24).

## 📋 Requirements

| | |
|---|---|
| 🐧 OS | Linux — developed and verified on Ubuntu; other distributions **unverified** |
| 🐾 CodeBuddy | 2.16x — schema verified against 2.161.4 |
| 🐍 Python | 3.11+ (tested on 3.14). Headless commands need only the stdlib |
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
```

| Variable | Default | Purpose |
|---|---|---|
| `CBUT_DB` | `~/.local/share/codebuddy-usage-tracker/usage.db` | database location |
| `CBUT_CODEBUDDY_DIR` | `~/.codebuddy` | source log directory |

## 🔒 Privacy

`cbut` reads only the **structured metadata** fields of CodeBuddy's own logs and stores
counts, timings, statuses and identifiers. It does **not** read or store prompt/response
text, tool argument values, or file contents. 🔒 Nothing leaves the machine.

## 🧪 Testing

```bash
python3 -m unittest discover -s scripts/tests     # Ran 173 tests ... OK
```

The suites are plain `unittest` (stdlib only, so `pytest` discovers them too). Each one
builds its own throwaway database in a temp directory — **your real `usage.db` and your
`~/.codebuddy` logs are never opened**, and nothing reaches the network.

| Suite | Tests | Covers |
|---|---:|---|
| `test_sync.py` | 61 | transcript parsing, tool classification, incremental vs `--full` re-sync |
| `test_tui.py` | 53 | tab wiring and report shapes, through Textual's own `run_test` harness |
| `test_usage.py` | 50 | the calendar-day windows (Today / 2 / 3 / 7 / 30 days / all), the token totals and the Dashboard KPI |
| `test_readme_bilingual.py` | 3 | the two READMEs stay one document in two languages |
| `test_record_coverage.py` | 6 | every covered commit is recorded in `CHANGELOG.md` |

## 📁 Repository layout

| Path | What it is |
|---|---|
| `bin/cbut` | bash entry point. Bash on purpose: it must still print a useful error when the TUI venv is missing, so it cannot depend on Python. Prefers `./.venv/bin/python`, falls back to `python3` |
| `install.sh` | creates the venv (`uv` when available) and symlinks `~/.local/bin/cbut`. Idempotent; refuses to replace a non-symlink unless `--force` |
| `scripts/cbut_db.py` | SQLite schema and the query helpers every entry point shares |
| `scripts/cbut-sync.py` | log parser and incremental indexer (`--full`, `--quiet`) |
| `scripts/cbut-stats.py` | headless reports: `stats` · `tools` · `skills` · `agents` · `plugins` · `mcp` · `models` · `show` · `recent` · `inventory` · `export` · `health` |
| `scripts/cbut-tui.py` | the eight-tab Textual UI |
| `scripts/tests/` | the 173 tests above |
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

## 🤝 Contributing

Issues and pull requests are welcome — 🐛 a mis-classified tool, 📈 a window that adds up
wrong, or 💡 a report you would want that isn't there. Two things keep a change landing:

- run `python3 -m unittest discover -s scripts/tests` first, and add a case alongside a
  behaviour change;
- keep the two guarantees intact: **read-only** towards CodeBuddy's logs, and **metadata
  only** in the database. A PR that stores prompt text or argument values is off-purpose,
  however useful it looks.

## 🙏 Attribution

Built in the spirit of
[`opencode-skill-tracker`](https://github.com/SHADE-glitch/opencode-skill-tracker),
which tracks the same five categories for OpenCode. The difference is architectural:
OpenCode has no persisted tool log, so that project needs a live hook plugin; CodeBuddy
does, so `cbut` reads instead. 💡

## 📄 License

MIT — see [LICENSE](LICENSE). © 2026 SHADE-glitch
