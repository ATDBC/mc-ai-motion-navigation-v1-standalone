# D063：首次完整请求目标面可确定的信息

日期：2026-10-05。状态：A—E 已完成，独立复审确认 P0／P1／P2 为零。随后只运行一次新的 `straight_2_0`，该场未通过，原始结果保留；下一步见 [D064](0064-use-proved-ground-direct-before-background-planning.md)。D062 与 D063 之间没有运行 Fabric。

## 要解决的问题

`NavigationSession._surface_for_goal()` 现在按目标区域中的列查询支撑面。`query_support_surfaces()` 一旦发现第一组未知格就返回 `NEEDS_INFORMATION`。这保证了 UNKNOWN 不会被当成空气，但同一个目标面查询下一步还能确定的缺失格没有一起返回。

D060-F 的首次目标面查询因此分了两轮。第一轮返回 25 格；回复写入 `WorldKnowledge` 后，同一个查询又发现 73 格。两组共 98 格，小于 Observation V3 每帧 128 格的正式上限，却仍用了两次请求和回复。规划与控制本身没有在这里失败，额外一轮信息等待把首次合法 Walk 推迟到 tick 12，并留下稳定窗开头的追赶尾部。

D063 只收拢这项目标面信息请求。它不放宽 UNKNOWN，不预读路径，不改变规划图，也不改 Walk 速度、跟随距离、修订节流或统计口径。

## 决定

当前 goal revision 第一次查询目标面时，返回“本次目标面几何查询已经能够确定的完整有界 missing 集”。本批场景应一次返回 25+73=98 格。

“完整”只指当前目标区域内，同一次支撑面和站位几何枚举能确定的未知格。它不包含规划器尚未展开的节点、路线候选、搜索边界外的格或下一阶段才会访问的图前沿。

“有界”受 Observation V3 的 128 格上限和现有 Session 请求优先级约束。查询可以保存超过 128 格的完整 missing 集，但每个 control frame 发出的 `ObservationRequestV3.air_positions` 仍不得超过 128。

## 怎样取得完整 missing

`support_surfaces` 继续拥有支撑面候选枚举。实现应复用现有候选列、feet-y 范围、碰撞 owner、支撑面和站位候选规则，在同一次只读查询中收集 missing。不能在 Session 中硬编码一个长方体，也不能根据本批的 25、73 或 98 写死坐标数量。

查询按下面的边界工作：

1. 先枚举当前 GoalState 覆盖的目标列和合法 feet-y 范围；
2. 收集支撑面 owner 查询能够确定的 missing；
3. 对还不能形成正式 `SupportSurface`、但其候选几何已由同一次枚举确定的列，继续收集该候选的净空、支撑和站位检查所需 missing；
4. 返回排序、去重、不可变的完整集合；
5. 任何事实仍为 UNKNOWN 时，结果保持 `NEEDS_INFORMATION`。missing 集只表示要观察什么，不证明这些格是空气。

这不是第二套表面查询。现有 `query_support_surfaces()` 和站位选择仍给出最终 `FEASIBLE`、`BLOCKED`、`UNSUPPORTED` 或 `NEEDS_INFORMATION`。D063 只让首次 `NEEDS_INFORMATION` 携带同一查询已知的完整问题集合。

## 请求预算和状态归属

每帧总预算仍为 128 格。请求合并顺序保持现状：运动残差、活动探边、离地前检查、`_snapshot_missing`、活动路线依赖。已知格复查的 20 tick 节流、未知格的 retry 节流、route dependency 的 16 格上限和既有分页方式都不变。

D063 不增加 owner、等待账本、队列或缓存。状态继续由现有对象负责：

- 目标面查询返回完整 missing；
- `NavigationSession._snapshot_missing` 保存当前 goal revision 的待观察集合；
- `InformationAcquisitionState` 使用现有 wait、视角和可见性状态；
- `NavigationObservationAdapter.air_request()` 继续执行每帧上限、节流和分页；
- `WorldKnowledge` 继续保存 Observation V3 写入的真实事实。

missing 超过 128 时，Session 保留完整集合，并按既有优先级和稳定排序逐页请求。收到一页只移除已由当前 world 事实回答的格；剩余页继续由同一信息 owner 处理。不得因为第一页装不下就截断查询结论、启动规划或新建另一个 owner。

## 目标修订、旧观察和取消

新 goal revision 被接受时，重新查询新目标面，并原子替换 `_snapshot_missing`。旧目标的未回答格不能与新目标合并，也不能占用新目标的完成条件。

