# F1 后导航结构整理验收

日期：2026-10-06。当前状态：S0 已完成；S1—S4 尚未执行。

## 1. 本轮做了什么

S0 只冻结删除清单和确定性行为基线，没有修改生产导航代码。

归一化工具使用 `r28-structure-trajectory-v1`。它保留字段、列表顺序和数据类型，只把有限浮点数取到小数点后 9 位。签名覆盖：

- 任务结果、业务原因和产品指标；
- 实际输入、身体轨迹和风险记录；
- 身体交接、规划提交次数和异步监视结论；
- 后台运动作业的操作、动作、交付 tick 和结果。

随机路线、动作和后台身份会按出现顺序改成局部编号。旧证据、旧哈希和正式模拟器都没有改动。

## 2. 基线结果

基线来自干净提交 `a1649c2`。该提交只增加量尺和测试；生产代码与父提交 `2f0e46b` 相同。运行时工作区为空，生产源码指纹为 `0a795b858711b9503b08b67a1b0e1588bf3896a3151b8070f33040390a864110`。

| 集合 | 结果 | 说明 |
|---|---:|---|
| v7 产品清单 | 2,000 项；1,718 项完成 | 零异常、零安全事件；未完成项继续留在分母中 |
| 协调清单 | 1,448/1,448 通过 | 中断、重复事件和异步交付组合 |
| 补充故障 | 4/4 通过 | 不计入产品成功率 |

这组数字只说明整理前的行为。它不改写旧 v7，也不代表产品能力提升。

## 3. 冻结删除清单

静态扫描覆盖 `mc2p`、`scripts` 和 `tests`，随后逐项检查状态 owner、现有替代入口和正式链检查。只有下面三个函数同时满足“零生产引用”和“零测试引用”：

| 候选 | 当前职责已经由谁承担 | 删除后必须通过的主要检查 |
|---|---|---|
| `NavigationSession._admit_async_event` | 生命周期继续接纳同步命令；规划和运动 owner 自己核对异步身份、窗口、事实与处置 | 生命周期、R27 异步接纳、协调 1,448、补充故障 4 项 |
| `NavigationSession._cell_fact_id` | 产生阻塞事实的 owner 在使用点创建 ID | 重试账本、协调 1,448、产品 2,000 |
| `MotionRouteCoordinator._upcoming_air_index` | Walk 到空中动作由 `_upcoming_gap_index` 提前准备；连续空中动作从真实观察到的入口再求解 | 连续下降、B10 运动候选、产品 2,000 |

S1 每次只删除一个同职责小组。找不到明确替代职责，或签名出现任何差异，就恢复该小组并停止扩大。

以下入口明确不删：

- `_wait_for_active_terminal` 有一个生产调用和三条直接检查。协调 1,448 没触发它，说明需要补入口证据，不说明它是死代码。
- `PlanningCoordinator.retry_from_current` 和 `RetryLedger.begin_recovery` 仍在正式恢复链上。它们本轮未被 1,448 项触发，同样属于覆盖缺口。
- `active_motion_mailboxes` 和 `active_route` 是正式模拟监视与诊断读取接口。没有普通产品调用者是预期行为。

机器可读清单见 [`deletion-inventory.json`](../../../evidence/motion_navigation/post-f1-structure-s0/deletion-inventory.json)。

## 4. 证据

紧凑证据保存在 [`evidence/motion_navigation/post-f1-structure-s0`](../../../evidence/motion_navigation/post-f1-structure-s0/)：

- `baseline-manifest.json`：源码身份、集合数量、命令和索引哈希；
- `product-v7-index.json`：2,000 项产品签名；
- `coordination-1448-index.json`：1,448 项协调签名；
- `faults-4-index.json`：4 项补充故障签名；
- `deletion-inventory.json`：候选、保留项、owner 和检查入口。

完整原始轨迹保存在本地运行目录，可由冻结输入重新生成。Git 只保存索引、哈希和差异，避免证据继续无界增长。

## 5. 边界

- S0 没有运行 Fabric，因为正式输入、动作窗口、感知和运行入口都未改变。
- 归一化器的两次单场景重复运行签名一致。完整集合只冻结一次；S1/S2 候选必须用同一工具重新生成并比较。
- S0 没有关闭结构门槛。行数和决定分支是否下降，要等 S1/S2 完成后判断。
