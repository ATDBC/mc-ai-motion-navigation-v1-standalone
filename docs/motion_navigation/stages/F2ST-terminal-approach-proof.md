# F2-ST：终点接近正式证明方案

日期：2026-10-08。状态：批次 0 和通用类型已保留；首轮实施触发止损，F2-ST 未通过。后续方向已转入 [F2-GP](F2GP-shared-ground-tracking-policy.md)，本文不再继续实施。

> **后续修订：** [D080](../decisions/0080-share-ground-tracking-policy-for-terminal-approach.md)确认普通 `STANDING + WALK` 是可恢复地面移动，不应继续要求一份输入计划证明整个连续入口窗口。F2-ST 的 9 项 RED、typed 出口和否定结果、terminal edge 与身份契约继续作为输入；搜索、接纳、执行和复核仍未贯通，F2-ST 不能记为通过。

**目标：** 把“支撑面代表点走进目标完成区域”建成有真实路线、入口、出口、成本和依赖的搜索边，关闭 F2SG-C-05。

**依据：** [D079](../decisions/0079-model-terminal-approach-as-a-real-search-edge.md)、[F2-SG](F2SG-region-goal-multi-terminal-planning.md)和 [F2-ST 验收](../acceptance/F2ST-terminal-approach-proof.md)。

## 1. 当前基线

F2-SG 批次 0—3 保留：

- typed 区域目标已经进入正式请求；
- 后台一次 A* 可以选择完整区域内的任意已知安全支撑面；
- `GoalTerminalWitness`、身份、世代、UNKNOWN 和接纳主链已经存在；
- 通用 A* 循环没有修改；
- F2S-C-04 主例已完成。

F2-SG 仍未通过。原 v8 杂乱层先出现 `51` 项旧成功退步。零边末段修复恢复 `42` 项，剩余 `9` 项仍为 `fixed_route_stalled` 或 `fixed_route_has_no_forward_control`。

按照 fail-fast，v7、五组行为、严格动作、协调集合、D061 和 Fabric 尚未运行。F2-S 任务 4 和路线优化器继续暂停。

## 1.1 首轮实施结果与停止原因

批次 0 已冻结剩余 9 项的机器可读清单。批次 1 保留了下列通用产物：

- typed `GroundTraversalExitRequirement`；
- typed `GroundTerminalApproachResult` 和零输入 `ALREADY_SATISFIED`；
- `GroundTerminalApproach`、`SurfaceTerminalApproachEdge` 与 `PlannerStateKey.terminal_approach_id` 的不可变契约；
- 通用 A* 循环不含终点动作类型的源码门禁。

当前这些类型还没有接入正式搜索、接纳和执行链。`SurfaceTerminalApproachEdge` 只是可构造、可计价的数据结构；正式 A* 不会生成或消费这条边。

首轮连接正式链时，发现普通 Walk 入口缺少可证明的连续状态：

- `PlannerStateKey` 只保存来向和宽泛的 `0..ground_max` 速度范围；
- 把入口收紧到 `0..0.10` 格／秒会让前一段近乎停稳，违反 D079；这条做法已否决；
- 按校准参数构造约 `0.50..1.19` 格／秒的非零窗口后，9 个历史失败在一次未提交的 Windows 诊断运行中均可完成；
- 但现有 `verify_ground_traversal()` 只验证窗口内的一个代表 `PhysicsState`，不能证明窗口的低、中、高速度和方向边界都由同一份计划覆盖。

因此 `9/9` 只说明非零连续入口方向值得继续研究，不是 F2-ST 的验收结果。未提交的搜索、接纳、执行和复核改动已经撤销。原 9 项仍按 RED 保存，不回写为成功。

本轮没有修改通用 A* 循环、Session 状态、等待或重试。原 51 项、F2S-C-04、大集合、D061 和 Fabric 均未继续运行。

后续若继续 F2-ST，必须先让搜索状态提供已证明的窄速度档，或让地面验证器真正证明整个 `SegmentEntryWindow`。不能再用一个代表状态代替入口集合证明。

## 2. 实施边界

预计修改：

- `mc2p/motion_nav/known_map_planner.py`
- 地面末段验证和类型所在模块；
- `mc2p/motion_nav/route_admission.py`
- `mc2p/motion_nav/action_route.py`
- `mc2p/motion_nav/action_route_executor.py`
- `mc2p/motion_nav/route_validation.py`
- `mc2p/motion_nav/__init__.py`
- 对应 `tests/motion_nav/`、`tests/sim/`、`scripts/` 和四类正式文档。

明确不改：

