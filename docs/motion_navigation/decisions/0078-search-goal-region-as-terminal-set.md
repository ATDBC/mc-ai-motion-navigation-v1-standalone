# D078：把目标区域作为终点集合交给一次后台搜索

日期：2026-10-08。状态：批次 0—3 已实施；终点执行契约由 D079 修订，阶段尚未通过。

> **后续修订：** 本决定关于完整区域目标、一次后台 A*、不用虚拟汇点和不按目标面重试的结论继续有效。原文中“`goal_test` 直接接受 terminal surface”以及只用 `connection_dependencies` 证明最后连接的设计不足，已由 [D079](0079-model-terminal-approach-as-a-real-search-edge.md) 取代。终点现在必须通过一条有正式 `GroundTraversalPlan` 和 tick 成本的 `SurfaceTerminalApproachEdge`。

## 要解决的问题

`GoalState.region` 表示机器人可以停在一个区域里的任意安全位置。当前正式链却在提交规划前调用 `NavigationSession._surface_for_goal()`，把这个区域压成一个 `SurfaceNodeId`。后台规划器只搜索这个节点。

这会丢掉合法终点。F2-S 任务 4 的 `f2r/clutter/0.2/3/9/product` 同时覆盖两个安全支撑面。代表点更近的面没有已知路线，另一个面可达。Session 选择前者后，后台只能返回 `planning_no_known_route`。

该问题不能靠调整排序解决。换一种排序只能改变哪一张地图失败，仍然会在搜索前丢掉其余合法终点。

## 决定

正式已知图规划把 `GoalState` 作为终点集合交给后台。后台在一次作业中搜索任意一个已知、安全、满足目标条件的终点。

现有普通 A* 和资源标签搜索已经接受 `goal_test`。本轮只为它们提供“当前状态是否属于终点集合”的判断，并把启发式改成到终点集合的安全下界。通用搜索循环不改。

Session 仍可计算一个 `preferred_surface`，但它只服务同支撑面或短直达的快速路径。快速路径无法证明时，后台请求必须保留完整目标区域。`preferred_surface` 不限制后台候选，不参与无路结论，也不能写入候选接纳身份。

## 与现有决定的关系

- D063 中“规划前必须得到单一目标 `SurfaceNodeId`”只继续适用于精确节点请求和本地快速路径。区域目标改为在后台快照中取得完整终点集合。相关 UNKNOWN 仍然返回信息需要，安全门槛不变。
- D077 的有界选面和性能结果继续用于 `preferred_surface`。它不再定义后台规划的唯一终点。
- D076 的完成区域初选与绑定区域复核继续生效，但初选对象改为 A* 实际到达的 terminal surface。

## 请求与结果契约

正式表面规划请求新增有类型的区域目标：

```text
GoalRegionPlanningRequest
    goal_state: GoalState
```

`SurfacePlanningRequest` 的正式 Session 路径保存该对象，不再要求一个预选 `goal: SurfaceNodeId`。旧的精确节点请求可以暂时留在 reference、bridge 和针对性测试入口，但必须使用单独的有类型请求，不能与区域请求靠 `None` 或字符串区分。

后台完成结果必须带：

```text
GoalTerminalWitness
    terminal_surface: SurfaceNodeId
    completion_region: GroundCompletionRegion
    connection_dependencies: tuple[BlockPos, ...]
```

其中：

- `terminal_surface` 是本次 A* 实际到达的末端支撑面；
- `completion_region` 是该面与原 `GoalState.region` 相交后得到的安全完成区域；
- `connection_dependencies` 证明从末端图点进入完成区域的最后一段连接；完成区域自己的依赖继续保存在 `completion_region.dependencies`；
- `SurfaceRouteCandidate.planning_goal` 必须来自 witness，不能继续复制 Session 的提示面；
- `COMPLETE` 结果必须有 witness，非完成结果不得伪造 witness。

接纳器只复核 witness 绑定的终点。它不能重新挑一个更优面，也不能借当前地图把旧候选换到另一个面。

## 后台搜索规则

