# F2-ST 终点接近正式证明验收

日期：2026-10-08。状态：通用类型定向检查通过；入口窗口证明不足，F2-ST 按止损规则未通过。后续按 [D080](../decisions/0080-share-ground-tracking-policy-for-terminal-approach.md) 和 [F2-GP 验收](F2GP-shared-ground-tracking-policy.md)推进，不回写本验收结果。

本验收只判断一件事：A* 选择的最后一段，是否已经用正式运动计算器证明，并且同一份证明是否一直用到真实执行结束。

## 1. 当前 RED

| 集合 | 当前结果 | 本阶段用途 |
|---|---:|---|
| F2-SG 原 v8 杂乱层 | `1696/1800` | 保留完整首次复跑结果 |
| 旧成功退步 | `51` | 不改写历史 |
| 零边末段修复后恢复 | `42` | 防止再次退步 |
| 仍未关闭 | `9` | F2-ST 主 RED |
| F2S-C-04 | 主例完成 | 保持区域目标多终点能力 |
| 完整 motion_nav | `1697/1698` | 唯一失败为历史公开 F2 源码指纹 |

剩余 9 项的稳定 ID、终点支撑面、失败原因和停止前轨迹必须保存在机器可读清单中。不能只在 Markdown 复制一份 ID。

## 1.1 首轮验收结果

| 项目 | 结果 | 结论 |
|---|---:|---|
| RED 清单 | 9 项已写入机器可读清单 | 保留原失败，不回写 |
| typed 契约与地面验证定向检查 | `11/11` | 通过，只覆盖通用类型与单个具体入口状态 |
| 近停入口 `0..0.10` 格／秒 | 否决 | 会让前一段近乎停稳，违反 D079 |
| 非零诊断入口 | 约 `0.50..1.19` 格／秒，未提交诊断为 `9/9` | 只验证一个代表状态，不能作为窗口证明或回归通过 |
| 搜索、接纳、执行、复核贯通 | 未验收 | 实现已撤销 |
| 原 51 项、F2S-C-04 和其他 Windows 集合 | 未运行 | fail-fast 停止 |
| D061、性能矩阵、Fabric | 未运行 | fail-fast 停止 |

保留的 `SurfaceTerminalApproachEdge` 和 `terminal_approach_id` 目前只有数据契约。正式搜索尚未生成 terminal edge，`GoalTerminalWitness`、ActionRoute、执行器和 RouteValidator 也没有持有该证明。

停止原因不是“非零入口一定不可行”。问题是证据不足：当前 `PlannerStateKey` 的速度范围过宽，现有验证器又只从一个 `PhysicsState` 生成一份计划。它没有证明同一 `SegmentEntryWindow` 的速度下界、中点、上界和方向边界都适用。按照入口验收的冻结门槛，这一项必须判失败。

## 2. 契约验收

必须证明：

- `GroundTraversalExitRequirement` 明确保存完成区域、姿态、模式和速度要求；
- `GroundTerminalApproach` 保存 route、entry window、exit requirement、plan、cost 和依赖；
- `SurfaceTerminalApproachEdge.cost_ticks` 等于 plan 的正式 tick 数；
- `PlannerStateKey.terminal_approach_id` 区分同一节点上的不同接近方案；
- `GoalTerminalWitness` 持有搜索选中的 approach；
- COMPLETE 候选没有 approach 时构造失败；
- 非完成结果不携带过期 approach。

## 3. 入口和出口验收

入口分三类独立测试：

1. 请求提供的精确 `PhysicsState`；
2. 前一动作的具体预测出口；
3. standing + WALK 的窄 `SegmentEntryWindow`。

每类都覆盖速度范围上下界、方向边界和边界外一项。边界外必须返回 typed `ENTRY_UNPROVEN`，不能自动改为零速。

出口覆盖：

- 已进入完成区域且满足要求：`ALREADY_SATISFIED`，零输入；
- 位置满足但速度不满足：继续合法制动或 typed 否定；
- 速度满足但姿态或模式不满足：typed 否定；
- 删除旧 `0.10` 常数后，改变 `GroundTraversalExitRequirement` 会改变验证结果。

## 4. 路线候选验收

至少覆盖：

- 直线唯一可行；
- X→Z 唯一可行；
- Z→X 唯一可行；
- 三条都可行时选择 tick 成本最低者；
- 成本相同时按冻结顺序选择；
- 退化路线去重；
- UNKNOWN、碰撞、支撑不足、材质不支持和期限耗尽；
- 正常不可行不抛 `ContractViolation`。