- `_plain_search()`、`_resource_aware_search()` 通用循环；
- Session、PlanningCoordinator、PlannerWorker 生命周期；
- Runtime、MotorGateway、ExecutionSupervisor 和输入账本；
- 空中动作、路线平滑、潜行生产者、动态避障和未知探索。

实施前先用调用图确认准确文件。若需要越过边界，停止并新增决定。

每个批次都按 TDD 执行：先写能复现缺口或错误副本的失败检查，再做最小实现，最后只整理本批涉及的代码。上一批的定向检查和变异检查通过后才能进入下一批。

## 3. 批次 0：冻结 RED 和证明断链变异

先写失败检查，不改生产行为。

1. 固定剩余 `9` 个退步 ID、停止前轨迹、正式所选 terminal surface 和原失败原因。
2. 固定零边、单边、多边、起点即终点和多个 terminal approach 的小图。
3. 覆盖精确起点、具体预测出口和普通 Walk 窄入口窗口。
4. 覆盖 standing + WALK 正例，以及 Sprint、非 standing、入口范围不足的 typed `ENTRY_UNPROVEN`。
5. 覆盖直线、X→Z、Z→X、候选去重和平手。
6. 覆盖入口已满足出口要求的零输入 `ALREADY_SATISFIED`。
7. 覆盖 UNKNOWN、碰撞、支撑不足、材质不支持和期限耗尽的 typed 否定结果。
8. 冻结原 `51` 项、已恢复 `42` 项和 F2S-C-04，不允许改写旧证据。

变异检查至少包含：

- `goal_test` 直接接受普通 terminal surface；
- `PlannerStateKey` 忽略 `terminal_approach_id`；
- 需要移动的 terminal edge 使用零成本，或任意 terminal edge 使用固定估计成本；
- 所有入口被替换成零速；
- 继续硬编码 `0.10` 格／秒出口速度；
- 验证失败抛 `ContractViolation`；
- 接纳或执行重新生成末段；
- 漏掉入口、完成区域或路线依赖；
- ActionRoute 使用另一条路线；
- 恢复完成区域面积阈值；
- 只生成一种 L 形；
- ALREADY_SATISFIED 仍发送方向输入。

**完成条件：** 所有 RED 在当前代码上以预期原因失败；每个错误副本至少被一个行为断言发现。

## 4. 批次 1：建立出口要求和类型化验证器

1. 新增不可变 `GroundTraversalExitRequirement`，统一位置、姿态、模式、速度和需要时的方向要求。
2. 移除末段验证和完成判断中的硬编码 `0.10` 速度判断，改读出口要求。
3. 把正常否定改为 typed 结果；`ContractViolation` 只保留给坏数据和坏调用。
4. 入口已经满足出口要求时返回零输入 `ALREADY_SATISFIED`。
5. 只声明 standing + WALK。其他模式和姿态明确返回 `ENTRY_UNPROVEN` 或更具体的不支持结果。

**完成条件：** 验证器的正例、正常否定、零输入和错误数据边界全部通过；没有改变正式搜索结果。

## 5. 批次 2：求出 GroundTerminalApproach

1. 新增不可变 `GroundTerminalApproach`，绑定 route、entry window、exit requirement、正式 plan、cost ticks 和依赖。
2. 从三类入口取证：精确起点、具体预测出口、普通 Walk 窄 `SegmentEntryWindow`。不得统一成零速状态。
3. 为每个入口生成直线、X→Z、Z→X 三类候选，退化路线去重。
4. 每个候选都用正式运动计算器验证完整路线和停止尾迹。
5. 按 `cost_ticks`、候选种类和固定坐标顺序稳定选择。墙钟只能让整个求解返回 TIMEOUT。
6. 同一规划作业内可以按入口、目标区域、规则和依赖复用结果，不能跨 world revision 复用。

**完成条件：** 小图与独立穷举得到相同方案和 tick 成本；顺序扰动不改变结果；剩余 9 项都得到可执行证明或 typed 否定。

## 6. 批次 3：把末段作为真实搜索边

1. 新增 `SurfaceTerminalApproachEdge`，使用 `GroundTerminalApproach.cost_ticks`。
2. `PlannerStateKey` 新增 `terminal_approach_id`。普通状态为 `None`，走完末段后保存稳定 ID。
3. 同一 `SurfaceNode` 上的多个末段保留为不同搜索状态。
4. terminal edge 只从 `terminal_approach_id is None` 的状态展开；终点状态不再展开另一条 terminal edge。
5. `goal_test` 只接受带有效 terminal approach 的状态。
6. `FEASIBLE` 计划使用正 tick 成本；`ALREADY_SATISFIED` 使用零输入、零 tick 成本。
7. 通用 A* 仍只运行一次，循环源码门禁保持不变。
8. `GoalTerminalWitness` 改为持有搜索实际选中的 `GroundTerminalApproach`。
9. 删除 `_MINIMUM_GOAL_COMPLETION_CONTROL_SPAN` 及其后备分支。

