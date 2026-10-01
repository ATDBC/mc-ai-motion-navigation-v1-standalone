# R25：规划行为所有权与信息阻塞证据方案

状态：R25-1 至 R25-5 已完成，组件、正式模拟、profile 4 Fabric 代表场景和公开独立导出均通过；导航协调 S5 重新关闭。

## 1. 本轮要解决什么

第二十四轮复审发现两个 P1：

1. 快照只返回坐标顺序中的第一个相关未知格。这个格子能证明范围不完整，却不一定阻挡当前路线。路径外、不可观察的未知格可能让会话超时，而路径上的缺失信息从未被请求。
2. 路线依赖变化导致候选被拒绝后，会话直接重发请求，没有登记 `RetryCause.DEPENDENCY`。地形持续变化时，请求代次可以无限增加。

两个问题都出在规划行为的所有权不完整。R25 不再分别补分支，而是把快照、后台请求、候选接纳和规划重试放进同一个协调器。

## 2. 不在本轮做什么

- 不修改运动计算器、支撑面几何、视觉空气和真实输入出口。
- 不加入 Swim、Climb、SprintJump 或新的路径评分。
- 不拆 `ActionRouteExecutor`，不建立通用运动插件系统。
- 不物理拆分 `known_map_planner.py` 中的 legacy 与 surface planner。
- 不一次拆完 `NavigationSession`。本轮只迁移规划行为，现有信息获取执行和身体交接继续沿用。

这些工作应在 S5 重新关闭、行为基线冻结后分别实施。

## 3. 职责边界

### 3.1 KnownMapSnapshotBuilder

继续负责：

- 按 `maximum_cells` 分片复制世界事实；
- 生成与活动世界脱离的不可变快照；
- 判断声明范围是否完整；
- 发现构建期间可能发生的世界版本变化。

不再负责：

- 决定下一步观察哪个格子；
- 按坐标顺序返回任意一个未知格；
- 判断哪个未知事实阻挡当前目标。

未知格仍通过快照中的 `CellKnowledge.UNKNOWN` 表达。构建器可以保存有界诊断计数，但正式规划不能把这个计数当作信息请求。区块段版本只表示“可能变化”，不能直接等同于一次规划失败。

### 3.2 表面规划器

表面规划器只报告端点解析和搜索真实遇到的阻塞事实。新增最小契约：

```python
class PlanningBlockerKind(StrEnum):
    SUPPORT = "support"
    CLEARANCE = "clearance"
    SWEEP = "sweep"
    ACTION_PRECONDITION = "action_precondition"

class PlanningFrontierKind(StrEnum):
    START = "start"
    GOAL = "goal"
    SEARCH_EDGE = "search_edge"
    ACTION = "action"

@dataclass(frozen=True, slots=True)
class PlanningBlocker:
    position: BlockPos
    kind: PlanningBlockerKind
    requirement_key: str
    frontier_kind: PlanningFrontierKind
    frontier_key: str

@dataclass(frozen=True, slots=True)
class PlanningInformationNeed:
    world_session_id: str
    snapshot_id: str
    request_id: str
    goal_id: str
    goal_revision: int
    selection_revision: int
    blockers: tuple[PlanningBlocker, ...]
    truncated: bool
```

约束：

- 起点或终点在正式搜索前缺少事实，也属于本次规划真实遇到的阻塞；
- 搜索开始后，只收集已经到达的列、连接或动作查询返回的未知依赖；
- 不扫描与端点解析和搜索无关的全部未知格；
- 阻塞键由坐标、精确查询要求和前沿位置共同组成。`requirement_key` 要能区分同一坐标上不同高度、姿态或扫掠范围的查询，不能只按坐标或粗粒度种类合并；
- 以到达前沿的搜索优先级、搜索发现顺序和阻塞键形成稳定次序；
- 去重后最多保存 128 个阻塞事实，协调器每批交给信息流程 64 个；
- 搜索前沿超过 128 个事实时设置 `truncated=True`，不能冒充已经穷尽搜索前沿；
- 已找到完整路线时，不因其他未知格返回信息请求。

`PlanningInformationNeed` 是一次不可变快照上的选择结果。收到观察后，必须按原阻塞键确认所需事实已经从 `UNKNOWN` 变为可用于对应查询的事实。仅仅发现同一坐标出现了新记录，不算取得进展。

一批阻塞事实表示可选的信息获取机会，不要求全部取得。现有信息流程要把选择结果报告为 `ACQUIRED`、`CURRENTLY_UNAVAILABLE` 或 `TIMED_OUT`：