独立穷举必须与正式求解器得到相同的 route、cost ticks 和结果分类。

## 5. 搜索验收

- 需要移动的 terminal approach 是正成本 edge，`ALREADY_SATISFIED` 是零输入、零 tick edge；
- 走边前后的 `SurfaceNode` 可以相同，但 `PlannerStateKey` 必须不同；
- terminal edge 只从非终点状态展开，零 tick 结果不能形成循环；
- `goal_test` 不接受 `terminal_approach_id is None`；
- A* 的 `total_cost_ticks` 包含末段成本；
- 同一支撑面的多个 approach 不会互相覆盖；
- 正式 A* 与独立 Dijkstra／穷举得到相同终点、approach 和总成本；
- 通用 `_plain_search()`、`_resource_aware_search()` 循环源码门禁不变；
- 没有虚拟汇点和按候选面重试。

## 6. 证明贯通验收

从搜索到执行记录以下字段：

- `approach_id`；
- route 点列；
- entry window；
- exit requirement；
- plan ID 或不可变内容哈希；
- cost ticks；
- dependencies。

搜索结果、candidate、witness、RouteAdmitter、ActionRoute、ActionRouteExecutor、RouteValidator 和最终报告必须逐项一致。

修改其中任一层的 approach ID、route、plan、cost 或依赖，正式链必须拒绝。接纳器和复核器不得悄悄重算一条新路线。

## 7. 变异验收

下列错误副本必须全部被发现：

1. 普通 terminal surface 直接通过 `goal_test`；
2. state key 不含 approach ID；
3. 需要移动的 terminal edge 为零成本；
4. terminal edge 使用估计成本，或给 `ALREADY_SATISFIED` 虚构一个等待 tick；
5. 所有入口改成零速；
6. 出口继续硬编码 `0.10`；
7. typed 否定改为异常；
8. 只生成一种 L 形；
9. 接纳器重建路线；
10. ActionRoute 改用另一条路线；
11. 复核重新选 approach；
12. 漏掉 approach 依赖；
13. ALREADY_SATISFIED 发送非中性输入；
14. 恢复完成区域面积阈值。

## 8. Windows 回归

按以下顺序运行：

1. 定向契约、验证器、搜索和证明贯通；
2. 主 9 项、原 51 项、F2S-C-04；
3. v9 216、v8 固定 104、v8 杂乱 1800；
4. v7 2000、F2 528、F2-R 1904；
5. 五组行为、严格动作和协调集合；
6. 完整 motion_nav 正序和逆序；
7. D061。

任一步失败就停止。旧成功退步必须为 `0`。新安全事件、伤害额度超出、来源泄漏和终态潜行必须为 `0`。

## 9. 性能验收

分别报告 1、4、16、32 个 terminal surface 的：

- approach 候选数和正式求解数；
- 同作业缓存命中数；
- 末段求解 P50/P95/P99/max；
- A* 展开数；
- 作业总耗时和结果；
- deadline miss。

门槛：

- 默认规划期限 `0.5 s`；
- 控制生产 P95 `≤8 ms`、P99 `≤15 ms`、最大值 `<30 ms`；
- 完整夹具最大值 `<50 ms`；
- deadline miss 为 `0`；
- 结果不依赖墙钟完成顺序。

## 10. Fabric 验收

最低覆盖：

- 直线、X→Z、Z→X 三类 approach；
- 起点即终点支撑面和经过普通 Walk 到达；
- 四方向 normal／首条晚1；
- 三档合法入口速度；
- 目标修订；
- approach 相关依赖变化和无关目标面变化；
- standing + WALK 正例；
- 不支持入口的 typed 反例；
- ALREADY_SATISFIED 零输入。

必须保存真实观察、后台候选、witness、route、plan、输入实际应用 tick、完成状态和来源注销记录。

## 11. 通过条件

- 剩余 9 项全部恢复完成，原成功退步为 `0`；
- 末段成本进入 A*；
- 入口不统一停稳；
- typed 否定和零输入完成成立；
- 同一 proof 贯穿搜索到复核；
- Windows、性能和 Fabric 门槛全部通过；
- 通用 A*、Session 生命周期和安全门槛不变。

通过后只解除 F2-SG 的当前阻塞。F2-SG 仍需从原停止点完成自己的旧大集合、D061 和 Fabric，不能直接继承 F2-ST 的阶段结论。
