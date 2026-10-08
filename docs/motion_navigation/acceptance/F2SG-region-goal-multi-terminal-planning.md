# F2-SG：区域目标多终点规划验收

日期：2026-10-08。状态：批次 0—3 已实现；Windows v8 回归门槛未通过，D061 与 Fabric 未运行，F2-SG 未通过。

## 1. 验收对象

本阶段只验收一件事：一个 `GoalState` 覆盖多个安全支撑面时，正式后台规划在一次作业中找到任意已知安全、可达且满足目标的终点。D079 生效后，“满足目标”还要求搜索走过有正式证明和成本的 terminal approach edge。

本阶段不验收路线平滑、对角移动、潜行生产者、动态避障、未知探索或新动作。

## 2. 基线

- F2-S 任务 4 停止提交：`b6787786`。
- 主 RED：`f2r/clutter/0.2/3/9/product`。
- 现状：目标覆盖两个可行面；Session 预选的近邻面无路，另一个面可达；正式结果为 `planning_no_known_route`。
- F2-S 任务 4 的 `143/143`、区域性能、v9 新层、首次 56 项退步及 55 项尾段修复都保留，不回写。
- F2-SP 的唯一正式 D061 结果继续有效，但不能代替本阶段修改后的性能复跑。

## 3. RED 和正例矩阵

| 类别 | 最少场景 | 通过条件 |
|---|---:|---|
| 原 F2S-C-04 | 1 | 一次后台作业选择另一个可达面并完成 |
| 两个目标面 | 8 | 四方向 normal／晚1 均完成 |
| 三个目标面 | 8 | 不按输入顺序选面，路线代价与参考一致 |
| 等代价目标 | 4 | 结果稳定，平手规则固定 |
| 较远但更便宜 | 4 | 选择总路线代价更小的终点，不按代表点距离 |
| 本地快速直达 | 4 | preferred 面可直达时不启动后台；失败时完整区域进入后台 |
| 相关 UNKNOWN | 4 | 返回有界信息需要，不编造终点 |
| 无关 UNKNOWN | 4 | 已知安全路线可以完成，不等待无关事实 |
| 目标／请求修订 | 6 | 旧结果全部失效，新结果只绑定当前修订 |
| 世界变化 | 8 | witness 依赖变化失效，无关目标面变化不影响已绑定路线 |

测试输入、目标区域、支撑面顺序、世界事实和预期分类在生产修改前冻结。

## 4. 契约门槛

- 正式 Session 请求使用 `GoalRegionPlanningRequest`，不再把 preferred 面当作后台唯一终点。
- 旧精确节点入口必须有单独类型，不能用 `None`、字符串或空 tuple 表示模式。
- `SurfaceRouteCandidate.status == COMPLETE` 时必须有 `GoalTerminalWitness`。
- `candidate.planning_goal == witness.terminal_surface`。
- `completion_region` 完全位于原 `GoalState.region` 内，面积为正，净空和支撑满足 F2-S 规则。
- `connection_dependencies` 对应从末端图点进入完成区域的实际连接。
- 非完成结果不带 witness。

## 5. 搜索门槛

- `_plain_search()` 和 `_resource_aware_search()` 通用循环不修改。
- 正式规划只提交一次后台作业，只调用一次通用搜索入口。
- `goal_test` 判断终点集合及 `PlannerStateKey` 的运动模式、姿态和速度条件；不得新增虚拟节点或零代价汇点边。
- 启发式不得高估到最近可行 witness 的代价。
- 小图用独立 Dijkstra／穷举验证最终终点和代价。
- 目标面顺序、支撑列顺序和节点 ID 扰动后，结果仍按固定平手规则一致。
- 资源、伤害额度和动作资格与单终点规划逐项一致。

## 6. UNKNOWN 与状态分类

| 条件 | 预期结果 |
|---|---|
| 已有已知安全可达终点，其他目标区域未知 | 可以完成已知路线 |
| 没有已知安全终点，未知可能形成终点 | `NO_KNOWN_ROUTE` + `PlanningInformationNeed` |
| 终点已知但路线前沿未知 | `NO_KNOWN_ROUTE` + 路线 blocker |
| 完整范围内全部合法终点不可达 | `NO_ROUTE_WITHIN_COMPLETE_SCOPE` |
| 材质、形状或目标条件不支持 | `UNSUPPORTED` |
| 枚举或搜索期限耗尽 | `TIMEOUT` |

blocker 仍受现有数量上限约束。部分枚举结果不能用来声明完整无路。

## 7. 身份和接纳门槛

接纳必须逐项核对：

- request ID 和请求序号；
- goal ID 和 goal revision；
- `AsyncWorkIdentity`／计算世代；
- 世界会话和 geometry revision；
- candidate 的最终 terminal surface；
- completion region；
- terminal support、completion 和 connection dependencies。

接纳失败只能拒绝当前候选并走现有重新规划入口。接纳器不能替换 witness，也不能转去另一个目标面。

## 8. 变异检查

以下错误副本必须失败：

1. Session 只把 preferred 面提交给后台；
2. `goal_test` 仍比较单一 `request.goal`；
3. 启发式只指向 preferred 面并提前结束；
4. COMPLETE 候选缺少 witness；
5. candidate 终点与 witness 不同；
6. 漏掉 completion dependencies；
7. 漏掉 connection dependencies；
8. 接纳时重新选择区域或目标面；
9. 目标修订后仍接纳旧 witness；
10. UNKNOWN 被当成安全终点；
11. Session 循环候选并多次提交 worker；
12. 正式图加入虚拟汇点。