- `ACQUIRED` 结束本批次，并用该阻塞键产生一次进展许可；
- `CURRENTLY_UNAVAILABLE` 不让整个批次立即失败，协调器继续尝试本批其他事实；
- 本批没有可取得事实时，协调器从同一份不可变结果中取下一批；
- 同一结果最多处理 128 个稳定阻塞键。128 个都不可获取、结果仍为 `truncated=True` 时返回 `information_frontier_truncated`，不能无界重跑搜索；达到信息期限或任务重试上限时也要有类型结束；
- 同一批次或同一阻塞键超时后，不能每帧重新开始。

因此，即使前 64 个事实当前不可观察，第 65 个可获取事实也能在固定期限内被尝试。R25 不新增探索移动；是否可获取只按现有转头、等待和探边能力判断。

`SurfaceRouteCandidate` 增加可选的 `information_need`。结果优先级固定为：

1. 找到路线时返回路线；
2. 没有路线，但端点或已到达前沿存在未知阻塞时返回信息需要；
3. 没有未知阻塞，且确实依赖未实现能力时返回 `UNSUPPORTED`；
4. 完整范围内既无未知阻塞也无可行路线时返回 `NO_ROUTE_WITHIN_COMPLETE_SCOPE`。

旁支出现 `UNSUPPORTED` 不能吞掉目标方向上的未知阻塞。

legacy 整数图入口继续保留，但不得继续消费 `SnapshotBuildProgress.missing_cells` 作为观察目标。R25-1 要么给它提供同样的端点／搜索前沿信息出口，要么明确限定它只接受完整已知快照并返回有类型的 `NEEDS_COMPLETE_SNAPSHOT`。不能静默改变公开入口。

### 3.3 PlanningCoordinator

新增 `mc2p/motion_nav/planning_coordinator.py`。它拥有 `PlanningPipelineState` 的行为，不复制任务状态或身体控制状态。

```python
class PlanningUpdateKind(StrEnum):
    RUNNING = "running"
    ROUTE_READY = "route_ready"
    NEEDS_INFORMATION = "needs_information"
    REQUIRES_INTERACTION = "requires_interaction"
    FAILED = "failed"

@dataclass(frozen=True, slots=True)
class PlanningUpdate:
    kind: PlanningUpdateKind
    world_session_id: str
    attempt_id: str
    request_id: str
    goal_revision: int
    route: ActiveRoute | None = None
    information_need: PlanningInformationNeed | None = None
    interaction: RequiredWorldInteraction | None = None
    failure: PlanningFailure | None = None
```

协调器提供的窄接口：

```python
begin(request, frame, *, permit, state_anchor,
      remaining_damage_budget) -> None
observe_changes(changed_cells) -> None
advance(frame, *, state_anchor, admission_evidence,
        remaining_damage_budget) -> PlanningUpdate
report_information_outcome(selection_revision, blocker_key, outcome) -> None
cancel(request_id) -> None
diagnostics(frame) -> PlanningWorkDiagnostics
```

它负责：

- 创建和重启分片快照；
- 建立规划尝试，消费一次性许可；
- 提交后台请求并记录身份、运动 tick 和单调时钟期限；
- 丢弃旧请求、旧目标、旧世界和过期结果；
- 调用 `RouteAdmitter`；
- 把无路线候选中的 `PlanningInformationNeed` 返回给会话；
- 在一批事实不可获取时，有界选择下一批；
- 保证一次尝试只登记一个终结结果；
- 为内部失败调用任务级 `RetryLedger`；
- 失败耗尽时返回类型化 `FAILED`，不直接释放身体控制者。

它不负责：

- 改写 `NavigationSessionState`；
- 创建或停止探边控制器；
- 决定旧路线何时安全释放；
- 直接写 Runtime 输入。

### 3.4 NavigationSession 与其他所有者

会话仍是公开门面和事件路由器。它负责：

- `start / update_goal / cancel / close / observe / propose`；
- 把目标和世界变化送给 `PlanningCoordinator`；
- 把 `NEEDS_INFORMATION` 交给现有信息获取流程；
- 把 `ROUTE_READY` 交给 `ExecutionSupervisor`；
- 把 `FAILED` 通过现有安全停止和任务终态流程发布；
- 汇总 `NavigationSessionReport`。

以下行为不得继续由会话直接实现：

- 操作 `_snapshot_builder`；
- 写 `PlanningPipelineState.submitted_*` 和结果期限；
- 直接轮询规划 worker；
- 在候选拒绝后直接调用 `_reissue_request_from_current()`；
- 绕过一次性许可创建下一次规划尝试。

