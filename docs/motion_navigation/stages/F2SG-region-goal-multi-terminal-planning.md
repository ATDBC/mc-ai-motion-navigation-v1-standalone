# F2-SG：区域目标多终点规划修正方案

> **实施要求：** 实施时按 RED、契约、后台搜索、Session／接纳迁移、Windows／Fabric 验收的顺序推进。每个批次先跑最小门槛，失败就停止，不扩大当前批次。

日期：2026-10-08。状态：批次 0—3 已进入正式链；Windows 回归发现终点执行证明缺口，已按 fail-fast 停止，阶段未通过。

**目标：** 修复 F2S-C-04。`GoalState` 覆盖多个安全支撑面时，后台用一次 A* 搜索任意已知安全终点，不再由 Session 提前选死一个面。

**设计：** 批次 0—3 已让正式请求携带区域目标。Session 的 `preferred_surface` 只服务本地快速直达，后台从快照建立终点集合。D079 进一步要求：`goal_test` 不能直接接受普通 terminal surface；必须先走一条有正式运动证明和成本的 terminal approach edge。

**依据：** [D078](../decisions/0078-search-goal-region-as-terminal-set.md)、[D079](../decisions/0079-model-terminal-approach-as-a-real-search-edge.md)、[F2-ST](F2ST-terminal-approach-proof.md)、[F2-S](F2S-support-region-convergence-before-route-optimization.md)和 [F2-SG 验收](../acceptance/F2SG-region-goal-multi-terminal-planning.md)。

## 1. 全局约束

- Windows 是正式开发、性能和 Fabric 验收平台；Linux 只作补充复核。
- F2、F2-R、F2-SP、F2-S 任务 0—3 和任务 4 已保存证据均只读。
- 原 `GoalState`、支撑比例 `0.5`、UNKNOWN 最坏解释、伤害额度和动作资格不放宽。
- Session 不新增状态、等待、重试、候选游标或候选队列。
- PlanningCoordinator 和 PlannerWorker 仍各自只处理一个当前规划作业。
- 不修改 `_plain_search()`、`_resource_aware_search()` 的循环、队列、资源标签或前驱回放。
- 不引入虚拟汇点，不按目标面多次提交规划。
- 不用字符串或 `None` 区分精确目标与区域目标，必须使用有类型请求。
- 采用 fail fast。当前批次之外的问题登记到缺陷台账，不顺手扩展。

## 2. 计划修改范围

预计修改：

- `mc2p/motion_nav/known_map_planner.py`
- `mc2p/motion_nav/planning_coordinator.py`
- `mc2p/motion_nav/planner_worker.py`
- `mc2p/motion_nav/navigation_session.py`
- `mc2p/motion_nav/route_admission.py`
- `mc2p/motion_nav/route_validation.py`
- `mc2p/motion_nav/__init__.py`
- 对应 `tests/motion_nav/`、`tests/sim/`、`scripts/` 和正式四类文档

明确不改：

- Runtime、MotorGateway、ExecutionSupervisor 和输入租约；
- 通用 A* 搜索循环；
- 运动计算器、动作求解器和动作接口；
- `standable_region_in_goal()` 的 F2-S 几何语义；
- 路线平滑、潜行状态机和局部跟踪策略。

若实际实现需要越过这些边界，停止本阶段并新增决定。

## 3. 批次 0：冻结 RED、基线和变异检查

**新增或修改：**

- `tests/motion_nav/test_goal_region_planning.py`
- `tests/motion_nav/test_navigation_session.py`
- `tests/motion_nav/test_route_admission.py`
- `tests/sim/f2sg_cases.py`
- `scripts/f2sg_goal_region_evidence.py`
- F2-S v9 清单的引用，不改原输入和原哈希

步骤：