## 9. 性能

Windows 正式报告：

- 目标面数量、已知安全 witness 数量；
- 终点枚举耗时；
- A* 展开数和搜索耗时；
- 单次作业总耗时；
- 控制生产层和完整夹具耗时。

门槛：

- 控制生产 P95 `≤ 8 ms`；
- 控制生产 P99 `≤ 15 ms`；
- 控制生产最大值 `< 30 ms`；
- 完整夹具最大值 `< 50 ms`；
- 完成的后台作业不超过请求 `maximum_planning_seconds`，默认 `0.5 s`；
- deadline miss 为 `0`。

1、4、16、32 个终点分别报告。性能失败时不得通过改变候选顺序、删掉目标面或提高门槛修正。

## 10. Windows 回归

按 fail-fast 顺序运行：

1. 契约与变异检查；
2. 主 RED 和同构多终点矩阵；
3. F2-S v9 新增 216 和原 v8 1904；
4. F2 528、F2-R 1904、v7 2000；
5. 五组行为、严格动作、协调集合；
6. 完整 motion_nav 正序和逆序；
7. D061。

历史已成功任务退步必须为 `0`。新安全事件、伤害额度超出、来源泄漏和终态潜行必须为 `0`。

## 11. Fabric

Fabric 至少覆盖：

- 原 F2S-C-04 四方向 normal／首条晚1；
- 两面和三面区域目标；
- 规划中目标修订；
- preferred 面变化但区域目标不变；
- witness 相关世界变化；
- 非 witness 目标面变化。

必须走 profile 4、Runtime、NavigationSession、PlannerWorker、RouteAdmitter 和真实输入账本。每个试次保存请求、候选、witness、实际终点、控制应用 tick、变化格和最终结果。

## 12. 关闭条件

- 主 RED 与同构多终点矩阵全部符合冻结结果；
- 单次后台搜索、typed request 和 witness 契约通过；
- UNKNOWN、身份、代次、接纳和世界变化门槛通过；
- Session 状态、等待、重试和候选队列零增长；
- Windows、性能和 Fabric 通过；
- F2-S 任务 4 可以从原停止点继续。

F2-SG 关闭只表示区域目标不再被提前压成单个面，不代表 F2-S、路线优化器或潜行生产者已经完成。

## 13. 首轮实施验收与 fail-fast 结论

### 13.1 已通过的部分

- typed 区域请求、精确目标入口和 COMPLETE witness 契约已进入正式代码；
- 正式 Session 请求把完整 `GoalState` 交给一次后台 A*，preferred 面只保留本地快速用途；
- F2S-C-04 主例能选择另一个可达面并完成；
- 通用 A* 循环源码门禁通过，没有虚拟汇点或按候选重复提交 worker；
- 完整运动导航检查为 `1697/1698`，唯一失败是历史公开 F2 源码指纹；
- 干净 `4b7adbf2` 的 v9 新增 `216` 项通过零安全事件门槛，分层完成数为 `96/96`、`24/24`、`24/24`、`32/72`；
- 同一提交的原 v8 固定层为 `104/104`。

### 13.2 阻塞回归

原 v8 杂乱层首次完整复跑为 `1696/1800`。与历史逐 ID 比较：

- 旧成功转失败：`51`；
- 旧失败转成功：`32`；
- 新安全事件：`0`。

零边图路径原先会返回 COMPLETE，却没有生成节点内走入完成区域的动作。`374ed0c7` 为它补上经过同一连接查询的普通末段。对 `51` 项退步逐项复查后，`42` 项恢复，剩余 `9` 项没有关闭。

剩余 `9` 项不是九种独立错误。A* 当前只计算到 `SurfaceNode` 代表点的图成本，witness 只证明代表点到完成区域的扫掠和支撑安全。它不能证明地面控制器能按同一末段进入该区域，也没有把末段耗时计入终点比较。规划因此会选择图路径较短、但执行阶段无法稳定完成的边缘终点。

### 13.3 GroundTraversalPlan 可行性验证

诊断把 `surface.position → completion reference` 拆成两种确定性 L 形普通路线，并从停稳状态调用现有 `verify_ground_traversal()`。剩余问题中被正式 A* 选中的终点都能得到 `5` 或 `7` tick 的证明。这说明后续可以让 witness 持有末段证明、成本和依赖，并让接纳及执行复用。

该诊断不能直接成为正式实现，原因有五项：模式／姿态覆盖不足；具体物理状态与搜索状态精度不一致；零速证明会引入强制停稳；部分否定结果仍抛契约异常；witness 还没有表达末段路线、入口窗口和完整成本。只有补齐这些契约，A* 才能比较“图路径成本 + 实际末段证明成本”，并保持一次搜索和最优性语义。

### 13.4 未运行范围

按照 fail-fast，发现旧成功退步后没有继续运行 v7 `2000`、五组行为、严格动作、协调集合、D061 或 Fabric。F2-SG 不能关闭，F2-S 任务 4 继续暂停。历史失败和旧证据没有改写。

### 13.5 后续修订

[D079](../decisions/0079-model-terminal-approach-as-a-real-search-edge.md) 已取代 D078 中“terminal surface 加几何 connection witness 即可完成”的部分。独立 [F2-ST 验收](F2ST-terminal-approach-proof.md) 将验证真实末段边、入口窗口、出口要求、正式成本和同一 proof 的完整流转。

F2-ST 尚未实施。本节只记录后续门槛，不改变本验收的 `51`、`42` 和剩余 `9` 项，也不补写未运行的大集合、性能或 Fabric 结果。
