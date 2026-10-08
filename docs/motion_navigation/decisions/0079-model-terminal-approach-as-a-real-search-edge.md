# D079：把终点接近建成真实搜索边

日期：2026-10-08。状态：已由 [D080](0080-share-ground-tracking-policy-for-terminal-approach.md) 修订；首轮 F2-ST 因入口窗口无法证明而停止。

> **后续修订：** 末段仍是带预计 tick 成本的真实搜索边，完整区域仍由一次 A* 处理。D080 取消了“普通 `STANDING + WALK` 末段必须证明整个 `SegmentEntryWindow`”的要求。普通地面改为规划 rollout 与 FixedRoute 执行共享唯一 `GroundTrackingPolicy`，接纳时按真实状态重锚，每 tick 复核安全。起跳、下降等不可逆动作仍保留严格证明。本文中要求末段保存并原样执行同一份 `GroundTraversalPlan` 的部分不再是现行要求。

## 实施后修订

首轮保留了出口要求、typed 结果、`GroundTerminalApproach`、`SurfaceTerminalApproachEdge` 和 `terminal_approach_id` 的通用契约，但没有把 terminal edge 接入正式搜索。

实施验证了一个重要边界：不能从 `PlannerStateKey` 的宽泛 `0..ground_max` 速度范围中随意挑一个代表速度，再把这次计算当成整个入口窗口的证明。

两种尝试都不能接纳：

- `0..0.10` 格／秒虽然不是字面上的精确零速，但会让前一段近乎停稳，违反本决定；
- 约 `0.50..1.19` 格／秒的校准非零窗口能在未提交诊断中跑通 9 个历史失败，但当前验证器只计算了一个代表 `PhysicsState`。它没有覆盖窗口的速度和方向边界，也不能证明同一份计划对整个窗口成立。

因此本决定的入口要求保持不变。继续实施前，必须先满足以下至少一项：

1. 搜索状态给出已经证明的窄速度档和方向范围；
2. 地面验证器能对完整 `SegmentEntryWindow` 给出同一份可执行证明。

不能用调宽窗口、近停入口或执行阶段现场纠偏代替这项证明。首轮的 `9/9` 只作根因诊断，不改变剩余 9 项的 RED 身份。

## 要解决的问题

D078 正确解决了一个问题：`GoalState` 覆盖多个支撑面时，后台应在一次 A* 中搜索全部已知安全终点，不能让 Session 预先选死一个面。

但 D078 对“到达终点”的定义还少了一段真实运动。当前搜索在到达 `SurfaceNode` 代表点时就可以通过 `goal_test`。`GoalTerminalWitness` 只保存完成区域和一次几何扫掠。它没有证明机器人能从该节点对应的身体状态走进完成区域，也没有把这段移动的 tick 成本加入 A*。

F2-SG 的原 v8 杂乱层因此出现 `51` 项旧成功退步。零边末段修正恢复 `42` 项，剩余 `9` 项都表现为：图上选择了一个看起来较便宜的终点，执行器却无法稳定走完最后一段。

这个问题不能再靠完成区域面积阈值、候选顺序或执行阶段补路线解决。搜索、接纳和执行必须使用同一份末段证明。

## 决定

区域目标仍由一次后台 A* 搜索。通用 A* 循环不改。

每个可执行的终点接近方案建成一条真实的 `SurfaceTerminalApproachEdge`。这条边从某个 `SurfaceNode` 出发，执行经过运动计算器验证的普通地面路线，结束于原 `GoalState` 内的 `GroundCompletionRegion`。

边的起点和终点可以是同一个 `SurfaceNode`。为避免搜索把它当成零长度自环，`PlannerStateKey` 新增 `terminal_approach_id`：

- 普通图状态为 `None`；
- 走完终点接近边后，保存该方案的稳定 ID；
- `goal_test` 只接受带有效 `terminal_approach_id` 的状态；
- 同一支撑面上的不同接近方案是不同搜索状态。

边的 `cost_ticks` 必须来自正式 `GroundTraversalPlan`。A* 的 `g` 值使用完整的“图路径成本 + 终点接近成本”。不得用直线距离、固定常数或事后补成本代替。

需要移动的 `FEASIBLE` 方案必须有正 tick 成本。入口已经满足出口要求时，正式计划返回零输入 `ALREADY_SATISFIED`，其末段成本为 `0`。这不会形成零成本循环：状态键会写入 `terminal_approach_id`，终点状态不再展开另一条终点接近边。

## 与 D078 的关系

D079 修订并取代 D078 中以下部分：

- “`goal_test` 直接接受 terminal surface”改为“`goal_test` 只接受已经走过 terminal approach edge 的状态”；
- `connection_dependencies` 表达的几何连接不再足以让候选完成；
- `GoalTerminalWitness` 不再只保存支撑面、完成区域和扫掠依赖，而要保存实际选中的 `GroundTerminalApproach`；
- 搜索终点的代价必须包含正式末段的 tick 成本。

D078 的其余决定继续有效：完整区域目标、一次后台作业、一次 A*、不用虚拟汇点、不按目标面重试、UNKNOWN 不能当安全事实、旧结果按身份和世代失效。

## 正式契约

### 1. GroundTraversalExitRequirement

新增不可变 `GroundTraversalExitRequirement`。它说明末段怎样才算完成，至少保存：