1. 把 `f2r/clutter/0.2/3/9/product` 固定为主 RED：几何近邻面无路，另一个已知安全面可达；当前结果必须是 `planning_no_known_route`。
2. 增加四类同构 RED：两个面、三个面、等代价面、较远面路线更便宜。目标面顺序和 `SurfaceNodeId` 顺序都要扰动。
3. 增加本地直达正例。`preferred_surface` 可让同支撑面或短直达立即完成，但快速路径失败后必须提交完整区域目标。
4. 增加 UNKNOWN：相关未知阻塞；存在已知安全可达终点时，无关未知不阻塞；没有已知终点时返回有界信息请求。
5. 增加目标修订、请求替换、计算世代变化、世界会话变化和 witness 依赖变化。
6. 冻结单终点、4、16、32 个终点的后台耗时、展开数和控制线程准备耗时。保存目标面数量、已知安全终点数量和信息 blocker 数。
7. 写变异检查。至少覆盖：只把 preferred 面传给后台；`goal_test` 仍比较单节点；启发式只看 preferred 面；COMPLETE 缺 witness；接纳器重新选面；漏记完成区域依赖；漏记末段连接依赖；旧目标修订仍被接纳；UNKNOWN 被当成可行；Session 按面重试；虚拟汇点进入正式路径。
8. 只提交 RED、冻结输入、量尺和变异检查，不改生产行为。

**完成条件：** 主 RED 和同构 RED 失败原因一致；现有成功场景保持原结果；每个错误副本都被至少一个行为断言发现。

## 4. 批次 1：建立有类型的目标集合和终点证据

**修改：**

- `mc2p/motion_nav/known_map_planner.py`
- `mc2p/motion_nav/__init__.py`
- 对应契约测试

步骤：

1. 新增不可变 `GoalRegionPlanningRequest`，保存一份 `GoalState`。正式区域请求不得再携带一个有决定权的目标面。
2. 调整 `SurfacePlanningRequest`，用有类型字段区分区域目标和仍需保留的精确节点诊断入口。构造时必须恰有一种目标类型。
3. 新增不可变 `GoalTerminalWitness`，至少绑定 `terminal_surface`、`completion_region` 和 `connection_dependencies`。
4. 调整 `SurfaceRouteCandidate`：COMPLETE 必须带 witness；`planning_goal` 必须等于 witness 的 `terminal_surface`；非完成结果不带 witness。
5. `surface_search_need()` 改为读取有类型目标。只有已经证明身体与区域目标处在同一支撑面时，才返回本地目标检查；区域相交不等于图搜索已经完成。
6. 保留 `request_id`、序号、目标 ID／修订、世界会话、资源、伤害额度、reach policy 和 `AsyncWorkIdentity` 的现有校验。

**完成条件：** 契约错误在提交后台前被拒绝；类型测试和变异检查通过；尚未改变正式搜索结果。

## 5. 批次 2：让一次后台 A* 搜索终点集合

**修改：**

- `mc2p/motion_nav/known_map_planner.py`
- `mc2p/motion_nav/planner_worker.py`
- `tests/motion_nav/test_known_map_planning.py`
- `tests/motion_nav/test_planner_worker.py`
- `tests/motion_nav/test_goal_region_planning.py`

步骤：

1. 在不可变快照中枚举目标区域覆盖的支撑列。使用正式支撑查询、F2-S 完成区域查询和当前 ground profile，生成已知安全的 `GoalTerminalWitness`。
2. witness 的最后连接从图节点的代表点进入 `completion_region.reference_point`。只有扫掠、支撑和完成区域均为 `FEASIBLE` 时才进入终点集合。
3. UNKNOWN、未支持事实和信息 blocker 按 D078 分类。枚举与搜索共用规划期限；枚举未完成时不能返回完整无路。
4. 启发式取当前节点到所有 witness 终点列的最小代价下界。`goal_test` 判断 `state.node_id` 是否属于 witness 集合，并复用现有 `PlannerStateKey` 的运动模式、姿态和速度条件；资源与最终实际状态继续走原检查。
5. 把现有 `_plain_search()` 或 `_resource_aware_search()` 原样调用一次。不得循环候选、修改通用 A* 或添加虚拟节点。
6. 搜索完成后把实际末端节点对应的 witness 写入候选，并把 witness 依赖加入候选依赖。
7. 材料化参考图入口采用同一目标集合语义，用 Dijkstra／穷举小图独立核对最小代价和终点。
8. 性能测试覆盖 1／4／16／32 个终点和不可达目标。默认 `0.5 s`、展开数上限和 blocker 上限保持不变。

