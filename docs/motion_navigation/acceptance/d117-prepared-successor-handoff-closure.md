# D117 prepared 后继交接收口验收

日期：2026-10-18。状态：**PASSED／SIGNED OFF（组件范围）。**

## 1. 验收范围

D117 只验证 prepared 后继链的组件行为。生产默认路径仍使用普通 `SOLVE`。本阶段不运行 Fabric，不新增运动能力。

## 2. 修复前事实

| 编号 | 事实 | 状态 |
|---|---|---|
| D117-A | prepared admission 缺少异步 `apply` 事件 | RED |
| D117-B | prepared 提交推进执行器但未推进 tracker，也未完成普通动作收尾 | RED |
| D117-C | 1 ULP 状态差使 prepared 控制器进入 `RECOVERING` | RED |
| D117-D | 快照锁忙被记成永久缺少信息，封存异常被吞掉 | RED |
| D117-E | 准备失败和安全落地后的恢复没有有界出口 | RED |

修复前 Windows 只读复核还确认：现行生产两跳成功，普通 `SOLVE` 为 2 次，prepared 入口调用为 0；持续快照失败时从 tick 20 等到 tick 80，零伤害但没有终态。

## 3. 实际结果

### 3.1 R0／R1

R0 已把 prepared 边界等待和恢复等待接入正式组件时间线。监视器只读取 typed 字段。R1 已让普通和 prepared 结果共用 `try_apply -> install -> admission -> retire`。prepared 生命周期为 `begin／apply／finish`，并按完整异步身份归因。

### 3.2 R2

`ActiveRouteTracker` 现在先生成绑定 tracker token、epoch、route identity、动作、观察序列和进度证据的本地计划，再一次提交。普通 `_advance()` 和 prepared 激活共用执行器的动作完成计划，统一结算伤害、清理已完成动作的候选和边界，并推进动作下标。

prepared 提交先同时检查 tracker 与 executor 计划。检查完成后，两项本地提交之间没有外部回调、registry 调用或查询。候选释放在 registry 锁退出后执行。下列定向检查通过：

- tracker 计划只检查不写状态，且只能提交一次；
- tracker 计划作废时，executor 的动作下标、控制器、伤害、候选和边界不变；
- executor 计划作废时，tracker 不变；
- 真实旧动作候选被释放，新 handle 转为 `EXECUTING`；
- 普通与 prepared 完成对预计伤害都只记一次，已经记账的动作不重复累计；
- D116 代表链中 executor 与 tracker 同时推进到第二个严格动作，下一帧不再出现 `route_identity_changed`。

当前直接结果：D102 action handoff 与 D116 timeline 共 55 项通过；D058 runtime validation 104 项通过；D102 registry lifecycle 40 项通过。D102 runtime activation 全模块初次运行有 1 项热路径递归校验失败；候选清理改用 registry 已有的直接 candidate 释放后，该失败项单独复跑通过。

### 3.3 R3

verified start 的数值比较现在只有一个实现。位置、每 tick 速度和 yaw 继续使用原有 `1e-7` 容差；其他浮点物理量继续使用原有 4 ULP 范围；movement tick、姿态、着地、碰撞和其他离散状态仍精确匹配。prepared staging 只把证明入口状态与锚点的比较改用该函数；当前观察与锚点、异步作业身份和 basis 检查没有放宽。

冻结的 1 ULP 速度差通过正式 `staging -> approval -> activation -> application -> decide` 链，D116 代表链在 80 tick 内完成两跳，零恢复、零伤害、零违规。`1e-6` 真实速度偏差仍被拒绝；movement tick、姿态、着地和碰撞变化仍得到 `ENTRY_MISMATCH`。独立恢复旧逐位相等后，代表链在 tick 21 进入 `RECOVERING`，随后触发 I23，证明成功变化来自 R3。R3 定向和相邻检查共 190 项通过，独立复核的 handoff／timeline 59 项通过。

### 3.4 R4

快照借用现在返回 `READY／BUSY／CAPACITY`。旧的可选 version 接口继续保留。快照发布新增 `READY／CAPACITY／INVALID` 结果，旧 bool 接口继续保留；容量不足与身份、内容、摘要或 revision 冲突不再混为一类。receiver 将 `INVALID` 交给三个消费者的 `INTERNAL_ERROR`，将容量不足交给各自的永久不支持结果。

