# 🐾 CodeBuddy 使用追踪器（`cbut`）

![CodeBuddy](https://img.shields.io/badge/CodeBuddy-2.16x-blue?logo=robotframework&logoColor=white)
![Python](https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/storage-SQLite-003B57?logo=sqlite&logoColor=white)
![Textual](https://img.shields.io/badge/TUI-Textual-ff69b4?logo=terminal&logoColor=white)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Platform: Linux](https://img.shields.io/badge/platform-Linux-lightgrey?logo=linux&logoColor=black)
[![Repository](https://img.shields.io/badge/repository-GitHub-black?logo=github)](https://github.com/SHADE-glitch/codebuddy-usage-tracker)

> 🔭 看清你**真正**是怎么用 [CodeBuddy Code](https://cnb.cool/codebuddy/codebuddy-code) 的 ——
> 每一个 🧰 内置工具、🎯 skill、🤖 agent、🧩 插件和 🔌 MCP 工具。

[English](README.md) · **简体中文**

---

## 📖 目录

- [✨ 特性](#-特性)
- [🤔 为什么做这个](#-为什么做这个)
- [🏗️ 工作原理](#️-工作原理)
- [📊 五个 tab](#-五个-tab)
- [📋 环境要求](#-环境要求)
- [🚀 安装](#-安装)
- [🧭 用法](#-用法)
- [🔒 隐私](#-隐私)
- [⚠️ 限制与路线图](#️-限制与路线图)
- [🙏 致谢](#-致谢)
- [📄 许可](#-许可)

## ✨ 特性

- 🧰 **五个视图，一条命令** —— 工具、skill、agent、插件、MCP。
- 📜 **完整历史，即刻可得** —— 回填磁盘上已有的全部会话。
- 🪶 **被动只读** —— 无 hook、无插件、不改 CodeBuddy 源码。
- 🗃️ **单个本地 SQLite 文件** —— 不联网、不上传、无遥测。
- ⚡ **快速增量同步** —— 273 个会话 / 393 MB 约 3 秒完成；之后只读新增字节。
- 🖥️ **TUI + 无头** —— 给人用的五 tab Textual 界面，给脚本用的 JSON/CSV 报告。
- 🔐 **只存元数据** —— 名称、耗时、状态、项目。绝不存消息正文或参数值。

## 🤔 为什么做这个

CodeBuddy 其实**已经在写**你需要的数据，只是**从不展示**。它保存了：

- 📜 **会话转录**：`~/.codebuddy/projects/<cwd>/<session>.jsonl`，每次工具调用是一条 `function_call` 记录（含名称、参数、状态、时间戳）；
- 🔭 **OpenTelemetry 轨迹**：`~/.codebuddy/traces/<pid>/trace_*.jsonl`（`function` / `agent` / `generation` span）；

对外却只提供会话级的 `/cost` 和 `/context`，**没有**跨会话、按工具 / 按 skill / 按插件的统计。`cbut` 补的就是这个缺口。

## 🏗️ 工作原理

```text
   ~/.codebuddy/                         cbut                          ~/.local/share/
 ┌──────────────────┐          ┌─────────────────────┐          ┌────────────────────────┐
 │ projects/        │          │  cbut-sync.py       │          │ codebuddy-usage-       │
 │   **/*.jsonl  ───┼── 只读 ──▶  解析 + 归类        │          │   tracker/usage.db     │
 │                  │          │  增量、幂等          │  写入     │                        │
 │ traces/          │          │                     ├─────────▶│  SQLite（工具 · skill · │
 │   **/*.jsonl     │          └─────────────────────┘          │  agent · 插件 · mcp）   │
 │                  │                    ▲                       └───────────┬────────────┘
 │ mcp.json         │                    │                                   │
 │ settings.json    │          ┌─────────┴───────────┐                       │
 │ plugins/         │          │  cbut-tui.py（TUI） │◀────── 查询 ──────────┘
 └──────────────────┘          │  cbut-stats.py（CLI）│
      不修改                   └─────────────────────┘
```

`cbut` 是一个**被动读取器**：解析 CodeBuddy 已经写好的日志并建立索引。它不碰 CodeBuddy 的任何文件、不装 hook、不增加运行时开销。

## 📊 五个 tab

| | Tab | 内容 |
|---|-----|------|
| 🧰 | **Tools** | 内置工具调用次数、完成率、平均耗时、最近使用 |
| 🎯 | **Skills** | skill 调用、所属插件、首次/最近使用 |
| 🤖 | **Agents** | 用到的子代理类型，主动 vs 内部 |
| 🧩 | **Plugins** | 已安装插件的 skill/agent/command，已用 vs 未用 |
| 🔌 | **MCP** | MCP 服务器与工具、调用次数 |

任意一行按 <kbd>Enter</kbd>，可查看该对象的按会话历史。

## 📋 环境要求

| | |
|---|---|
| 🐧 系统 | Linux —— 在 Ubuntu 上开发验证；其他发行版**未验证** |
| 🐾 CodeBuddy | 2.16x —— schema 按 2.161.4 实测 |
| 🐍 Python | 3.11+（实测 3.14）。无头命令只需标准库 |
| 🖥️ `textual` | 仅交互式 TUI 需要（由 `install.sh` 安装） |

## 🚀 安装

```bash
git clone https://github.com/SHADE-glitch/codebuddy-usage-tracker.git
cd codebuddy-usage-tracker
./install.sh          # 创建带 textual 的 .venv，并链接 ~/.local/bin/cbut
cbut sync             # 用已有 CodeBuddy 日志建立索引
cbut                  # 启动 TUI
```

不想用 venv？无头报告用系统 `python3` 即可：

```bash
python3 scripts/cbut-sync.py
python3 scripts/cbut-stats.py stats
```

## 🧭 用法

```bash
cbut                     # 🖥️  交互式 TUI（五个 tab）
cbut sync                # 🔄  增量索引新日志
cbut sync --full         # ♻️  从零重建数据库
cbut stats               # 📊  五类总览
cbut tools|skills|agents|plugins|mcp
cbut show tool Bash      # 🔍  某个对象的最近调用
cbut recent              # 🕒  最近的工具调用
cbut inventory           # 📦  已安装清单，已用 vs 未用
cbut export              # 💾  导出所有表为 JSON
cbut health              # 🩺  数据库与数据源检查
```

| 变量 | 默认值 | 用途 |
|---|---|---|
| `CBUT_DB` | `~/.local/share/codebuddy-usage-tracker/usage.db` | 数据库位置 |
| `CBUT_CODEBUDDY_DIR` | `~/.codebuddy` | 源日志目录 |

## 🔒 隐私

`cbut` 只读取 CodeBuddy 自身日志中的**结构化元数据**字段，只存储计数、耗时、状态和标识符。**不**读取也不存储提示词/回复正文、工具参数值或文件内容。🔒 数据不出本机。

## ⚠️ 限制与路线图

- 🔌 **MCP 数据初期会很少**，除非你真的调用了 MCP 工具；该 tab 由真实调用填充（`mcp__<server>__<tool>`），也包括被延迟工具机制（`DeferExecuteTool`）包裹的调用。
- 🤖 **内部 agent**（`autoModeClassifier`、`summaryGenerator` 等）只在 OTel 轨迹里，v1 尚未接入轨迹采集。
- 🔄 同步是手动的（或可选 systemd 定时器）；没有实时 hook。

## 🙏 致谢

灵感来自 [`opencode-skill-tracker`](https://github.com/SHADE-glitch/opencode-skill-tracker) —— 它为 OpenCode 追踪同样的五类。区别在架构：OpenCode 不落盘工具日志，所以那个项目需要实时 hook 插件；CodeBuddy 会落盘，所以 `cbut` 选择读取。💡

## 📄 许可

MIT —— 见 [LICENSE](LICENSE)。© 2026 SHADE-glitch