**完成条件：** 主 RED 由同一次后台作业到达可达面；参考搜索与正式 A* 的终点和代价一致；通用搜索循环源码哈希或静态门禁不变。

## 6. 批次 3：迁移 Session、协调器和接纳器

**修改：**

- `mc2p/motion_nav/navigation_session.py`
- `mc2p/motion_nav/planning_coordinator.py`
- `mc2p/motion_nav/route_admission.py`
- `mc2p/motion_nav/route_validation.py`
- 对应正式链测试

步骤：

1. `_surface_for_goal()` 的结果改为本地 `preferred_surface`。它只进入当前已有的同支撑面／短直达判断。
2. 本地快速路径未接纳时，Session 创建区域目标请求。不得把 preferred 面复制成后台唯一终点。
3. PlanningCoordinator、PlanningBasis、worker 提交、结果槽和历史记录保存区域请求及原计算世代。不得新增候选游标或重试账本。
4. 接纳先核对 request ID、目标 ID／修订、世界会话、work identity 和 geometry revision，再核对 witness。
5. 接纳只验证 witness 的末端支撑面、绑定完成区域和最后连接。验证失败时拒绝当前候选并走现有重新规划入口，不在接纳器中换面。
6. changed cells 命中路径、末端支撑、完成区域或最后连接依赖时，候选失效。无关变化不能改变 witness。
7. 活动路线、ExecutableCorridor、ActionRoute 和 validation plan 的终点统一来自 witness。不得继续读取 Session preferred 面作为完成依据。
8. 统计 Session 状态、字段、等待和重试数量。与批次 0 相比必须无新增。

**完成条件：** 主 RED 走正式 Runtime → NavigationSession → 后台 → RouteAdmitter 链完成；晚到旧结果、修订目标和相关世界变化均被拒绝；身体责任规则不变。

## 7. 批次 4：Windows 回归和性能

按以下顺序执行：

1. 先跑契约、搜索、Session 和接纳定向检查。
2. 运行 F2S-C-04 主例及所有同构 RED，确认不存在按候选重试。
3. 运行 F2-S v9 新增 216 项和原 v8 `1904` 项。旧成功退步必须为 `0`，新安全事件为 `0`。
4. 运行 F2 `528`、F2-R `1904`、v7 `2000`、五组行为集合、严格动作对照、协调集合和完整 motion_nav 正逆序。
5. 运行 D061。生产 P95/P99/max 继续满足 `8/15/<30 ms`，完整夹具最大值 `<50 ms`。
6. 报告后台目标枚举和总规划耗时。所有完成作业不超过请求的 `0.5 s`；超时必须有类型返回，不能退化成无路。

任一最小门槛失败就停止。不得靠重复运行挑选通过批次。

## 8. 批次 5：Fabric 正式链

最低场景：

- F2S-C-04 原地图四方向，normal／首条晚1；
- 两个面与三个面的目标各四方向；
- 规划途中目标修订；
- 规划完成前改变 preferred 面；
- 路线交付前改变实际 witness 的完成区域或末段连接依赖；
- 只改变另一个未选目标面的无关事实。

每个场景必须走 profile 4、Runtime、NavigationSession、后台 PlannerWorker、RouteAdmitter 和实际应用账本。要求：

- 到达原 `GoalState`，witness 与实际终点一致；
- 首条晚1由客户端账本确认；
- 相关变化使旧候选失效，无关变化不换终点；
- 零掉落、伤害、危险接触、窗口外输入和来源泄漏；
- 任务结束后输入来源全部注销。

Fabric 通过后，回到 F2-S 任务 4，从停止点继续旧大集合和任务 5。不得把 F2-SG 通过直接写成 F2-S 或路线优化器通过。

## 9. fail-fast 止损

出现以下任一情况，停止 F2-SG：

