<p align="right"><a href="README.md">English</a> | <a href="README.zh-CN.md"><b>简体中文</b></a></p>

# 🐾 CodeBuddy 使用追踪器（`cbut`）

![CodeBuddy](https://img.shields.io/badge/CodeBuddy-reads%20local%20logs-blue?logo=robotframework&logoColor=white)
![Python](https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/storage-SQLite-003B57?logo=sqlite&logoColor=white)
![Textual](https://img.shields.io/badge/TUI-Textual-ff69b4?logo=terminal&logoColor=white)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Platform: Linux](https://img.shields.io/badge/platform-Linux-lightgrey?logo=linux&logoColor=black)
[![Repository](https://img.shields.io/badge/repository-GitHub-black?logo=github)](https://github.com/SHADE-glitch/codebuddy-usage-tracker)

> 🔭 看清你**真正**是怎么用 [CodeBuddy Code](https://cnb.cool/codebuddy/codebuddy-code) 的 ——
> 每一个 🧰 内置工具、🎯 skill、🤖 agent、🧩 插件和 🔌 MCP 工具。

---

## 📖 目录

- [✨ 特性](#-特性)
- [🤔 为什么做这个](#-为什么做这个)
- [🏗️ 工作原理](#️-工作原理)
- [📊 八个 tab](#-八个-tab)
- [📋 环境要求](#-环境要求)
- [🚀 安装](#-安装)
- [🧭 用法](#-用法)
- [⚙️ 配置](#️-配置)
- [🔒 隐私](#-隐私)
- [🧪 测试](#-测试)
- [📁 仓库结构](#-仓库结构)
- [⚠️ 限制与路线图](#️-限制与路线图)
- [🧹 卸载](#-卸载)
- [🙏 致谢](#-致谢)
- [📄 许可](#-许可)

## ✨ 特性

- 🧰 **八个视图，一条命令** —— Dashboard、工具、skill、agent、插件、MCP、token、用量。
- 📜 **完整历史，即刻可得** —— 回填磁盘上已有的全部会话。
- 🔑 **以名字为准，什么都不丢** —— 每个 tab 都同时列出你**用过**的和已安装的，仅以名字为主键：改名、升级或被删除的对象都保留历史，同名但来自不同归属的合并成一行。
- 🪶 **被动只读** —— 无 hook、无插件、不改 CodeBuddy 源码。
- 🗃️ **单个本地 SQLite 文件** —— 不联网、不上传、无遥测。
- ⚡ **快速增量同步** —— 约 300 个会话 / 420 MB 约 5 秒完成；之后只读新增字节。
- 🖥️ **TUI + 无头** —— 给人用的八 tab Textual 界面，给脚本用的 JSON 报告。
- 🔢 **真实模型 token** —— 按模型的输入 / 输出 / 总计，以及缓存 hit / miss / write，直接来自 `providerData.rawUsage`；总计优先使用 provider 自报值。
- 📊 **自然日窗口，每个 tab 独立** —— 顶部栏的范围选择器（今天 / 2 天 / 3 天 / 7 天 / 30 天 / 全部时间）**按 tab 各自保存**，改一个不会影响别的：实体 tab 默认 **7 天**，Dashboard 与 Usage 页默认 **今天**，Plugins 恒为全时段。窗口按**本地自然日**（00:00 → 当前），绝不是「当前减 N 小时」。窗口只缩小**计数**，列表始终完整。
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

## 📊 八个 tab

**Dashboard** 最先打开：一屏概览重要数字，让你知道该往哪里深入。tab 上方有一条共享顶部栏，其中放着范围选择器，且**每个 tab 各自保存自己的范围** —— 改一个 tab 的窗口不会影响其他 tab。实体 tab（Tools、Skills、Agents、MCP、Tokens）默认 **7 天**；Dashboard 与 Usage 页默认 **今天**。**Plugins** tab 完全没有窗口：它的列表是全时段，所以那里会隐藏选择器。窗口只改变**计数**，绝不丢行 —— 因为每个 tab 都以名字为主键。

| | Tab | 内容 |
|---|-----|------|
| 🧭 | **Dashboard** | 一组面板，均取 Dashboard 自己的窗口：用量 KPI（工具/skill/agent/MCP/插件 调用数）、token 摘要（请求数、输入/输出、API 总计、usage 总计、缓存命中率）、缓存 hit/miss/write 绝对值、运行时健康（完成/未完成、平均工具耗时、去重 session/project 数）、Top 5 模型与 Top 5 工具，以及按天调用量 sparkline |
| 🧰 | **Tools** | 按名称统计的工具调用、完成率、平均耗时、最近使用 |
| 🎯 | **Skills** | 按名称统计的 skill 调用（跨归属合并）、最近使用 |
| 🤖 | **Agents** | 按名称统计的子代理类型、调用次数、最近使用 |
| 🧩 | **Plugins** | 每个插件 —— 已安装的**和**用过的 —— 及其 skill/agent/command；全时段，无版本列 |
| 🔌 | **MCP** | MCP 服务器与工具、调用次数 |
| 🔢 | **Tokens** | 按模型的 **usage 总计**、请求数、输入、输出、**API 总计**、缓存 hit/miss/write |
| 📊 | **Usage** | 请求日志与多个汇总面板（cc-switch 风格列） |

任意一行按 <kbd>Enter</kbd>，可查看该对象的最近调用历史 —— 工具、skill、agent、MCP 工具显示最近调用，Tokens tab 上显示该模型的逐条 token 明细。任何 tab 在没有数据时都会显示明确的空状态提示，而不是一片空白。

**每个 tab 都列出「用过的 ∪ 已安装的」的并集，以名字为主键，按调用次数排序。** 被删除的 skill、被卸载的插件、被改名的 agent 都保留自己的行和计数；同名但来自两个插件的合并成一行。**Plugins** 统计归属到插件的每一次调用 —— 包括它的 skill、agent 和斜杠命令 —— 所以某个插件的 skill 被用过之后，不会再显示 `uses = 0`。归属是**有据可依**的：只有当插件清单把某个名字映射到该插件时，才会计入。

> **API 总计** = 有 provider 原始值时用原始值，否则用 `输入 + 输出`。
> **Usage 总计** 是仅展示的指标（`输入 + 输出 + 缓存 hit`），它**重复加了一次缓存 hit**（hit 本就包含在输入里），所以约为 API 总计的 2 倍 —— 绝不要把它当作 API 总计。
> 缓存 hit/miss/write **单独展示**，绝不加进 API 总计。响应里缺失的字段显示为 `-`，绝不伪装成 `0`。

### 📊 Usage 页

Usage tab 仿照会话用量面板的样式，但每个数字都来自磁盘上的转录：

- **Usage 页保留自己的范围**（默认 **今天**），在 Usage tab 激活时显示在共享顶部栏里；其他每个 tab 也各自保留，互不影响。选项为 今天 / 2 天 / 3 天 / 7 天 / 30 天 / 全部时间 —— 以当前时刻结尾的**整段本地自然日**（00:00 → 现在），**不是**「当前减 N 小时」，也**不是** tracker 首次运行的时刻。本页的 **Window / Requests** 面板显示当前范围的日期。
- **单一的 Request Logs 列表**，最近的请求在前。列顺序对齐 cc-switch 的请求记录 —— 时间 · 模型 · usage 总计 · 输入 · 输出 · API 总计 · 缓存读取(hit) · 缓存未命中(miss) · 缓存创建(write) · 缓存命中率。cc-switch 的供应商、费用、耗时、HTTP 状态码列在转录里没有数据源，因此**略去而非估算**。优先列在最左；终端太窄放不下时，列计划会**整列收起**尾部而不是把标签切一半——表格上方那行会写明收了哪几列、要多宽才能全部看到。
- **与 Tokens 相同的准确性规则** —— **API 总计** = 有 provider 原始值时用原始值，否则 `输入 + 输出`。另有一个 **Usage 总计**（`输入 + 输出 + 缓存 hit`）仅供参照，并明确标注它会重复计入缓存 hit（约 2 倍 API 总计）。缓存 hit/miss/write 绝不并入 API 总计，缺失值保持 `-`。
- **缓存命中率** = `缓存 hit / (缓存 hit + 缓存 miss + 缓存 write)` —— 即「可缓存输入」，与 cc-switch 一致；某行没有缓存数据时显示 `-`。
- **汇总面板高度受限并内部滚动**，因此下方的 Request Logs 表在小终端上也始终可见。80×24 实测：
  面板转为两列、高度仍受限，表格保留 9 行（测试把下限钉在 6）；列计划此时只显示 **10 列里的前 5 列**，
  表上方那行写明被收起的 5 列。具体收哪几列取决于数据 —— 宽度是按真实行内容量出来的，不是假设的；
  这两个数都是本机自己的数据库在 80×24 下的读数，你那台机器上的数字会不一样。没有任何一列被切成半截。
- **列计划覆盖每一张表，不只是被抱怨的那两张**：8 个标签页里的 7 张表（Dashboard 只有面板、没有表），
  加上两个详情页（某个实体的调用历史、某个模型的响应），一共 9 张 —— 数目是从源码里数出来的，不是估的，
  而且只要有一张表没进登记表就会有用例失败。所以名字长到会把列挤出屏幕时，代价是少几列，而不是少可读性。
  下限是 2 列：连「名字 + 第一个数字」都放不进 80 格时，提示行会写 `even these are cut`
  （这些也仍然被切），而不是假装表格放得下。
- **远超该占宽度的单元格会被截到 28 个字符**（默认值，设置项 `name_cap`）—— 这个数是本机数据量出来的：
  实体名全部放得下
  （tool 22、model 24、agent 20、plugin 19、skill 28），唯一超出的是项目路径
  （482 行里 162 行超过 24，最长 74）。路径**从左边截**（`…/work/backend-service`），
  因为它的开头是每行都相同的前缀、能区分彼此的在结尾；其它值**从右边截**，保住可读的头部。
  截了就会说：提示行写 `cells capped at 28`（印的是当前生效的数，不是写死的常量），
  而且它只在「不截就要丢列」时才生效——宽终端照样显示完整值。
  两个名字共享可见前缀的行仍各自进自己的详情页，因为行键用的是未截断的值。
  本机实际收益：History 页在 80×24 从 5 列里的 2 列回到 **5 列全显**，其它页读数不变。

## 📋 环境要求

| | |
|---|---|
| 🐧 系统 | Linux —— 在 Ubuntu 上开发验证；其他发行版**未验证** |
| 🐾 CodeBuddy | CodeBuddy Code —— schema 是**按你本机的日志**实测的，不是按版本号：转录行里没有任何版本字段，而本机 `codebuddy` 是 shell alias，版本号取不到。详见 [`docs/maintenance/compatibility.md`](docs/maintenance/compatibility.md#codebuddy) |
| 🐍 Python | 3.11+ —— 全套在 **3.11 / 3.12 / 3.13 / 3.14** 上均跑绿（CI 跑的是 3.11 和 3.14）。无头命令只需标准库 |
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

### 🔄 可选：不开 TUI 也让索引保持最新

[`systemd/`](systemd) 里带了两个用户级 unit，装上后每天跑一次 `cbut sync --quiet`。下面这四
行就是 `install.sh` 安装成功时最后打印的那段：

```bash
mkdir -p ~/.config/systemd/user
cp systemd/cbut-sync.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cbut-sync.timer
```

`Persistent=true` 会把关机期间错过的运行补上，唤醒时刻随机抖动最多 15 分钟，服务以
`Nice=10` 运行 —— 不会和你的会话抢 CPU。用 `systemctl --user status cbut-sync.timer` 查看。

## 🧭 用法

```bash
cbut                     # 🖥️  交互式 TUI（八个 tab）
cbut sync                # 🔄  增量索引新日志
cbut sync --full         # ♻️  从零重建数据库
cbut stats               # 📊  各类总览
cbut tools|skills|agents|plugins|mcp
cbut models              # 🔢  按模型 token：in/out/total + 缓存 hit/miss/write
cbut show tool Bash      # 🔍  某个对象的最近调用
cbut recent              # 🕒  最近的工具调用
cbut inventory           # 📦  已安装清单，已用 vs 未用
cbut export              # 💾  导出所有表为 JSON
cbut health              # 🩺  数据库与数据源检查
cbut backup              # 💾  风险步骤前留快照（`sync --full` 会自己做）
cbut restore             # ⏪  列出快照；`cbut restore NAME` 回滚到某一份
cbut format              # 🧾  本工具依赖的 CodeBuddy 字段与路径
```

| 变量 | 默认值 | 用途 |
|---|---|---|
| `CBUT_DB` | `~/.local/share/codebuddy-usage-tracker/usage.db` | 数据库位置 |
| `CBUT_CODEBUDDY_DIR` | `~/.codebuddy` | 源日志目录 |

## ⚙️ 配置

每个可调项的默认值就是它原本的行为，而且这个文件是可选的：你不建它，`cbut` 就按文档里写的那样跑。

`~/.config/cbut/config.toml` —— 或用 `CBUT_CONFIG` 指到别处：

```toml
top_n = 5            # Dashboard 里 Top models / Top tools 的行数
log_limit = 100      # Usage 页的请求行数
detail_limit = 200   # 历史 / 模型响应详情页的行数
name_cap = 28        # 单元格最宽渲染多少字符，超出就截断
refresh_secs = 5     # TUI 重绘定时器
sync_secs = 30       # TUI 自动同步定时器
```

- **优先级：环境变量 > 文件 > 默认值。** 每个键也读 `CBUT_TOP_N`、`CBUT_LOG_LIMIT`、
  `CBUT_DETAIL_LIMIT`、`CBUT_NAME_CAP`、`CBUT_REFRESH_SECS`、`CBUT_SYNC_SECS`。
- **配置文件坏了就直接拦住 TUI**，并说出是哪个文件、哪个键——而不是默默用默认值启动。
  一边编辑一个不被读取的文件，是设置层最坏的失败方式。`cbut health` 会给同样的判定，并写明它读到了哪个文件。
- **未知键直接拒绝**：拼错的设置会永远什么都不做，而且不吭声。值也做类型与范围校验
  （`top_n = 0` 是空白面板，不是偏好）。
- 用标准库的 `tomllib` 解析，所以**不新增依赖**、也没有任何联网面。程序永远不写这个文件，
  设置也不存进数据库。

## 🔒 隐私

`cbut` 只读取 CodeBuddy 自身日志中的**结构化元数据**字段，只存储计数、耗时、状态和标识符。**不**读取也不存储提示词/回复正文、工具参数值或文件内容。🔒 数据不出本机。

## 🧪 测试

```bash
python3 -m unittest discover -s scripts/tests     # Ran 334 tests ... OK
```

测试是纯 `unittest`（只用标准库，所以 `pytest` 也能收集）。每一份都在临时目录里建自己的
一次性数据库 —— **不会打开你真实的 `usage.db`，也不会读 `~/.codebuddy` 日志**；本仓库代码没有任何联网路径
（`test_privacy.py` 用 import 白名单守着这条）。

| 测试文件 | 用例数 | 覆盖 |
|---|---:|---|
| `test_sync.py` | 85 | 转录解析、工具归类、增量同步与 `--full` 重建、崩溃恢复、未识别记录计数 |
| `test_tui.py` | 86 | tab 接线与报告结构（走 Textual 自带的 `run_test`）、窄终端布局，以及**每张表**都不再把列切半的"列计划"、单元格上限与它背后的设置项、状态栏诚实性 |
| `test_usage.py` | 61 | 自然日窗口（今天 / 2 / 3 / 7 / 30 天 / 全部）、token 汇总、Dashboard 查询、结构版本门禁、索引值不值得留 |
| `test_config.py` | 21 | 设置层：默认值没变、文件、env 优先级与取值边界、坏文件会让 `main()` 在启动 TUI 前就退出、app 真的读了哪些键，以及 `cbut health` 把每个键都报出来——环境变量会被藏起来，所以一台配置过的机器也不会把这些用例变成假失败 |
| `test_dispatcher.py` | 12 | `bin/cbut`：子命令转发、venv 解析、装不上时 help 仍然能跑 |
| `test_format_registry.py` | 10 | CodeBuddy 格式登记表与解析器双向一致 |
| `test_privacy.py` | 11 | import 白名单，**加上绕过它的调用方式**（`__import__`/`eval`/`import_module`）、任何表任何文本列都不落自由文本、不写 CodeBuddy 自己的文件 |
| `test_hermetic.py` | 10 | 一个运行时绊线：套件绝不打开真实的 `usage.db`——进程内没有 `sqlite3.connect`，也没有哪个子进程会在没钉 `CBUT_DB`/`--db` 时去解析它 |
| `test_snapshots.py` | 9 | 破坏性步骤前自动留快照、列举、回滚、保留份数 |
| `test_maintenance_docs.py` | 13 | 生成的格式依赖文档不可能与代码脱节，任何文档也不许写工作流里并没有的 CI 版本 |
| `test_readme_counts.py` | 5 | 上面这张表就是运行器会打印的那张表，并且两份页面都把 loader 认识的每个设置项写全了 |
| `test_record_coverage.py` | 6 | 每个被覆盖的提交都记录进 `CHANGELOG.md` |
| `test_readme_bilingual.py` | 5 | 两份 README 始终是「一份文档、两种语言」，且目录里每个链接都落得到真实标题 |

`test_readme_counts.py` 负责让这张表不说谎：新增套件却不加行、或行里的数字过期，套件就会红。

## 📁 仓库结构

| 路径 | 是什么 |
|---|---|
| `bin/cbut` | bash 入口。刻意用 bash：venv 缺失或坏掉时它仍要能跑出有用的报错，所以不能依赖 Python。优先用 `./.venv/bin/python`，否则退回 `python3` |
| `install.sh` | 建 venv（有 `uv` 就用）并把 `~/.local/bin/cbut` 做成符号链接。可重复执行；目标不是符号链接时，不加 `--force` 拒绝覆盖 |
| `scripts/cbut_db.py` | SQLite 结构，以及各入口共用的查询辅助 |
| `scripts/cbut-sync.py` | 日志解析与增量索引器（`--full`、`--quiet`） |
| `scripts/cbut-stats.py` | 无头报告：`stats` · `tools` · `skills` · `agents` · `plugins` · `mcp` · `models` · `show` · `recent` · `inventory` · `export` · `health` · `backup` · `restore` · `format` |
| `scripts/cbut-tui.py` | 八个 tab 的 Textual 界面 |
| `scripts/tests/` | 上面那 334 个用例 |
| `docs/maintenance/` | CodeBuddy 变了之后要复查什么：生成的格式依赖清单、版本兼容矩阵、以及体积与性能的固定度量法和基线 |
| `systemd/` | 可选的每日同步 service + timer |
| `requirements.txt` | `textual>=8.2,<9` —— 只有 TUI 需要，其余全是标准库 |

沙箱安装可以改路径：入口用 `CBUT_SCRIPTS` 与 `CBUT_VENV`，数据用 `CBUT_DB` 与
`CBUT_CODEBUDDY_DIR`（见[用法](#-用法)）。

## ⚠️ 限制与路线图

- 🔌 **MCP 数据初期会很少**，除非你真的调用了 MCP 工具；该 tab 由真实调用填充（`mcp__<server>__<tool>`），也包括被延迟工具机制（`DeferExecuteTool`）包裹的调用。
- 🤖 **内部 agent**（`autoModeClassifier`、`summaryGenerator` 等）只在 OTel 轨迹里，v1 尚未接入轨迹采集。
- 🏷️ **没有 provider 字段** —— CodeBuddy 转录不记录 provider / 账号 / 站点 / endpoint，所以 Usage 页干脆没有 provider 列，而不从模型名猜测。
- 🔢 **`context tokens` 与模型 token 的区别。** 无头 `cbut stats` 会在按模型 token 总计之外，单独打印一行会话级 `context tokens`（turn-metrics `tokenDelta`）；两者刻意分开。`context tokens` 现在是幂等的 —— 重复同步与 schema 迁移不再让它膨胀。
- 🔄 同步是手动的（或可选 systemd 定时器）；没有实时 hook。

## 🧹 卸载

仓库之外 `cbut` 只碰三个地方，删掉它们就什么都不剩：

```bash
systemctl --user disable --now cbut-sync.timer 2>/dev/null          # 只在你装过它时需要
rm -f ~/.config/systemd/user/cbut-sync.service ~/.config/systemd/user/cbut-sync.timer
systemctl --user daemon-reload
rm -f ~/.local/bin/cbut                                            # install.sh 建的符号链接
rm -rf ~/.local/share/codebuddy-usage-tracker                      # SQLite 索引
```

再把仓库目录（连同它的 `.venv`）删掉就干净了。CodeBuddy 自己的文件从头到尾只被读过，
没有需要恢复的东西。🗑️

## 🙏 致谢

灵感来自 [`opencode-skill-tracker`](https://github.com/SHADE-glitch/opencode-skill-tracker) —— 它为 OpenCode 追踪同样的五类。区别在架构：OpenCode 不落盘工具日志，所以那个项目需要实时 hook 插件；CodeBuddy 会落盘，所以 `cbut` 选择读取。💡

## 📄 许可

MIT —— 见 [LICENSE](LICENSE)。© 2026 SHADE-glitch