旧 Observation V3 仍按正式顺序写入 `WorldKnowledge`。这些真实事实以后可以被新查询读取，但旧回复、旧 notification 或旧否定结果不能直接完成新 revision 的目标面查询。新 revision 必须用自己的 GoalState 在当前 world view 上重新计算。

取消或终态沿现有生命周期清除请求和等待。取消后到达的旧观察只更新 world，不得重启目标查询、规划或输入。

目标突然跳到另一区域时也使用同一规则：新目标 missing 替换旧集合；旧区域晚到的回复不能让新目标进入 planning。

## 规划边界保持不变

D063 不修改 planner snapshot、搜索范围、图扩展、候选评分、RouteAdmitter 或两项规划工作的身份和期限。目标面尚未得到合法 `SurfaceNodeId` 时仍处于 `NEEDS_INFORMATION`，不能提前提交 planning work。

目标面得到正式事实后，Session 才按现有顺序查询当前身体支撑、建立 `SurfacePlanningRequest` 并进入 PlanningCoordinator。D063 不能借“完整 missing”向路径方向扩张，也不能把规划前沿未知格塞进目标面请求。

## 红测和反例

### A. Observation V3 正式链

用正式 Observation V3、NavigationObservationAdapter、NavigationSession 和 RuntimeNavigationDriver 冻结 D060-F 的起点与目标面：

1. 首次 control frame 返回同一目标查询可确定的 98 个 missing，包含原 25 格和 73 格，顺序稳定、无重复；
2. 一次合法回复写入全部 98 格后，下一控制帧进入现有 planning 流程，不再发第二轮目标面查询；
3. 回复缺格、状态 unavailable 或 Observation 顺序不合法时，不能进入 planning；
4. 测试读取正式 control proposal 和 Session report，不调用 Session 私有字段改变状态。

### B. 容量、可见性和生命周期

- 构造同一次目标面查询可确定的 missing 超过 128 的场景。每帧 payload≤128，分页无丢失、无重复 owner，全部事实到齐前不规划；
- `occluded`、`out_of_range` 和 `unavailable` 继续使用现有有界信息处理。它们不能被计成已回答，也不能靠转头之外的旁路授权规划；
- 新 revision 把目标跳到不相交区域时，`_snapshot_missing` 只保留新集合；旧 Observation 和旧 information notification 只更新 world／按旧身份退休，不能完成新查询；
- cancel、close 和业务终态后，不再发目标面请求或 planning submission；晚到回复不能恢复任务；
- 已知 `BLOCKED`、`UNSUPPORTED` 和真正无可站立面继续走原 typed 失败出口，不变成信息等待。

### C. 边界反例

- 目标面 98 格之外、但位于可能路径方向的 UNKNOWN 不得进入本次 missing；
- 尚未展开的 planner graph frontier 不得进入目标面请求；
- 目标区域、feet-y、方块形状或碰撞 owner 变化时，候选必须来自 `support_surfaces` 的正式枚举，不能用固定盒子保持旧坐标；
- 第一页已有 128 格时，残差、探边和离地前检查仍按现有优先级占用预算；目标面页只能使用剩余额度。

## 产品与性能门槛

D063 完成组件和模拟回归后，只允许一次新的 `straight_2_0` fail-fast。它必须同时满足：

- 首个合法 Walk 不晚于场景 tick 11；
- 目标面信息请求／回复轮次为 1；
- 首个 Walk 前 planning submission 不超过 5；
- 全场 planning submission／accepted revision 不劣于原 20／16，比例仍≤1.25；
- revision response P95≤5 tick；
- stable excess P95≤1.5 格；
- 原安全、终态、source release、host time attribution 和 cleanup 门槛全部通过。

这组门槛不修改稳定窗口。原 D060-F 的 1.541509 格失败继续保留。D063 只有在正式链把首次 Walk 提前且原量尺自然通过时才能关闭该问题。

性能回归沿用 D061：prepare P95≤8 ms、P99≤15 ms、max<30 ms；retained 正式控制路径 max<50 ms、deadline miss=0、minimum slack>0。还要记录每帧 query payload 数量、分页轮次和目标面 missing 总量，证明 payload≤128，且完整收集没有把目标面查询变成无界扫描。

## 实施顺序

### D063-A：冻结红测

先落 Observation V3 的 98 格、一次回复、>128 分页、occluded／outside、目标跳变、旧通知和取消测试。修前必须稳定复现两轮目标面信息请求和 tick 12 首次 Walk。

### D063-B：目标面查询

在 `support_surfaces` 现有枚举中补完整有界 missing 收集。Session 只消费 typed 查询结果并替换当前 revision 的 `_snapshot_missing`；不新增 owner 或平行查询器。