`GoalRequestLedger` 继续唯一拥有任务、目标修订和全局请求代次。协调器只能通过窄接口申请并绑定请求，不能复制计数。`replacement_failure`、planner worker 生命周期、取消后的迟到结果和搭桥工作点的二次提交，都要在迁移表中指定唯一所有者。

公开 `NavigationSessionPort`、请求类型和 Runtime 驱动接口保持兼容。

## 4. 规划尝试、许可和重试

### 4.1 规划尝试与一次性许可

规划尝试在开始快照前建立，不依赖候选是否存在：

```text
planning/{task_id}/{goal_revision}/{attempt_sequence}
```

一次尝试覆盖快照构建、一次后台提交、候选消费和接纳。它只能以路线、信息需要、交互要求、被新任务取代、取消或一种失败结束一次。迟到结果和重复轮询不能为同一尝试登记第二个失败。

初始尝试之后，每次新尝试必须消费一个 `PlanningAttemptPermit`：

| 请求来源 | 授权依据 | 是否消耗失败额度 |
|---|---|---:|
| 新任务或新的目标修订 | 新的目标修订身份 | 否；也不清空已有任务级额度 |
| 当前阻塞事实已取得 | 对应阻塞键的 `ACQUIRED` 进展 | 否；只恢复本轮额度，不恢复任务总额度 |
| 世界交互已经由后续观察确认 | 有身份的交互事务结果 | 否；同一事务只能授权一次 |
| 已验证路线前段完成或安全交接后重锚 | 新鲜交接证据或路线进度身份 | 否；同一进度只能授权一次 |
| 依赖变化、规划超时或 worker 失败 | 首次登记且尚未消费的 `RetryLedger.RETRY` | 是 |
| 提案未获仲裁，但目标、身体和世界依据未变 | 无 | 不新建尝试；继续保留或重新验证现有结果 |

许可只能使用一次，并绑定任务、目标修订和产生它的事件身份。目标修订属于合法任务更新，不是假失败，也不是任务进度；旧目标许可不能用于新目标。

### 4.2 快照变化如何处理

区块段几何版本只用于发现“可能发生变化”。协调器要按规划范围和事实变化分类：

- 变化在声明快照范围外：当前构建继续，不登记失败；
- 范围内的 `UNKNOWN -> 已知`：属于正常增加知识。在当前尝试期限内重启快照，不登记失败；同一观察不能反复重启；
- 已复制的相关已知事实发生改变，可能推翻路线依据：结束当前尝试，按 `DEPENDENCY` 登记一次；
- 正常增加知识持续发生：仍受当前尝试墙钟期限和任务总期限约束，不能无限延长；期限到达后按规划超时登记。

实现可以继续用区块段版本快速发现变化，但必须比较声明范围和事实变化后再决定处置。同区块段、规划范围外的变化不能耗尽重试。

### 4.3 进入共享账本的失败

| 情况 | RetryCause | 获准后的动作 |
|---|---|---|
| 路线依赖在计算期间变化 | `DEPENDENCY` | 从当前身体状态建立下一次尝试 |
| worker 无结果、死亡或调用方期限到达 | `PLANNING` | 若身体责任允许则开始下一次尝试，否则先安全收尾 |
| 已复制的相关已知事实变化，使快照依据失效 | `DEPENDENCY` | 开始下一次尝试；相同变化不得每帧重复登记 |
| 规划器明确不支持当前规则 | `PLANNING` | 默认失败；只有上层改变能力或条件后才可建立新任务 |
| 取得当前阻塞事实 | `BLOCKING_FACT` 进展 | 清空本轮次数，不清空任务总次数 |

相同失败被重复观察时不得重复计数。账本只有返回 `RETRY` 且许可尚未消费时，才能建立下一次尝试。`CAUSE_EXHAUSTED`、`ROUND_EXHAUSTED` 或 `TASK_EXHAUSTED` 都返回类型化失败。

仅切换 stone/dirt 等合法材质不是进展，除非该观察取得了当前 `PlanningInformationNeed` 声明的阻塞事实。合法材质反复变化仍按同一原因消耗上限。

## 5. 实施顺序

### R25-0：重新打开门槛并冻结反例

修改文档状态，保留第二十四轮通过记录，不把历史证据改写成失败。

先加入失败测试：

- 路径外不可观察未知格与路径上头部未知格同时存在；确认路径头部为空后必须规划成功；
- 前 64 个阻塞事实均不可获取，第 65 个可通过现有视角补齐；必须在固定期限内选到第 65 个；
- 起点或终点解析缺少事实；必须报告对应端点阻塞；
- 旁支不支持、目标方向缺信息同时存在；必须先报告目标方向的信息需要；
- 路线依赖在两种合法普通材质之间反复变化；必须在共享上限内到达有类型结果；
- 分片期间连续取得新观察，以及同区块段但规划范围外的变化；不能被当成连续失败。