- 完成区域；
- 允许的姿态和地面模式；
- 允许的水平速度范围；
- 需要时的速度方向或朝向范围；
- 支撑面身份和规则版本。

它取代当前散落的 `0.10` 格／秒硬编码终速。验证器、接纳器和执行器都读取同一份出口要求。

### 2. GroundTerminalApproach

新增不可变 `GroundTerminalApproach`，至少保存：

- 稳定的 `approach_id`；
- terminal surface 和 completion region；
- 直线或 L 形 `FixedRoute`；
- `SegmentEntryWindow`；
- `GroundTraversalExitRequirement`；
- 正式 `GroundTraversalPlan`；
- `cost_ticks`；
- 扫掠、支撑、净空、完成区域和规则依赖。

`GoalTerminalWitness` 持有一份 `GroundTerminalApproach`。它不能在接纳、执行或复核时重新生成另一条路线。

### 3. SurfaceTerminalApproachEdge

新增 `SurfaceTerminalApproachEdge`。搜索算法只依赖现有 edge 公共字段。该边提供：

- 起点和终点节点；
- `approach_id`；
- 正式 `cost_ticks`；
- 入口窗口、出口要求和资源变化；
- `GroundTerminalApproach` 及全部依赖。

这是一条保存真实计划和成本的搜索边，不是虚拟汇点。只有 `ALREADY_SATISFIED` 可以使用零输入、零 tick；需要移动的方案不能退化成零代价标记边。

## 入口状态从哪里来

首版不能把所有入口统一成静止零速。入口必须来自搜索已经证明的状态：

1. 终点节点就是规划起点时，使用请求里的精确 `entry_physics_state`；
2. 前一条边有具体预测出口时，使用该出口及其适用范围；
3. 普通 Walk 只有离散规划状态时，从方向、速度档、姿态和地面模式构造窄的 `SegmentEntryWindow`，验证器必须覆盖整个窗口；
4. 无法从这些证据得到可靠入口时，返回 typed `ENTRY_UNPROVEN`，不能默认零速，也不能让执行阶段再试。

首版只支持 `STANDING + WALK`。Sprint、Crawl、Sneak、Swimming 等入口返回 typed `ENTRY_UNPROVEN` 或现有更具体的不支持结果。本阶段不扩大动作能力。

## 候选路线和验证结果

每个末端入口最多生成三条确定性候选：

1. 从入口到完成区域参考点的直线；
2. 先走 X、再走 Z 的 L 形；
3. 先走 Z、再走 X 的 L 形。

退化为相同路线时去重。候选用正式地面计算器验证，按 `cost_ticks`、路线种类和固定坐标顺序稳定选择。不能按墙钟完成顺序选择。

验证器的正常否定必须使用类型化结果，例如：

- `FEASIBLE`；
- `ALREADY_SATISFIED`；
- `ENTRY_UNPROVEN`；
- `NO_CONTROLLABLE_ROUTE`；
- `UNKNOWN`；
- `UNSUPPORTED`；
- `TIMEOUT`。

入口已经满足 `GroundTraversalExitRequirement` 时，返回零输入的 `ALREADY_SATISFIED`。正常不可行不能抛 `ContractViolation`。契约异常只用于数据结构损坏或调用者违反接口。

## 同一份证明怎样流转

同一个 `GroundTerminalApproach` 必须贯穿：

1. A* 的终点边和 `g` 成本；
2. `GoalTerminalWitness`；
3. RouteAdmitter 的入口、身份和依赖复核；
4. `ActionRoute` 的最终普通 Walk；
5. 执行器实际采用的路线、入口窗口和出口要求；
6. 世界变化后的路线复核；
7. 完成判断和结构化证据。

任何一层重新选择参考点、重建 L 形路线、改变入口窗口或补写成本，都视为证明断链。

## 删除临时逻辑

删除 F2-SG 中为缓解回归加入的 `_MINIMUM_GOAL_COMPLETION_CONTROL_SPAN` 筛选及其后备分支。面积可以用于稳定平手，不能代替可执行性证明。

## 性能边界

终点接近求解在后台规划作业内完成。直线和两种 L 形候选数量固定，可以按入口、完成区域、规则和世界依赖缓存同一次作业内的结果。不得增加控制线程 rollout、Session 状态或跨帧等待。

继续执行以下门槛：

- 单次规划默认期限 `0.5 s`；
- 1、4、16、32 个终点分别报告枚举、末段求解、A* 和总耗时；
- 控制生产 P95 `≤ 8 ms`、P99 `≤ 15 ms`、最大值 `< 30 ms`；
- 完整夹具最大值 `< 50 ms`；
- deadline miss 为 `0`。

## 不修改的边界

- 不修改通用 A* 循环；
- 不增加 Session 状态、等待、重试或候选队列；
- 不加入路线平滑、对角搜索、潜行生产者、动态避障或未知探索；
- 不扩大到 standing + WALK 之外的入口；
- 不降低支撑、净空、碰撞、UNKNOWN、伤害或输入期限门槛；
- 不改写 F2-SG 批次 0—3 和剩余 9 项的历史结果。

## 后续

按 [F2-ST 阶段方案](../stages/F2ST-terminal-approach-proof.md)实施。F2-ST 通过后，回到 F2-SG 的 Windows 大集合、性能和 Fabric 停止点。只有 F2-SG 随后通过，F2-S 任务 4 才能继续。