**完成条件：** A* 比较完整 tick 成本；正式终点与独立参考搜索一致；不存在零代价自环、虚拟汇点或候选外层重试。

## 7. 批次 4：贯通接纳、ActionRoute、执行和复核

1. RouteAdmitter 复核 witness 的同一份入口窗口、出口要求、计划和依赖。
2. ActionRoute 最后一段直接使用 `GroundTerminalApproach.route` 和 `GroundTraversalPlan`，不能重建路线。
3. 执行器使用同一入口窗口启动，使用同一出口要求判断完成。
4. RouteValidator 重放同一计划。依赖变化时复核同一个 approach；不重新选择直线或 L 形。
5. 搜索、candidate、witness、ActionRoute、执行报告和验收证据记录同一个 `approach_id`。
6. 目标修订、计算世代或相关依赖变化继续让旧证明失效；无关变化不改写 approach。

**完成条件：** 用断言证明七个环节持有相同 `approach_id`、route、plan 和 cost；任一字段被替换时正式链拒绝候选。

## 8. 批次 5：Windows 回归和性能

按以下顺序运行，任一步失败即停止：

1. 类型、验证器、搜索边和证明贯通定向检查；
2. 剩余 9 项、原 51 项和 F2S-C-04；
3. F2-S v9 新增 216、原 v8 固定 104、原 v8 杂乱 1800；
4. v7 2000、F2 528、F2-R 1904、五组行为、严格动作和协调集合；
5. 完整 motion_nav 正序和逆序；
6. D061；
7. 1、4、16、32 个终点的后台性能矩阵。

门槛：

- 历史成功退步 `0`；
- 新安全事件、伤害额度超出、来源泄漏和终态潜行均为 `0`；
- 单次规划默认期限 `0.5 s`；
- 控制生产 P95/P99/max 满足 `8/15/<30 ms`；
- 完整夹具最大值 `<50 ms`；
- terminal approach 求解数、缓存命中、A* 展开数和总规划耗时必须单列。

## 9. 批次 6：Fabric 正式链

最低场景：

- 剩余 9 项中覆盖直线和两种 L 形的代表地图；
- 起点即目标支撑面和经过普通 Walk 到达目标支撑面；
- 四方向 normal／首条晚1；
- 入口速度处于窄窗口的低、中、高位置；
- 目标修订和 approach 相关世界变化；
- 无关目标面变化；
- standing + WALK 正例和至少一个 typed ENTRY_UNPROVEN 反例；
- ALREADY_SATISFIED 零输入。

必须走 profile 4、Runtime、NavigationSession、PlannerWorker、RouteAdmitter、ActionRouteExecutor 和真实输入账本。记录 `approach_id`、正式成本、命令实际应用 tick、完成状态和所有依赖变化。

## 10. 止损条件

出现以下任一情况立即停止：

- 需要修改通用 A* 循环；
- 需要新增 Session 状态、等待、重试或候选队列；
- 需要把入口统一成零速或停稳；
- 需要扩大到 standing + WALK 之外才能修复当前 9 项；
- 搜索、接纳和执行无法复用同一份 proof；
- 需要恢复面积阈值、候选顺序或场景 ID 特判；
- 两轮局部修正后仍有历史成功退步；
- 1／4／16／32 终点矩阵无法在既有期限内完成；
- Windows 控制或完整夹具性能门槛失败；
- Fabric 中计划成本、实际执行和完成要求不能对应。

停止后保留已经通过的通用类型和检查，不用扩大本阶段掩盖失败。新架构方向必须另写决定。

## 11. 关闭条件

1. 剩余 9 项恢复完成，原成功退步为 `0`；若新证据证明某项旧成功本身不安全，必须先另写决定修订基线，不能用 typed 否定直接关闭本阶段；
2. terminal approach 是保存正式计划和成本的真实搜索边；需要移动时成本为正，已经满足时为零输入、零 tick；
3. 入口不依赖统一零速，出口不依赖硬编码 `0.10`；
4. 同一 proof 贯穿搜索、接纳、ActionRoute、执行、复核和依赖；
5. 通用 A*、Session 生命周期和安全门槛没有扩大；
6. Windows、性能和 Fabric 全部通过。

F2-ST 通过后只回到 F2-SG 停止点。它不自动关闭 F2-SG、F2-S 或路线优化器。