完成条件：这些测试在当前实现上稳定暴露缺口，并保存最短复现。

### R25-1：让规划器报告真实阻塞事实

在端点解析、查询和 `_SurfaceExpander` 中收集未知依赖。给候选增加 `PlanningInformationNeed`，补齐身份、稳定排序、去重、容量、截断、翻页和混合状态优先级测试。

完成条件：

- 路径外未知格不能遮蔽实际受阻连接；
- 已知路线存在时不请求无关信息；
- 前一批不可获取时能有界进入下一批，不反复返回同一事实；
- 单批 64、总计 128 个容量触顶时结果有类型、内存有界；
- Dijkstra 对照、路线代价和现有已知地图结果不变。

### R25-2：先建立协调器，不改变会话外部行为

创建 `PlanningCoordinator` 和纯组件测试。迁移快照推进、尝试及许可、请求提交、期限检查、候选身份检查和候选接纳。此时 `NavigationSession` 只消费 `PlanningUpdate`。

先把结构迁移与策略变化分开验证。相同输入下，除已经列出的 P1 和明确列出的超时／重试变化外，规划候选、路线身份、失败原因、旧路线收尾和控制提案都要与迁移前一致。

### R25-3：统一失败登记和下一次尝试

把依赖拒绝、相关快照失效、规划超时和 worker 失败收进协调器。内部自动重试必须带尚未消费的 `FailureRegistration(RETRY)`；任务更新和真实进展必须带各自的一次性许可。

完成条件：

- 100 次材质切换不能产生 101 代请求，并断言准确的获准次数和终态；
- 同一个候选重复返回只计一次；
- 同一次许可不能生成两次尝试；
- 快照阶段没有候选时，也能稳定登记一次失败；
- 连续取得新观察和同区块段范围外变化不会误耗尽，真实相关材质反复变化会耗尽；
- 目标频繁修订、交互确认、已验证前段完成和安全重锚都走声明的许可来源；
- 有旧执行器时，重试耗尽仍由旧执行器安全退场。

### R25-4：切断会话的旧规划写入口

让 `NavigationSession._advance_planning()` 缩减为协调器调用和类型化结果路由。删除已迁移的重复字段代理和直接重发分支。

维护“旧字段／旧方法—新拥有者—保留的只读入口”迁移表。兼容期只允许 `NavigationSession -> PlanningCoordinator` 的单向委托，禁止协调器回调会话继续执行规划策略。同一字段始终只有一个写入者。

增加静态门禁：除 `PlanningCoordinator` 和状态定义外，生产代码不得写 `PlanningPipelineState.submitted_*`、builder 和 deadline 字段，不得调用 `clear_submission/clear`、递增请求代次或直接调用 planner worker 的 submit/poll。

完成条件：规划状态 owner 和操作规划状态的代码位于同一模块；公开接口不变。

### R25-5：重新验收 S5

依次运行：

1. 规划协调器、阻塞事实、RetryLedger 和 NavigationSession 组件测试；
2. 正式路径中的上述 P1 场景族，以及重复结果、一次许可重复使用、旧世界结果、目标频繁修订、交互工作点规划和旧控制者安全退场；
3. 完整 `tests/motion_nav`；
4. 固定 14 场、冻结 200 种子、192 个中断组合和 1000 个随机事件序列；
5. 分段证据、Java 门禁、公开证据和独立导出检查；
6. 一个 profile 4 Fabric 信息获取场景：路径上缺失净空，路径外存在结构性不可观察未知格；记录请求格、传感器结果、取得事实和开始移动的时刻。

Fabric 只验证信息选择进入真实观察链，不重跑连续高度 800 场。若迁移改变运动路线、带速交接或输入时序，再扩大对应实机回归。

## 6. 新增门禁

| 编号 | 不变量 |
|---|---|
| I15 | `PLANNING` 必须拥有与当前 attempt、request、目标修订一致且仍在期限内的快照或后台作业 |
| I16 | 初始尝试之后，每次新规划尝试必须消费一个与当前任务和目标修订匹配、此前未使用的一次性许可 |
| I17 | `NEEDS_INFORMATION` 中的事实必须来自当前请求的端点解析或实际到达的搜索前沿，并保留世界、快照和查询身份 |

I15 不能只检查字段非空。I15、I16、I17 都要有故意违反的监视器测试。

## 7. S5 重新关闭条件

必须同时满足：