- 需要新增 Session 状态、等待、重试、候选游标或候选队列；
- 需要修改通用 A* 搜索循环或引入虚拟汇点；
- 需要按目标面提交多次后台作业；
- 需要降低支撑、UNKNOWN、碰撞、伤害或动作安全门槛；
- 目标集合顺序、结果分类或终点选择依赖墙钟；
- witness 无法覆盖末端支撑、完成区域和最后连接依赖；
- 控制生产层或完整夹具性能门槛失败；
- 两轮修复后仍出现“目标区域被某层重新压成单点”的同类缺陷。

## 10. 关闭条件

1. F2S-C-04 和同构多终点场景在一次后台作业内完成；
2. 精确目标诊断入口与正式区域目标入口均为有类型请求；
3. COMPLETE 候选和活动路线持有同一份 `GoalTerminalWitness`；
4. UNKNOWN、请求／目标修订、计算世代和世界变化门槛全部通过；
5. Session 状态、等待、重试和候选队列零增长；
6. Windows、性能和 Fabric 验收通过，原失败证据保持原样。

## 11. 实施结果与当前停止点

批次 0—3 已完成 typed 区域请求、`GoalTerminalWitness`、单次后台多终点搜索，以及 Session、PlanningCoordinator 和 RouteAdmitter 的正式链迁移。通用 `_plain_search()`、`_resource_aware_search()` 没有修改；没有新增 Session 状态、等待、重试或候选游标。

Windows 定向检查与正式主例通过。完整 `tests/motion_nav` 共运行 `1698` 项，`1697` 项通过；唯一失败是历史 F2 公开快照仍保存旧源码指纹。该失败留给公开整理，不能通过改写历史证据消除。

干净提交 `4b7adbf2` 上，v9 新增层通过安全门槛：支撑边缘 `96/96`、障碍边缘 `24/24`、跨旧切块 `24/24`，高度边缘 `32/72`；零安全事件、伤害、来源泄漏和终态潜行。原 v8 固定层为 `104/104`。原 v8 杂乱层首次完整复跑为 `1696/1800`，相对历史 `1715/1800` 有 `51` 项旧成功退步和 `32` 项旧失败转成功，因此回归门槛没有通过。

`374ed0c7` 修正了零边图路径：A* 在起点节点命中区域终点时，仍会生成走入 witness 完成区域的普通末段。对上述 `51` 项退步逐项复查，`42` 项恢复；还剩 `9` 项，统一表现为规划选中的终点几何安全，但末段没有可复用的执行证明和正式成本，最终落到 `fixed_route_stalled` 或 `fixed_route_has_no_forward_control`。

现有 `GroundTraversalPlan` 可以为这 `9` 项中的正式所选终点生成停稳后的 `5` 或 `7` tick 末段，说明复用方向可行。不过当前契约还有五个缺口：

1. 验证器只覆盖站立、非疾跑的普通 Walk，不能代表区域目标允许的全部模式和姿态；
2. 验证器需要具体 `PhysicsState`，搜索状态只有方向和速度范围；统一用零速状态会强制中途停稳，改变连续移动语义；
3. 部分不适用末段会抛出 `ground traversal trajectory is incomplete`，没有返回有类型的否定结果；
4. 斜向末段需要确定两种 L 形顺序，并把所选路线、入口窗口和全部依赖绑定到 witness；
5. 若要保持最优性，A* 必须比较图路径加末段证明的完整 tick 成本，接纳和执行也必须复用同一份证明。现有 witness 只绑定几何连接，尚未表达这些内容。

终点执行契约已经由 [D079](../decisions/0079-model-terminal-approach-as-a-real-search-edge.md) 修订，后续在独立 [F2-ST](F2ST-terminal-approach-proof.md) 阶段实施。F2-ST 使用真实 `SurfaceTerminalApproachEdge`，让正式末段证明及 tick 成本进入搜索，并让同一 proof 贯穿接纳、ActionRoute、执行和复核。不能用完成区域面积阈值、目标顺序或场景特判代替证明。

按 fail-fast，本轮没有继续 v7、五组、协调集合、D061 或 Fabric；F2-SG 保持未通过。F2-ST 通过后回到本阶段停止点继续验收，不能直接把 F2-ST 通过写成 F2-SG 通过。