短暂 `BUSY` 和 lease 的瞬时 `NOT_READY` 会保留原记录、身份和期限，在下一个 receiver 周期重试。receiver 不增加 sleep、墙钟判断或失败计数。取消或关闭发生在借用期间时，返回结果不会复活已退役工作。D116 的 typed 注入中，`BUSY` 1／3／5 次都完成第二跳，零伤害、零违规；取消跨周期重试的 mutation 会让正式链停在边界并触发 I23。

Windows 定向结果：快照、receiver 和 D108 共 84 项通过；D102 交接、Runtime 激活与 D116 共 96 项通过；新增定向与 package boundary 12 项通过。发布分类修正后的直接集合 87 项通过；反向把 `INVALID` 误报为 `CAPACITY` 时，12 个子场景全部失败。

### 3.5 R5

未激活的 prepared 后继现在可以按完整身份撤销。协调器会一起清理 stream、signature、submission、selection、已知 work window 和 registry candidate；执行器只撤销对应的后继要求与 pending boundary。过期判断继续使用已有异步窗口、movement tick 期限和输入流期限，没有新建计数器或读取墙钟。

撤销后，当前动作通过既有普通 `SOLVE` 继续。已经激活且仍在空中的控制器继续持有身体责任；代表链安全着地并稳定后，转入既有 `INPUT_LOST` grounded recovery。`EXECUTING` 或 `SAFE_TAIL` 不会被伪造释放。持续快照竞争、容量拒绝、内部错误和过期都能到达完成或类型化终态，不再无限停在 prepared 边界。

R5 直接与相邻检查 271／271 通过。独立审查还确认：删掉未激活过期处理会重现无界等待；删掉着地交接会重现 `RECOVERING` 无上界。

### 3.6 端到端与完整回归

| 检查 | 结果 |
|---|---|
| D116 代表链，空闲重复 | 30／30 完成，完成 tick 均为 36 |
| D116 代表链，受控同核负载 | 30／30 完成，完成 tick 均为 36 |
| 快照 `BUSY` 1／3／5 次 | 全部完成 |
| 快照持续 `BUSY` | 撤销 prepared 后由普通 `SOLVE` 完成 |
| 容量不足／内容无效 | 分别得到永久不支持／内部错误 |
| 超出数值等价范围的状态偏差 | 保持拒绝 |
| Windows 源码树正序 | 195／195 模块，2473 项通过 |
| Windows 源码树逆序 | 195／195 模块，2473 项通过 |
| 独立公开快照正序 | 194／194 模块，2449 项通过 |
| 独立公开快照逆序 | 194／194 模块，2449 项通过 |

完整分片均为零失败、错误、超时和跳过。第一次源码树正序运行暴露两项发布元数据问题：新增 typed enum 没有进入结构量尺，以及历史 D115／D116 语义哈希被错误地拿当前文件比较。这两项只改量尺与导出检查；修正后重新运行正序、逆序全部通过。行为模块在第一次运行中已经全部通过。

公开快照第一次全量还暴露一项测试时序假设：持续 `BUSY` 时，组件时钟可能在 receiver 开始 prepared 作业前就到达普通回退。此时合法生命周期为空；如果作业已经开始，则必须是 `begin／finish`。两种调度都不得出现 `apply`。旧断言固定要求 `begin／finish` 和动作边界，因此在同一份代码上会偶发失败。修正后的用例连续 20／20 通过。公开快照正序在更严格的第一版修正下已全绿；最终字节重新导出并完成逆序全量。生产代码没有因该测试修正变化。

## 4. 证据边界

- 三方提供的 30 次 Linux 对照只用于确认根因；正式签署使用本项目 Windows 重复结果；
- Windows 已复核生产入口计数、持续快照失败、1 ULP 状态差、空闲与同核负载重复以及完整正逆分片；
- D117 尚未运行 Fabric；
- prepared 自动准备仍未接回生产默认路径；
- 历史 D115／D116 失败证据不回写。
- 紧凑结果见 `evidence/motion_navigation/d117-prepared-handoff-v1/acceptance-summary.json`；完整分片输出只保留在本地 `.tmp`，不把可重建的大型日志提交进仓库。
