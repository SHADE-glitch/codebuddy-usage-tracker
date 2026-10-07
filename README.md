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
- [📊 The five tabs](#-the-five-tabs)
- [📋 Requirements](#-requirements)
- [🚀 Install](#-install)
- [🧭 Usage](#-usage)
- [🔒 Privacy](#-privacy)
- [⚠️ Limitations & roadmap](#️-limitations--roadmap)
- [🙏 Attribution](#-attribution)
- [📄 License](#-license)

## ✨ Features

- 🧰 **Five views, one command** — tools, skills, agents, plugins, MCP.
- 📜 **Full history, instantly** — backfills every session already on disk.
- 🪶 **Passive & read-only** — no hook, no plugin, no CodeBuddy source changes.
- 🗃️ **One local SQLite file** — no network, no upload, no telemetry.
- ⚡ **Fast incremental sync** — ~3 s to index 273 sessions / 393 MB; only new bytes after that.
- 🖥️ **TUI + headless** — a five-tab Textual UI for humans, JSON/CSV-friendly reports for scripts.
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

## 📊 The five tabs

| | Tab | What it shows |
|---|-----|---------------|
| 🧰 | **Tools** | built-in tool counts, completion, average duration, last used |
| 🎯 | **Skills** | skill invocations, owning plugin, first/last use |
| 🤖 | **Agents** | subagent types used, active vs internal |
| 🧩 | **Plugins** | installed plugins with their skills/agents/commands, used vs unused |
| 🔌 | **MCP** | MCP servers and tools, invocation counts |

Press <kbd>Enter</kbd> on any row for a per-session history of that entity.

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
cbut                     # 🖥️  interactive TUI (five tabs)
cbut sync                # 🔄  incremental index of new log data
cbut sync --full         # ♻️  rebuild the database from scratch
cbut stats               # 📊  overview of all five categories
cbut tools|skills|agents|plugins|mcp
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
- 🔄 Sync is manual (or via the optional systemd timer); there is no live hook.

## 🙏 Attribution

Built in the spirit of
[`opencode-skill-tracker`](https://github.com/SHADE-glitch/opencode-skill-tracker),
which tracks the same five categories for OpenCode. The difference is architectural:
OpenCode has no persisted tool log, so that project needs a live hook plugin; CodeBuddy
does, so `cbut` reads instead. 💡

## 📄 License

MIT — see [LICENSE](LICENSE). © 2026 SHADE-glitch
