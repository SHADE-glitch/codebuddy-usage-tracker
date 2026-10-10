# MAINTENANCE.md — codebuddy-usage-tracker

维护流程文件。本文件是**路由**：长篇维护资料在
[`docs/maintenance/`](docs/maintenance/README.md)（随仓库提交），本仓的工作规则在
[`AGENTS.md`](AGENTS.md)。不要把它们的表格抄到这里——被抄写的事实正是会漂移的东西。

唯一要紧的维护风险是 **CodeBuddy 改变日志格式**：字段改名后读出来是 `NULL`，记录类型改名后
会被静默地归为未认领，面板清空、`cbut sync` 仍打印 "done"、CI 依旧全绿。检查清单见
[`docs/maintenance/codebuddy-format.md`](docs/maintenance/codebuddy-format.md)。

## 验证层级
- **L0** —— `python3 -m unittest discover -s scripts/tests`（临时数据库，不碰宿主）。
- **L1** —— 仓外的临时库（`--db /tmp/…`），喂真实或拷贝来的日志。
- **L2** —— 真实的 `~/.codebuddy` 日志与真实 `usage.db`；需要本机所有者同意。

## CI
`.github/workflows/ci.yml` 在 Python 3.11 / 3.14 矩阵上跑离线层。见 `AGENTS.md` § CI。

## 资产
| 文档 | 什么时候读 |
|---|---|
| [`codebuddy-format.md`](docs/maintenance/codebuddy-format.md) | CodeBuddy 发了新版本，或某面板不再显示它以前显示的东西 |
| [`compatibility.md`](docs/maintenance/compatibility.md) | 决定声明支持什么，或某个版本串需要更新 |
| [`measurements.md`](docs/maintenance/measurements.md) | 判断体积或性能变化 |
