# CodeBuddy 使用追踪器（`cbut`）

![CodeBuddy](https://img.shields.io/badge/CodeBuddy-2.16x-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![SQLite](https://img.shields.io/badge/storage-SQLite-003B57?logo=sqlite)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
[![Repository](https://img.shields.io/badge/repository-GitHub-black?logo=github)](https://github.com/SHADE-glitch/codebuddy-usage-tracker)

记录并查询 [CodeBuddy Code](https://cnb.cool/codebuddy/codebuddy-code) 里**每一个内置工具、skill、agent、插件和 MCP 工具**的真实使用情况：什么时候跑、跑了多久、是否完成、在哪个项目里。所有数据落在一个本地 SQLite 文件——不联网、不上传、不含对话内容。

[English](README.md) · **简体中文**

---

## 🤔 为什么

CodeBuddy 其实**已经在写**你需要的数据，只是**从不展示**。它保存了：

- **会话转录**：`~/.codebuddy/projects/<cwd>/<session>.jsonl`，每次工具调用是一条 `function_call` 记录（含名称、参数、状态、时间戳）；
- **OpenTelemetry 轨迹**：`~/.codebuddy/traces/<pid>/trace_*.jsonl`（`function` / `agent` / `generation` span）；

对外却只提供会话级的 `/cost` 和 `/context`，没有跨会话、按工具 / 按 skill / 按插件的统计。`cbut` 补的就是这个缺口。

`cbut` 是一个**被动读取器**：解析 CodeBuddy 已经写好的日志并建立索引。它不碰 CodeBuddy 的任何文件、不装 hook、不增加运行时开销。

刻意保留的设计约束：

- **不修改 CodeBuddy 源码**，无需安装插件。
- 对 `~/.codebuddy` **只读**；唯一写入的是 `cbut` 自己的数据库。
- 只存结构化元数据（工具名、状态、耗时、项目、会话、模型）。**不存消息正文、不存密钥、不存工具参数值。**
- 一个存储层 + 一个索引器 + 一个入口，依赖最小。

## 📊 五个 tab

| Tab | 内容 |
|---|---|
| **Tools** | 内置工具调用次数、完成率、平均耗时、最近使用 |
| **Skills** | skill 调用、所属插件、首次/最近使用 |
| **Agents** | 用到的子代理类型，主动 vs 内部 |
| **Plugins** | 已安装插件的 skill/agent/command，已用 vs 未用 |
| **MCP** | MCP 服务器与工具、调用次数 |

任意一行按回车，可查看该对象的按会话历史。

## 📋 环境要求

| | |
|---|---|
| 系统 | Linux（在 Ubuntu 上开发验证；其他发行版未验证） |
| CodeBuddy | 2.16x —— schema 按 2.161.4 实测 |
| Python | 3.11+（实测 3.14）。无头命令只需标准库 |
| `textual` | 仅交互式 TUI 需要（由 `install.sh` 安装） |

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
cbut                     # 交互式 TUI（五个 tab）
cbut sync                # 增量索引新日志
cbut sync --full         # 从零重建数据库
cbut stats               # 五类总览
cbut tools|skills|agents|plugins|mcp
cbut show tool Bash      # 某个对象的最近调用
cbut recent              # 最近的工具调用
cbut inventory           # 已安装清单，已用 vs 未用
cbut export              # 导出所有表为 JSON
cbut health              # 数据库与数据源检查
```

数据库位置：`~/.local/share/codebuddy-usage-tracker/usage.db`
（可用 `CBUT_DB` 覆盖；源目录用 `CBUT_CODEBUDDY_DIR` 覆盖）。

## 🔒 隐私

`cbut` 只读取 CodeBuddy 自身日志中的结构化元数据字段，只存储计数、耗时、状态和标识符。不读取也不存储提示词/回复正文、工具参数值或文件内容。数据不出本机。

## ⚠️ 限制 / 路线图

- **MCP 数据初期会很少**，除非你真的调用了 MCP 工具；该 tab 由真实调用填充（`mcp__<server>__<tool>`），也包括被延迟工具机制（`DeferExecuteTool`）包裹的调用。
- **内部 agent**（`autoModeClassifier`、`summaryGenerator` 等）只在 OTel 轨迹里，v1 尚未接入轨迹采集。
- 同步是手动的（或可选 systemd 定时器）；没有实时 hook。

## 🙏 致谢

灵感来自 [`opencode-skill-tracker`](https://github.com/SHADE-glitch/opencode-skill-tracker)——它为 OpenCode 追踪同样的五类。区别在架构：OpenCode 不落盘工具日志，所以那个项目需要实时 hook 插件；CodeBuddy 会落盘，所以 `cbut` 选择读取。

## 📄 许可

MIT —— 见 [LICENSE](LICENSE)。