### D063-C：组合与回归

运行目标面、Observation V3、Session、PlanningCoordinator、F1-C 和 D062 聚焦检查，再运行完整 motion_nav。已有失败逐项对照；任何新增失败或旧失败签名变化先查根因。

### D063-D：D061 性能

先跑低成本工具和短参数检查。若查询 payload 或正式控制路径发生可测变化，再从干净实现提交建立一次不可变性能证据；失败即停止，不反复运行。

### D063-E：独立复审

复审核对 UNKNOWN、查询边界、128 格上限、目标 revision、旧通知、取消、规划身份和性能。P0／P1／P2 清零后，才允许运行一次新的 `straight_2_0`。

### D063-F：单场 Fabric fail-fast

只运行 `straight_2_0`。任一门槛失败就停止，不运行另外四场。通过后仍先提交证据并复审，再决定 F1-D 剩余场景。

## 不做的事

- 不把 UNKNOWN 当空气；
- 不扩规划图前沿或预取整条路线；
- 不新增信息 owner、线程、缓存或恢复预算；
- 不修改 128 格上限、请求优先级、节流或分页规则；
- 不修改 Walk／follow 控制、稳定窗或统计算法；
- 不在 D062 复审完成前实施 D063；
- 不在 D062 与 D063 之间运行 Fabric。

## 2026-10-05 实施结果

生产修改保持在两个既有入口内：

- `query_support_surfaces(..., collect_complete_missing=True)` 在同一次候选枚举中继续收集已知支撑候选的净空和支撑 missing；默认参数仍保持原查询行为；
- `NavigationSession._surface_for_goal()` 只在目标面查询时启用完整 missing，并继续使用现有 `_snapshot_missing`、请求优先级、节流和分页。

没有新增 owner、缓存、线程、规划分支或 UNKNOWN 的通行权限。目标面事实不完整时，Session 仍为 `NEEDS_INFORMATION`，不提交 planning work，也不发移动输入。

直接证据如下：

- D060-F 等价目标首次返回排序去重的 98 格；一次合法回复后进入现有 planning，只再请求 5 格真实图前沿，不再出现第二轮 73 格目标面等待；
- missing 超过 128 时按页请求，残差信息先占预算；所有目标面事实到齐前不规划；
- `occluded` 和 `outside_view` 保持 UNKNOWN，五 tick 内不重复请求，到期后重试；
- 目标跳变后，新 revision 的 missing 替换旧集合；旧回复只更新世界事实；取消后的晚到回复不能恢复任务；
- 正式 `PlayerRuntimeV1 -> RuntimeNavigationDriver -> KnownWorldFollowDriver` 回放使用 D060-F 的 6 格起距、66 tick 移动期和 11 tick 目标输入循环。目标面等待只有一轮，首条实际 Walk 位于场景 tick 6；此前 planning submission 为 5，图前沿信息请求为 2 轮；全场规划比例 18／15=1.2，revision response P95 为 1 tick。

回放保留完整 27 个稳定窗口样本，但不把模拟器的跟随距离当成 Fabric 通过证据。它的 stable excess P95 为 1.534463 格、task recovery 为 1，分别留给下一次单场 Fabric 和正式记录判断；不再用原 4 格短场景的单样本 P95=0 宣称产品门槛通过。

D063 专项 8/8，D059—D063 证明合同 39/39，支撑面、表面规划、Session 和运行时交接相关检查通过。完整 motion_nav 共 1,437 项，1,432 项通过；失败仍是此前登记的同 5 项，名称和断言没有变化。D061 的低成本性能工具检查通过，本轮没有刷新长 Session 证据，也没有运行 Fabric。

独立复审先发现原回放使用 4 格起距且稳定窗口只有一个样本。`6d8baca` 已改成 D060-F 的 6 格起距、66 tick 移动期和 11 tick 目标输入循环，并直接统计每个 revision 的目标面请求、图前沿请求和首个 Walk 前 planning submission。复审确认整改后的 P0／P1／P2 为零。

复审后只运行一次新的 `straight_2_0`：

`artifacts/f1-known-world-following/20261005T1630101063860Z-straight-2-0-d063/`

规划比例 19／17=1.117647、revision response P95=5 tick、task recovery=0，安全、终态、source、host、time 和 cleanup 均通过。target 实测均速 1.773723 格／秒，低于 1.8 下限；stable excess mean／P95 为 3.302394／4.000742 格，未通过。场景 tick 14 才出现第一条 Walk；执行期间三次因新目标缺事实先退场后取证。该批失败，另外四场没有运行。