后台先在不可变快照中枚举目标区域内的已知支撑面。每个面只有在完成区域、目标支撑、姿态、运动模式、材质和最后连接均可证明时，才进入终点集合。

启发式取当前节点到所有终点列的最小可证明代价下界。`goal_test` 同时判断节点是否属于终点集合，以及现有 `PlannerStateKey` 的运动模式、姿态和速度区间是否满足目标条件。资源下限继续由资源标签搜索核对；执行结束时仍由现有完成判断核对实际速度、朝向和资源。平手、动作边代价和前驱回放继续使用现有实现。

搜索只提交一次作业。Session 不按候选面循环提交请求，PlanningCoordinator 不维护候选序号，PlannerWorker 不做“失败后换下一个面”的外层重试。

## UNKNOWN 和失败分类

UNKNOWN 不能当成空气、支撑或可达终点。

- 找到一条通往已知安全终点的完整路线时，可以返回该路线；目标区域其他未知部分不阻塞这个存在性结论。
- 没有找到已知安全终点时，若未知事实可能产生终点或路线，返回 `NO_KNOWN_ROUTE` 和现有有界 `PlanningInformationNeed`。
- 目标范围与搜索范围均完整，且所有合法终点都已知不可达时，才能返回 `NO_ROUTE_WITHIN_COMPLETE_SCOPE`。
- 不支持的材质、形状或目标条件继续返回有类型的 `UNSUPPORTED`，不能归入 UNKNOWN。
- 目标枚举、完成区域查询和搜索共用同一规划期限。期限耗尽返回 `TIMEOUT`，不能用已枚举的部分终点得出无路结论。

## 身份、接纳和世界变化

现有 `request_id`、请求序号、`goal_id`、`goal_revision`、世界会话、`AsyncWorkIdentity` 和计算世代继续生效。目标修订后到达的旧结果整体失效，即使 witness 指向的地形仍安全。

候选依赖必须包含：

- 实际路径节点和边；
- witness 的末端支撑事实；
- `completion_region.dependencies`；
- `connection_dependencies`。

相关世界事实变化后，接纳必须拒绝或复核绑定 witness。无关区域变化不能让候选换终点。活动路线开始后继续遵守现有身体责任和安全收尾规则。

## 性能边界

目标集合枚举和 A* 都在后台进程中完成。控制线程只保留当前的本地直达提示，不同步枚举整个终点集合。

继续使用：

- 控制生产层 P95 `≤ 8 ms`、P99 `≤ 15 ms`、最大值 `< 30 ms`；
- 完整夹具最大值 `< 50 ms`；
- 单次规划默认期限 `0.5 s`；
- 现有展开数上限和信息 blocker 上限。

新增报告目标面数量、可证明终点数量、终点枚举耗时、展开数和总规划耗时。不得用墙钟截止顺序改变终点集合或结果；墙钟只允许让整个作业返回 `TIMEOUT`。

## 不采用的做法

### 不采用虚拟汇点

虚拟汇点会引入没有实际几何、动作和资源语义的合成边，还会让最终支撑面及依赖难以回溯。现有 `goal_test` 已经能表达多个终点，没有必要改图。

### 不采用 Session 候选重试

逐个目标面提交后台会增加等待、取消、世代和重试组合，也可能先接受一条更差路线。一次搜索可以直接比较所有已知安全终点。

### 不把 `preferred_surface` 当作后台偏好

本地提示只用于快速直达。后台搜索按真实路线代价选择终点，不能因为 Session 的几何近邻提示放弃更可达或更便宜的面。

## 边界

本决定不加入路线平滑、对角边、动态避障、未知探索、新运动方式或通用 A* 重写。F2-S 的潜行生产者和路线优化器仍保持未交付。

## 进入实施的条件

按 [F2-SG 方案](../stages/F2SG-region-goal-multi-terminal-planning.md)先冻结 RED、接口和性能基线。任何实现若要求新增 Session 状态、等待、重试，修改通用 A* 循环，或通过调排序掩盖 F2S-C-04，立即停止并回到设计阶段。
