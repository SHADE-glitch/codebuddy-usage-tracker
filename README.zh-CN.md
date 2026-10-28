<p align="right"><a href="README.md">English</a> | <a href="README.zh-CN.md"><b>简体中文</b></a></p>

# 🐾 CodeBuddy 使用追踪器（`cbut`）

![CodeBuddy](https://img.shields.io/badge/CodeBuddy-reads%20local%20logs-blue?logo=robotframework&logoColor=white)
![Python](https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/storage-SQLite-003B57?logo=sqlite&logoColor=white)
![Textual](https://img.shields.io/badge/TUI-Textual-ff69b4?logo=terminal&logoColor=white)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Platform: Linux](https://img.shields.io/badge/platform-Linux-lightgrey?logo=linux&logoColor=black)
[![Repository](https://img.shields.io/badge/repository-GitHub-black?logo=github)](https://github.com/SHADE-glitch/codebuddy-usage-tracker)

**是什么** —— 一个针对 CodeBuddy Code 的被动只读追踪器：八 tab 的 TUI 与无头 JSON 报告，覆盖你用过的每一个内置工具、skill、agent、插件与 MCP 工具。

**解决什么** —— CodeBuddy 把历史存在为「恢复会话」而设计的本地日志里；`cbut` 把它们索引进一个本地 SQLite 文件，看清用过什么、装了什么。

**怎么跑** —— `./install.sh`，然后 `cbut sync`。

**去哪看** —— 见 [AGENTS.md](AGENTS.md) 与 [MAINTENANCE.md](MAINTENANCE.md)。

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
- [🤝 参与贡献](#-参与贡献)
- [📄 许可](#-许可)

## ✨ 特性

- 🧰 **八个视图，一条命令** —— Dashboard、工具、skill、agent、插件、MCP、token、用量。
- 📜 **完整历史，即刻可得** —— 回填磁盘上已有的全部会话。
- 🔑 **以名字为准，什么都不丢** —— 每个 tab 都同时列出你**用过**的和已安装的，仅以名字为主键：改名、升级或被删除的对象都保留历史，同名但来自不同归属的合并成一行。TUI 默认只显示用过的一半；按 <kbd>a</kbd>（每个 tab 独立）把「已安装但没用过」的行带回来。
- 🪶 **被动只读** —— 无 hook、无插件、不改 CodeBuddy 源码。
- 🗃️ **单个本地 SQLite 文件** —— 不联网、不上传、无遥测。
- ⚡ **快速增量同步** —— 约 300 个会话 / 420 MB 约 5 秒完成；之后只读新增字节。
- 🖥️ **TUI + 无头** —— 给人用的八 tab Textual 界面，给脚本用的 JSON 报告。
- 🔢 **真实模型 token 与 credits** —— 按模型的输入 / 输出 / 总计，以及缓存 hit / miss / write，直接来自 `providerData.rawUsage`，并带 **Provider** 列（内置，或由 `providerData.requestModelId` 派生的第三方标签）；总计优先使用 provider 自报值。provider 自己的 **`credit`** 按原值保存在独立列，与 token 并排显示在 Dashboard、Tokens、Usage、Top models、项目板和模型下钻上 —— 绝不换算成 token。
- 📊 **自然日窗口，每个 tab 独立** —— 顶部栏的范围选择器（今天 / 2 天 / 3 天 / 7 天 / 14 天 / 30 天 / 全部时间）**按 tab 各自保存**，改一个不会影响别的：实体 tab 默认 **7 天**，Dashboard 与 Usage 页默认 **今天**。Plugins 也有窗口 —— 列表是全时段，计数不是。窗口按**本地自然日**（00:00 → 当前），绝不是「当前减 N 小时」。窗口只缩小**计数**，列表始终完整。
- 📅 **自定义日期范围** —— 顶部栏的 📅 按钮（或范围列表里的 **Custom…**）打开选择器：快捷预设、可编辑的 **START TIME** / **END TIME**（日期 + 24 小时制时间），以及一个月历；‹ › 翻月，跨年时自动换年。每张卡下面一排时间快选，把当天的边界放到一次点击之外——START 提供 `00:00`/`06:00`/`12:00`/`18:00`/`23:59`，END 另有 **now**；日历下方是 24 格小时网格，改的是当前卡片的小时，所以日期和小时用同一种方式点。点卡片即把两个网格都指向它，之后点日期**或小时**都写进那张卡；↑/↓ 与滚轮按你选的步进（`1m` / `10m` / `1h`）微调当前输入框。日期与时间都可宽松输入 —— `10-1` 就是 `10/01`，`9:5 AM` 与 `21:05` 都是 `21:05`。选择按 tab 保存，顶部栏始终显示当前区间。
- 🔎 **两级下钻** —— 在 Tools / Skills / Agents / Plugins / MCP / Tokens 任意一行按 <kbd>Enter</kbd> 或双击：第一页先给出该对象**在所属 tab 窗口内**的汇总（各种次数与合计），下面是它的最近调用；在某条调用上再按 <kbd>Enter</kbd>，就看这条调用的**全部字段**，铺满整屏。
- 🔐 **只存元数据** —— 名称、耗时、状态、项目。绝不存消息正文或参数值。

## 🤔 为什么做这个

CodeBuddy 其实**已经在写**你需要的数据，只是**从不展示**。它保存了：

- 📜 **会话转录**：`~/.codebuddy/projects/<cwd>/<session>.jsonl`，每次工具调用是一条 `function_call` 记录（含名称、参数、状态、时间戳）；
- 🔭 **OpenTelemetry 轨迹**：`~/.codebuddy/traces/<pid>/trace_*.jsonl`，外加轨迹完成时整体落盘的 `trace_<id>.json`（`function` / `agent` / `generation` span）；

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

**Dashboard** 最先打开：一屏概览重要数字，让你知道该往哪里深入。tab 上方有一条共享顶部栏，其中放着范围选择器，且**每个 tab 各自保存自己的范围** —— 改一个 tab 的窗口不会影响其他 tab。实体 tab（Tools、Skills、Agents、MCP、Tokens）默认 **7 天**；Dashboard 与 Usage 页默认 **今天**。**Plugins** tab 同理：列表是全时段（已安装 ∪ 用过），而 `uses` 与 `last used` 只在它的窗口内计数。窗口只改变**计数**，绝不丢行 —— 因为每个 tab 都以名字为主键。

| | Tab | 内容 |
|---|-----|------|
| 🧭 | **Dashboard** | 四行面板，均取 Dashboard 自己的窗口：用量（请求数、credits、session 数、项目数、工具/skill/agent/MCP/插件 调用数）、token 摘要（usage 总计、输入/输出、API 总计、credits、缓存命中率）、缓存 hit/miss/write 绝对值、Top 5 模型与 Top 5 工具、运行时健康（完成/未完成/待定、平均工具耗时、去重 session/project 数）、按天调用量 sparkline，以及 **Projects 项目板**（请求 · usage 总计 · credits · session 数 · 最近使用）。项目板随内容长高，并吃掉上面几行剩下的高度，让仪表盘铺到状态行；项目多到放不下时窗格滚动，列表不会被截断 |
| 🧰 | **Tools** | 按名称统计的工具调用、完成率、平均耗时、最近使用 |
| 🎯 | **Skills** | 按名称统计的 skill 调用（跨归属合并）、最近使用 |
| 🤖 | **Agents** | 按名称统计的子代理类型、调用次数、最近使用 |
| 🧩 | **Plugins** | 每个插件 —— 已安装的**和**用过的 —— 及其 skill/agent/command；列表全时段、计数随窗口，无版本列 |
| 🔌 | **MCP** | MCP 服务器与工具、调用次数 |
| 🔢 | **Tokens** | 按模型的 **usage 总计 · credits**、请求数、输入、输出、**API 总计**、缓存 hit/miss/write、缓存命中率与最近使用 —— 本 tab 窗口内真正回答过的 (模型, provider) 各一行 |
| 📊 | **Usage** | 请求日志与汇总面板：窗口/请求/session/项目，模型 token **与 credits**，缓存明细，运行时 |

任意一行按 <kbd>Enter</kbd>（或双击）进入**两级下钻**。第一页先给出该对象**在所属 tab 窗口内**的汇总（各种次数与合计），下面是它的最近调用；在某条调用上再按 <kbd>Enter</kbd>，打开这条调用的**全部字段**（时间、项目、会话、状态、耗时、各种 id，模型响应还含 token / 缓存明细）。除 Dashboard 与 Usage 外的实体 tab 都支持 —— 工具、skill、agent、插件、MCP 工具、模型。任何 tab 在没有数据时都会显示明确的空状态提示，而不是一片空白。

**每个 tab 都列出「用过的 ∪ 已安装的」的并集，以名字为主键，按调用次数排序。** 被删除的 skill、被卸载的插件、被改名的 agent 都保留自己的行和计数；同名但来自两个插件的合并成一行。**Plugins** 统计归属到插件的每一次调用 —— 包括它的 skill、agent 和斜杠命令 —— 所以某个插件的 skill 被用过之后，不会再显示 `uses = 0`。归属是**有据可依**的：只有当插件清单把某个名字映射到该插件时，才会计入。

TUI 默认**只显示用过的那一半** —— 本 tab 窗口内 0 次调用的行会被藏起来，按 <kbd>a</kbd>（每个 tab 独立）把「已安装但没用过」的一半放回来（Tokens 本来就只统计用过的，没有这个开关）。<kbd>/</kbd> 打开过滤器，边输入边收窄，匹配的是渲染后的值（名字、分类、state 都可以）。有过滤生效时过滤栏常驻，摘要行会写明匹配数和当前排序；每次编辑都从第一条匹配开始显示，页面不会自己滑走。<kbd>Enter</kbd> 保留过滤并把焦点交回表格，<kbd>Esc</kbd> 清空。Usage 请求日志只显示最新 100 行，窗口里更多时会在说明里写明；没有翻页键。

tab 栏和页脚就是全部按键面：<kbd>q</kbd> 退出，<kbd>r</kbd> 同步并重载，<kbd>s</kbd> 循环当前 tab 的排序，<kbd>a</kbd> 切换全集，<kbd>/</kbd> 过滤，<kbd>d</kbd> 打开日期选择器，<kbd>tab</kbd>/<kbd>shift+tab</kbd> 切 tab，<kbd>Enter</kbd> 下钻，<kbd>Esc</kbd>/<kbd>q</kbd> 从详情屏返回。TUI 只在打开时同步一次，之后保持静止 —— 没有后台刷新定时器；新数据通过 <kbd>r</kbd> 进来。

每个实体 tab 的最后一列是 **state（状态）** —— 它是唯一能把「还在」和「早没了」分开的东西：`in use`（已安装，且**在本 tab 的窗口内**被调用过）、`installed`（此刻在磁盘上，但这个窗口内没调用）、`removed`（已从磁盘消失，只保留历史，显示为暗色）。标签跟随与行内 `calls` 相同的窗口，所以 0 次调用永远不会配一个 `in use`；全时段历史留在计数和 `ever used` 里，不在这个标签里。已安装这一侧是**每次同步都从磁盘重建的快照**，所以卸载一个东西之后，下一次同步它就会从「已安装」这列消失，而它已经挣到的计数一分不少。窄终端会先收起 `last used` 或 `avg`，不会收起这一列，也不会收起名字列。

**Tokens 记录的是真正回答过的模型，不是目录里有什么。** 每个至少回答过一次的 (模型, provider) 占一行。已安装包的 `product.json` 会随升级和账号地区变化，而 `~/.codebuddy/models.json` 里的是路由 id（`ark-code-latest`），它实际回答时是另一个模型（`deepseek-v4-1-flash`）—— 把两者任何一边列进来，都会产生「没人调用过」的 0 请求行。历史才是持久的一侧：从目录里消失的模型（`glm-5.1`）保留它自己挣到的每一行和每一笔合计。没有 `requestModelId` 的响应当作 `Built-in` 读，所以同一个模型不会裂成 `Built-in` 和 `-` 两行。

**Credits 是计费，不是 token。** 当响应带有 `providerData.rawUsage.credit` 时，原值存入独立列，并显示在所有能承载它的页面上、紧挨 token 合计。它绝不会被加进任何 token 计数，也不会由它反推 token。没有上报 credit 的路由显示 `-`（NULL），绝不伪造 `0`；一个聚合里若没有任何一行带过该字段，同样显示 `-`。旧索引会原地升级（schema v13）：下一次 `cbut sync` 会重读转录，为已知的响应补上 credit 列。

> **API 总计** = 有 provider 原始值时用原始值，否则用 `输入 + 输出`。
> **Usage 总计** 是仅展示的指标（`输入 + 输出 + 缓存 hit`），它**重复加了一次缓存 hit**（hit 本就包含在输入里），所以约为 API 总计的 2 倍 —— 绝不要把它当作 API 总计。
> 缓存 hit/miss/write **单独展示**，绝不加进 API 总计。响应里缺失的字段显示为 `-`，绝不伪装成 `0`。

**一个坏名字不能弄坏一行。** 工具名为空、超过 200 字符、或含空白/控制字符时，会计入 `invalid_tool_name`，不会作为一次调用存下来。这条规则之前已经入库的值会在 TUI 和无头报告里统一压成一行显示，所以模型错误生成的换行或转义序列既不能把表格行拆开，也不能移动光标。

**一条坏记录不能弄坏整次同步。** 一行能解析成 JSON 却不是记录（`null`、裸数组、字符串），或者某字段该是标量却放了列表/对象时，会计入 `unusable_record` 并跳过。代价是一条记录、在 `cbut health` 里看得见，而不是所有项目的索引一起停摆。

### 📊 Usage 页

Usage tab 仿照会话用量面板的样式，但每个数字都来自磁盘上的转录：

- **Usage 页保留自己的范围**（默认 **今天**），在 Usage tab 激活时显示在共享顶部栏里；其他每个 tab 也各自保留，互不影响。选项为 今天 / 2 天 / 3 天 / 7 天 / 14 天 / 30 天 / 全部时间 —— 以当前时刻结尾的**整段本地自然日**（00:00 → 现在），**不是**「当前减 N 小时」，也**不是** tracker 首次运行的时刻。选 **Custom…**（或 📅 按钮）可自选任意起止、精确到分。本页的 **Window / Requests** 面板显示当前范围的日期，以及该窗口的 session 数与项目数。
- **单一的 Request Logs 列表**，最近的请求在前。列顺序对齐家族布局 —— 时间 · 模型 · 提供方 · usage 总计 · **credits** · 输入 · 输出 · API 总计 · 缓存读取(hit) · 缓存未命中(miss) · 缓存创建(write) · 缓存命中率。cc-switch 的费用、耗时、HTTP 状态码列在转录里没有数据源，因此**略去而非估算**；提供方与 credit 都是转录里真实存在的字段。优先列在最左；终端太窄放不下时，列计划会**整列收起**尾部而不是把标签切一半——表格上方那行会写明收了哪几列、要多宽才能全部看到。
- **与 Tokens 相同的准确性规则** —— **API 总计** = 有 provider 原始值时用原始值，否则 `输入 + 输出`。另有一个 **Usage 总计**（`输入 + 输出 + 缓存 hit`）仅供参照，并明确标注它会重复计入缓存 hit（约 2 倍 API 总计）。缓存 hit/miss/write 绝不并入 API 总计，缺失值保持 `-`。
- **缓存命中率** = `缓存 hit / (缓存 hit + 缓存 miss + 缓存 write)` —— 即「可缓存输入」，与 cc-switch 一致；某行没有缓存数据时显示 `-`。
- **汇总面板高度受限并内部滚动**，因此下方的 Request Logs 表在小终端上也始终可见。80×24 实测：
  面板转为两列、高度仍受限，表格保留 9 行（测试把下限钉在 6）；列计划此时只显示 **12 列中的一段前缀**，
  表上方那行写明被收起的列。具体保留哪几列取决于数据 —— 宽度是按真实行内容量出来的，不是假设的；
  行数读数来自本机自己的数据库在 80×24 下的运行，你那台机器上的数字会不一样。没有任何一列被切成半截。
- **列计划覆盖每一张表，不只是被抱怨的那两张**：8 个标签页里的 8 张表（含 Dashboard 的 Projects 项目板），
  加上两个详情页（某个实体的调用历史、某个模型的响应），一共 10 张 —— 数目是从源码里数出来的，不是估的，
  而且只要有一张表没进登记表就会有用例失败。所以名字长到会把列挤出屏幕时，代价是少几列，而不是少可读性。
  名字/行键列和实体的 `state` 列是受保护的：窄终端先收起 `last used` 或 `avg`，不会让一行失去
  「它是否还装着」这个答案。下限是 2 列：连「名字 + 第一个数字」都放不进 80 格时，提示行会写
  `even these are cut`（这些也仍然被切），而不是假装表格放得下。
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

[`systemd/`](systemd) 里带了两个用户级 unit，装上后每 15 分钟跑一次 `cbut sync --quiet`。下面这四
行就是 `install.sh` 安装成功时最后打印的那段：

```bash
mkdir -p ~/.config/systemd/user
cp systemd/cbut-sync.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cbut-sync.timer
```

`Persistent=true` 会把关机期间错过的运行补上，服务以
`Nice=10` 运行 —— 不会和你的会话抢 CPU。用 `systemctl --user status cbut-sync.timer` 查看。

## 🧭 用法

```bash
cbut                     # 🖥️  交互式 TUI（八个 tab）
cbut sync                # 🔄  增量索引新日志
cbut sync --full         # ♻️  按仍在磁盘上的转录重建（见下）
cbut stats               # 📊  各类总览
cbut tools|skills|agents|plugins|mcp
cbut models              # 🔢  按模型 token：in/out/total + 缓存 hit/miss/write
cbut show tool Bash      # 🔍  某个对象的最近调用
cbut sessions            # 🗂  每个会话一行，最新在前
cbut show model <名称>   # 🔍  某个模型的最近响应
cbut recent              # 🕒  最近的工具调用
cbut inventory           # 📦  已安装清单，已用 vs 未用
cbut export              # 💾  导出所有表为 JSON
cbut health              # 🩺  数据库与数据源检查
cbut backup              # 💾  风险步骤前留快照（`sync --full` 会自己做）
cbut restore             # ⏪  列出快照；`cbut restore NAME` 回滚到某一份
cbut vacuum              # 🧹  压缩文件并报告回收量（先做快照）
cbut format              # 🧾  本工具依赖的 CodeBuddy 字段与路径
```

`cbut sync --full` 只按 `~/.codebuddy` 里仍在的转录重建索引。CodeBuddy 会清理旧转录，被清理的那份
读不回来，只存在于其中的会话、调用与响应也就从重建后的库里消失了。它会先留快照（`cbut backup`
同理，`cbut restore` 列出可用的快照）；而增量 `cbut sync` 从不丢掉已索引的东西 —— 转录全被清理的
会话会保留，只有转录被原地改写才会触发重建。

| 变量 | 默认值 | 用途 |
|---|---|---|
| `CBUT_DB` | `~/.local/share/codebuddy-usage-tracker/usage.db` | 数据库位置 |
| `CBUT_CODEBUDDY_DIR` | `~/.codebuddy` | 源日志目录 |
| `CBUT_PRODUCT_JSON` | 由 `PATH` 上的 `codebuddy` 可执行文件定位 | 已安装包的工具/agent 目录表。不设置时，`cbut` 扫 `PATH` 找到 `codebuddy`，读它上层的 `product.json`；没装 CodeBuddy 时回落到仓内的常量名单，并在 `cbut health` 里明说。钉住它可以试一份目录表，也可以读 `PATH` 找不到的那一份。 |

## ⚙️ 配置

每个可调项的默认值就是它原本的行为，而且这个文件是可选的：你不建它，`cbut` 就按文档里写的那样跑。

`~/.config/cbut/config.toml` —— 或用 `CBUT_CONFIG` 指到别处：

```toml
top_n = 5            # Dashboard 里 Top models / Top tools 的行数
log_limit = 100      # Usage 页的请求行数
detail_limit = 200   # 历史 / 模型响应详情页的行数
name_cap = 28        # 单元格最宽渲染多少字符，超出就截断
```

- **优先级：环境变量 > 文件 > 默认值。** 每个键也读 `CBUT_TOP_N`、`CBUT_LOG_LIMIT`、
  `CBUT_DETAIL_LIMIT`、`CBUT_NAME_CAP`。
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
python3 -m unittest discover -s scripts/tests     # Ran 630 tests ... OK
```

测试是纯 `unittest`（只用标准库，所以 `pytest` 也能收集）。每一份都在临时目录里建自己的
一次性数据库 —— **不会打开你真实的 `usage.db`，也不会读 `~/.codebuddy` 日志**；本仓库代码没有任何联网路径
（`test_privacy.py` 用 import 白名单守着这条）。

| 测试文件 | 用例数 | 覆盖 |
|---|---:|---|
| `test_sync.py` | 152 | 转录解析、工具归类、路由出处列（`requestModelId` → 内置 / 厂商 / 主机 / 端点）、增量同步与 `--full` 重建、崩溃恢复、未识别记录计数、整程同步锁、OTel agent 轨迹读取器（流式与完结文件按
  spanId 合并；会话关联、状态只升不降） |
| `test_traces.py` | 12 | 轨迹读取端到端：span 结果只落一次、
  `cli`/`ptc` 拒收、running 占位升级且重放不降级、
  完结文件用 `trace.sessionId` 补上流式拿不到的会话、重读幂等、
  `_run` 集成 |
| `test_hostile_inputs.py` | 4 | 给两个读取器喂生成式恶意输入：真实记录形状的每个字段轮流换成各种坏值、删掉、或整条替换（结构内部也下一层）—— 没有哪种形状能中断整次运行，读不了的计入 `unusable_record` 而不是被静默丢掉 |
| `test_tui.py` | 143 | tab 接线与报告结构（走 Textual 自带的 `run_test`）、每 tab 独立的日期选择器（日历翻月/跨年、当前目标卡片、能整行放下的预设与快选快捷项、输入框读写的 24 小时时钟、小时网格、微调步进选择器、切 tab 后顶栏跨度仍完整）、两级下钻（窗口内汇总头、Enter 打开单条全字段、长值折行不裁剪、插件路径）、窄终端布局，以及**每张表**都不再把列切半的"列计划"、单元格上限与它背后的设置项、状态栏诚实性、默认只显示用过的（每 tab `a` 开关）、`/` 名称过滤（Enter 应用、Esc 取消）、Usage 请求日志只保留最新 100 行且没有翻页键、同名模型的提供方后缀、Dashboard 的 Projects 项目板，以及路由未上报时显示 `-` 的 credits 单元格 |
| `test_cross_repo_golden.py` | 17 | 跨仓 golden fixture：一组固定输入（NULL / 部分上报 / cache 列缺失的 token 行、不同 model 拼写、本地午夜与跨小时边界、2026 年两个美国夏令时切换日）经过本仓自己的 Usage Total 表达式、窗口聚合、模型 rollup、日期时间输入与自然日窗口，钉在与 ocut、qcut 共享的权威文件上 |
| `test_usage.py` | 121 | 自然日窗口（今天 / 2 / 3 / 7 / 14 / 30 天 / 全部）与自定义范围的标签、日期时间的宽松输入归一化（24 小时显示、输入仍兼容 12 小时）、token 汇总与提供方分组、Dashboard 查询、下钻查询（窗口内按实体汇总、插件使用计数、单条调用的整行）、结构版本门禁、索引值不值得留、无界面会话列表（窗口、最新在前、limit） |
| `test_indexes.py` | 4 | 前两次切索引只能靠一次手写读数的那条规则：报告面真的会跑的每条语句都被抓下来用 `EXPLAIN QUERY PLAN` 过一遍，声明了却没有任何计划点名的索引一律失败——外加 v9 那次真正用上的结构规则：键是另一个已声明索引键的严格前缀 |
| `test_stats.py` | 40 | 无界面报告 CLI（`cbut stats`）：总览与各分类表、按模型的 token 报告及窄终端下被丢弃的 cache 列、`show`/`recent`/`inventory`/`export`、`health`（结构版本、设置行、未识别计数）、`backup`→`restore` 往返、`vacuum`（先快照、回收字节数、行数不变）、生成的 `format` 表面，以及空数据时的 `(none)` 提示 |
| `test_config.py` | 25 | 设置层：默认值没变、文件、env 优先级与取值边界、坏文件会让 `main()` 在启动 TUI 前就退出、app 真的读了哪些键，以及 `cbut health` 把每个键都报出来——环境变量会被藏起来，所以一台配置过的机器也不会把这些用例变成假失败 |
| `test_dispatcher.py` | 12 | `bin/cbut`：子命令转发、venv 解析、装不上时 help 仍然能跑 |
| `test_format_registry.py` | 10 | CodeBuddy 格式登记表与解析器双向一致 |
| `test_privacy.py` | 11 | import 白名单，**加上绕过它的调用方式**（`__import__`/`eval`/`import_module`）、任何表任何文本列都不落自由文本、不写 CodeBuddy 自己的文件 |
| `test_hermetic.py` | 13 | 一个运行时绊线：套件绝不打开真实的 `usage.db`（进程内没有 `sqlite3.connect`，也没有哪个子进程会在没钉 `CBUT_DB`/`--db` 时去解析它），也绝不读真实的 host 足迹（`~/.codebuddy`——没有 `open`、目录遍历，也没有哪个子进程会在没钉 `CBUT_CODEBUDDY_DIR` 时去读它） |
| `test_snapshots.py` | 9 | 破坏性步骤前自动留快照、列举、回滚、保留份数 |
| `test_maintenance_docs.py` | 30 | 生成的格式依赖文档不可能与代码脱节，任何文档也不许写工作流里并没有的 CI 版本、TUI 并没有的定时器、代码并不写的 schema 版本或表/视图数量、并不读取的 trace 目录，`cbut health` 的每种原因都在列它们的页面上点名，`pipeline.md` 里每条 `file:line` 引用都仍落在它点名的符号上 |
| `test_readme_counts.py` | 7 | 上面这张表就是运行器会打印的那张表，并且两份页面都把 loader 认识的每个设置项写全了 |
| `test_record_coverage.py` | 6 | 每个被覆盖的提交都记录进 `CHANGELOG.md` |
| `test_readme_bilingual.py` | 5 | 两份 README 始终是「一份文档、两种语言」，且目录里每个链接都落得到真实标题 |
| `test_conventions.py` | 9 | A 档根文件层是否在根目录齐备、AGENTS.md 是否用一行指明档位而不是抄规范正文，以及忽略清单覆盖的运行态是否真的没有被跟踪 |

`test_readme_counts.py` 负责让这张表不说谎：新增套件却不加行、或行里的数字过期，套件就会红。

## 📁 仓库结构

| 路径 | 是什么 |
|---|---|
| `bin/cbut` | bash 入口。刻意用 bash：venv 缺失或坏掉时它仍要能跑出有用的报错，所以不能依赖 Python。优先用 `./.venv/bin/python`，否则退回 `python3` |
| `install.sh` | 建 venv（有 `uv` 就用）并把 `~/.local/bin/cbut` 做成符号链接。可重复执行；目标不是符号链接时，不加 `--force` 拒绝覆盖 |
| `scripts/cbut_db.py` | SQLite 结构，以及各入口共用的查询辅助 |
| `scripts/cbut-sync.py` | 日志解析与增量索引器（`--full`、`--quiet`） |
| `scripts/cbut-stats.py` | 无头报告：`stats` · `tools` · `skills` · `agents` · `plugins` · `mcp` · `models` · `sessions` · `show` · `recent` · `inventory` · `export` · `health` · `backup` · `restore` · `vacuum` · `format` |
| `scripts/cbut-tui.py` | 八个 tab 的 Textual 界面 |
| `scripts/tests/` | 上面那 630 个用例 |
| `docs/maintenance/` | CodeBuddy 变了之后要复查什么：生成的格式依赖清单、版本兼容矩阵、体积与性能的固定度量法和基线，以及从一条日志记录到屏幕上那个数字的 `file:line` 路线图 |
| `systemd/` | 可选的 15 分钟同步 service + timer |
| `requirements.txt` | `textual>=8.2,<9` —— 只有 TUI 需要，其余全是标准库 |

沙箱安装可以改路径：入口用 `CBUT_SCRIPTS` 与 `CBUT_VENV`，数据用 `CBUT_DB` 与
`CBUT_CODEBUDDY_DIR`（见[用法](#-用法)）。

## ⚠️ 限制与路线图

- 🔌 **MCP 数据初期会很少**，除非你真的调用了 MCP 工具；该 tab 由真实调用填充（`mcp__<server>__<tool>`），也包括被延迟工具机制（`DeferExecuteTool`）包裹的调用。
- 🤖 **内部 agent**（`autoModeClassifier`、`summaryGenerator` 等）来自 `~/.codebuddy/traces/` 下的 OTel 轨迹 —— 按进程的 span 流，以及轨迹结束时写出的整轨迹文件 —— 记为 `source='span'`。
- 🏷️ **提供方是读出来的，不是猜的。** Usage 与 Tokens 两页有 Provider 列：内置显示 `Built-in`，第三方由 `providerData.requestModelId` 派生的标签 —— 已知端点 URL 显示厂商名，否则显示其主机，再否则显示端点标签。绝不从模型名猜测，因为自定义端点的别名可能长得像内置。
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

## 🤝 参与贡献

这是个人工具，没有路线图，也没有评审队列。问题与补丁仍然欢迎：开 issue 时附上 `cbut health` 的输出
和你运行的版本。提交代码前先读 [AGENTS.md](AGENTS.md)——里面的不变量（只读访问 CodeBuddy 自己的文件、
不写第二套解析、八个 tab 及其按键）是补丁必须守住的。

## 📄 许可

MIT —— 见 [LICENSE](LICENSE)。© 2026 SHADE-glitch
