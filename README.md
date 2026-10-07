# CodeBuddy Usage Tracker (`cbut`)

![CodeBuddy](https://img.shields.io/badge/CodeBuddy-2.16x-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![SQLite](https://img.shields.io/badge/storage-SQLite-003B57?logo=sqlite)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
[![Repository](https://img.shields.io/badge/repository-GitHub-black?logo=github)](https://github.com/SHADE-glitch/codebuddy-usage-tracker)

Record and query how **every built-in tool, skill, agent, plugin and MCP tool**
in [CodeBuddy Code](https://cnb.cool/codebuddy/codebuddy-code) is actually used:
what ran, when, how long it took, whether it completed, and in which project.
Everything lands in a single local SQLite file — no network, no upload, no
conversation content.

**English** · [简体中文](README.zh-CN.md)

---

## 🤔 Why

CodeBuddy already *writes* the raw data you need — but it never *shows* it.
It keeps:

- **session transcripts** in `~/.codebuddy/projects/<cwd>/<session>.jsonl`
  (every tool call as a `function_call` record with name, arguments, status and
  timestamps);
- **OpenTelemetry spans** in `~/.codebuddy/traces/<pid>/trace_*.jsonl`
  (`function` / `agent` / `generation` spans);

and exposes only session-level `/cost` and `/context`. There is no
cross-session, per-tool / per-skill / per-plugin breakdown. That gap is what
`cbut` fills.

`cbut` is a **passive reader**: it parses the logs CodeBuddy already writes and
builds an index. It never touches CodeBuddy's files, installs no hook, and adds
no runtime overhead.

Design constraints, deliberately kept:

- **No source changes to CodeBuddy**, no plugin to install.
- **Read-only** on `~/.codebuddy`; the only thing written is `cbut`'s own DB.
- Only structured metadata is stored (tool name, status, duration, project,
  session, model). **No message bodies, no secrets, no tool argument values.**
- One storage layer + one indexer + one entry point, minimal deps.

## 📊 The five tabs

| Tab | What it shows |
|---|---|
| **Tools** | built-in tool counts, completion, average duration, last used |
| **Skills** | skill invocations, owning plugin, first/last use |
| **Agents** | subagent types used, active vs internal |
| **Plugins** | installed plugins with their skills/agents/commands, used vs unused |
| **MCP** | MCP servers and tools, invocation counts |

Select any row (Enter) for a per-session history of that entity.

## 📋 Requirements

| | |
|---|---|
| OS | Linux (developed and verified on Ubuntu; other distributions unverified) |
| CodeBuddy | 2.16x — schema verified against 2.161.4 |
| Python | 3.11+ (tested on 3.14). Headless commands need only the stdlib |
| `textual` | only for the interactive TUI (installed by `install.sh`) |

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
cbut                     # interactive TUI (five tabs)
cbut sync                # incremental index of new log data
cbut sync --full         # rebuild the database from scratch
cbut stats               # overview of all five categories
cbut tools|skills|agents|plugins|mcp
cbut show tool Bash      # recent calls for one entity
cbut recent              # latest tool calls
cbut inventory           # what is installed, used vs unused
cbut export              # dump every table as JSON
cbut health              # database + data-source check
```

Database location: `~/.local/share/codebuddy-usage-tracker/usage.db`
(override with `CBUT_DB`; source dir override `CBUT_CODEBUDDY_DIR`).

## 🔒 Privacy

`cbut` reads only the structured metadata fields of CodeBuddy's own logs and
stores counts, timings, status and identifiers. It does not read or store
prompt/response text, tool argument values, or file contents. Nothing leaves
the machine.

## ⚠️ Limitations / roadmap

- **MCP usage will be sparse** until you actually invoke an MCP tool; the tab is
  populated from real calls (`mcp__<server>__<tool>`), including calls wrapped by
  the deferred-tool mechanism (`DeferExecuteTool`).
- **Internal agents** (`autoModeClassifier`, `summaryGenerator`, …) live in the
  OTel traces; trace ingestion is planned but not yet wired into v1.
- Sync is manual (or via the optional systemd timer); there is no live hook.

## 🙏 Attribution

Built in the spirit of
[`opencode-skill-tracker`](https://github.com/SHADE-glitch/opencode-skill-tracker),
which tracks the same five categories for OpenCode. The difference is
architectural: OpenCode has no persisted tool log, so that project needs a live
hook plugin; CodeBuddy does, so `cbut` reads instead.

## 📄 License

MIT — see [LICENSE](LICENSE).
