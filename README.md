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
- [📊 The seven tabs](#-the-seven-tabs)
- [📋 Requirements](#-requirements)
- [🚀 Install](#-install)
- [🧭 Usage](#-usage)
- [🔒 Privacy](#-privacy)
- [⚠️ Limitations & roadmap](#️-limitations--roadmap)
- [🙏 Attribution](#-attribution)
- [📄 License](#-license)

## ✨ Features

- 🧰 **Seven views, one command** — tools, skills, agents, plugins, MCP, tokens, usage.
- 📜 **Full history, instantly** — backfills every session already on disk.
- 🪶 **Passive & read-only** — no hook, no plugin, no CodeBuddy source changes.
- 🗃️ **One local SQLite file** — no network, no upload, no telemetry.
- ⚡ **Fast incremental sync** — ~5 s to index ~300 sessions / ~420 MB; only new bytes after that.
- 🖥️ **TUI + headless** — a seven-tab Textual UI for humans, JSON reports for scripts.
- 🔢 **Real model tokens** — per-model input / output / total plus cache hit / miss / write, straight from `providerData.rawUsage`; total prefers the provider's own reported value.
- 📊 **Rolling usage windows** — a cc-switch-style request log over 24h/48h/72h/7d/30d/all time, measured back from *now*, not calendar days.
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

## 📊 The seven tabs

| | Tab | What it shows |
|---|-----|---------------|
| 🧰 | **Tools** | tool calls by name, completion, average duration, last used |
| 🎯 | **Skills** | skill invocations, owning plugin, last use |
| 🤖 | **Agents** | subagent types used, active vs internal |
| 🧩 | **Plugins** | installed plugins with their skills/agents/commands; **uses** counts every attributed skill/agent/command invocation, not just command entries |
| 🔌 | **MCP** | MCP servers and tools, invocation counts |
| 🔢 | **Tokens** | per-model requests, with-usage count, input, output, **API total**, **usage total**, cache hit/miss/write, coverage |
| 📊 | **Usage** | rolling-window request log (cc-switch-style columns) with summary panels |

Press <kbd>Enter</kbd> on any row for a per-entity history — recent calls for a tool,
skill, agent or MCP tool, and the per-response token detail for a model on the Tokens
tab. Every tab shows an explicit empty-state message when it has no data yet, instead
of a blank region.

**Plugins** counts every invocation attributed to a plugin — its skills, agents and
slash commands — so a plugin whose skill was used no longer reads `uses = 0`.
Attribution is evidence-based: a name is credited to a plugin only when the plugin
inventory maps that name to it.

> **API Total** = the provider's reported total when present, otherwise Input + Output.
> **Usage Total** is a display-only metric (`Input + Output + cache hit`) that
> **re-adds cache hit** — which is already inside Input — so it lands at ~2× the API
> Total. Never read it as the API total. Cache hit/miss/write are reported
> **separately** and are never added into the API total. Fields absent from a response
> are shown as `-`, never as a fabricated `0`.

### 📊 The Usage page

The Usage tab mirrors the style of a session-usage panel, but every number comes from
the transcripts on disk:

- **Rolling windows** — `24h` / `48h` / `72h` / `7d` / `30d` / `All time`, each
  computed back from the current clock, **not** natural calendar days and **not** from
  when the tracker first ran.
- **One Request Logs list**, newest first. The columns follow cc-switch's request
  records — time · provider · model · input · output · API total · usage total · cache
  read (hit) · cache miss · cache write · usage (Real / Partial / Missing) · source. cc-switch's cost,
  duration and HTTP-status columns have no transcript source, so they are omitted
  rather than estimated. The priority columns sit leftmost; the cache detail is
  reachable by horizontal scroll.
- **Provider is always `Transcript / Unknown`.** CodeBuddy transcripts carry no
  provider / account / site / endpoint field, so the provider column reports an honest
  `Unknown` rather than guessing from the model name — the same for the international,
  China-mainland and third-party coding-plan builds.
- **Same accuracy rules as Tokens** — **API Total** = provider total when present, else
  `Input + Output`, and the summary reports its `Source` (`provider` / `derived` /
  `mixed`). A separate **Usage Total** (`Input + Output + cache hit`) is shown for
  reference, explicitly labelled as re-adding cache hit (~2× the API total). Cache
  hit/miss/write are never folded into the API total, and missing values stay `-`.
- **Cache hit rate** = `cache hit / (cache hit + cache miss + cache write)` — the
  cacheable input, matching cc-switch. It shows `-` when a window has no cache data.
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

## 🧭 Usage

```bash
cbut                     # 🖥️  interactive TUI (seven tabs)
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

## ⚠️ Limitations & roadmap

- 🔌 **MCP usage is sparse** until you actually invoke an MCP tool; the tab is populated from
  real calls (`mcp__<server>__<tool>`), including calls wrapped by the deferred-tool
  mechanism (`DeferExecuteTool`).
- 🤖 **Internal agents** (`autoModeClassifier`, `summaryGenerator`, …) live in the OTel
  traces; trace ingestion is planned but not yet wired into v1.
- 🏷️ **No provider attribution** — CodeBuddy transcripts record no provider / account /
  site / endpoint, so the Usage page reports a single `Transcript / Unknown` provider
  row rather than guessing from the model name.
- 🔢 **`context tokens` vs model tokens.** The headless `cbut stats` prints a session-level
  `context tokens` figure (turn-metrics `tokenDelta`) next to the per-model token totals;
  the two are deliberately kept apart. The `context tokens` sum is idempotent — repeated
  syncs and schema migrations no longer inflate it.
- 🔄 Sync is manual (or via the optional systemd timer); there is no live hook.

## 🙏 Attribution

Built in the spirit of
[`opencode-skill-tracker`](https://github.com/SHADE-glitch/opencode-skill-tracker),
which tracks the same five categories for OpenCode. The difference is architectural:
OpenCode has no persisted tool log, so that project needs a live hook plugin; CodeBuddy
does, so `cbut` reads instead. 💡

## 📄 License

MIT — see [LICENSE](LICENSE). © 2026 SHADE-glitch