1. 两个原始 P1 和 Astra 补出的相邻场景族全部通过；
2. I15、I16、I17 没有违规，故意违反测试能稳定报告；
3. `NavigationSession` 不再直接拥有规划推进和候选生命周期行为；
4. 规划结果、失败和信息需要都有请求、目标、世界、快照或尝试身份；
5. 固定、种子、中断和随机门禁没有白名单外结果；
6. profile 4 代表场景证明真实观察链能取得规划声明的阻塞事实；
7. 文档、实现、测试和公开导出一致。

## 8. S5 之后的结构优化顺序

R25 不把后续工作提前实现，只冻结顺序：

1. 把现有信息请求、视角、探边、取证许可和超时迁入 `InformationAcquisitionCoordinator`；
2. 为 `ActionRouteExecutor` 增加薄的 `SegmentExecutionAdapter`，让上一段只读取下一段的 `SegmentEntryWindow`；
3. 物理拆分 legacy walk planner 和 surface planner，保持公开规划入口不变；
4. 再用 Swim 或 Climb 检查新增能力是否仍需要修改多个核心模块。

`fixed_route.py` 和 `motion_solver.py` 只在实测证明维护或性能问题后拆分，不按文件行数机械重构。

## 9. 文件范围

预计新增或修改：

- `mc2p/motion_nav/planning_coordinator.py`
- `mc2p/motion_nav/navigation_owners.py`
- `mc2p/motion_nav/navigation_session.py`
- `mc2p/motion_nav/known_map_planner.py`
- `mc2p/motion_nav/retry_ledger.py`，仅在需要公开许可或诊断契约时修改
- `tests/motion_nav/test_planning_coordinator.py`
- `tests/motion_nav/test_known_map_planning.py`
- `tests/motion_nav/test_navigation_session.py`
- `tests/motion_nav/test_navigation_closed_loop.py`
- `tests/sim/monitor.py` 与对应固定清单
- 本阶段 architecture、acceptance、decisions 和 stages 文档

不修改 `physics_1_21.py`、几何查询规则、Fabric 输入权限和用户世界数据。

## 10. 当前实施结果

2026-09-30 已完成以下迁移：

- 表面规划器只报告端点解析和实际展开前沿遇到的未知事实。`PlanningInformationNeed` 绑定世界、快照、请求、目标修订、选择修订和稳定阻塞键；单次结果最多保留 128 个事实，协调器按 64 个一批处理。
- `PlanningCoordinator` 现在独占分片快照、规划尝试、一次性许可、后台提交和轮询、调用方期限、候选身份检查、路线接纳、信息翻页以及规划失败重试。
- `GoalRequestLedger` 是请求代次的唯一写入者。`NavigationSession` 不再直接写 `PlanningPipelineState`，也不直接调用规划 worker。
- 依赖变化、worker 无结果和调用方超时进入任务级 `RetryLedger`。目标修订、取得阻塞事实和确认世界交互分别使用有身份的一次性许可，不冒充失败重试。
- I15、I16、I17 已进入逐 tick 监视器，并各有故意违反的反例测试。静态门禁同时检查规划 worker、pipeline 和请求代次的唯一写入者。

本轮完整 `tests/motion_nav` 为 790/790。固定 14 场为 13 个任务成功、1 个有界安全失败；冻结 200 种子为 198 个任务成功、2 个有界安全失败；192 个中断与晚到组合全部到达终态；1000 个固定随机序列全部落在事先冻结的允许结果内。

随机序列第一次扩大运行时发现：替换规划失败后，旧路线已经请求停止，但仍持有身体的探边没有同步退场，任务会永久停在 `STOPPING`。修正后，替换失败、待重试和旧路线终结都通过同一条监督收尾路径停止探边；新增正式事件序列回归后，1000 个固定序列通过。该失败记录不从历史中删除。

profile 4 Fabric 代表批次 `20260930T070421228037Z-9393f4c4` 通过。路径净空 `(0,100,6)` 在观察序列 7 首次进入信息请求，序列 13 取得空气事实；侧墙后的比较格 `(1,100,3)` 到序列 20 才进入后续前沿；机器人在序列 24 开始移动，并在序列 58 以 `goal_state_satisfied` 完成。12 项部署、结构化观察和零图像门禁全部通过。

两次脚本失败和一次公共证据检查失败继续保留：第一批误读 `MovementV1` 字段；第二批把“后续也不能请求旁格”写成了过强条件并漏传清理原因；第三批核心场景已通过，但把业务逐帧记录误交给零图像诊断检查。它们都是验收脚本问题，没有改写机器人结果。最终独立公开版导出和哈希校验通过，S5 据此重新关闭。
