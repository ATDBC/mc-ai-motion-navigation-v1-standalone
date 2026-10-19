# D058：普通同高 Walk 的依赖变化先做有界重验

日期：2026-10-05。状态：已实施，组件、正式链和性能门槛已通过独立复审；`straight_2_0` Fabric 尚未复跑，F1-D 仍打开。

## 发现的问题

F1-D 的 `straight_2_0` 原批次出现四次周期性停车。活动路线刚接纳后，正式空气查询把路线前方两格从 `UNKNOWN` 更新为 `AIR`。这些格与路线依赖相交，Session 立即按 `active_route_dependency_changed` 停止旧路线并重新规划。

这里没有物理方块变化。`UNKNOWN -> AIR` 是真实的新知识，且新事实证明原普通平地 Walk 仍然可行。直接停车保持了安全，但造成 4 次任务恢复、4 个额外规划工作和稳定距离退步。

不能通过过滤 `UNKNOWN -> AIR` 解决。规划、严格动作和未知空间边界仍需要知道一个格子的导航语义刚刚改变。也不能把所有路线变化都改成在线重算。Step、空中动作和连续高度 Walk 的证明条件不同，把它们塞进同一快速路径会扩大安全范围和控制帧成本。

## 决定

保留 `WorldKnowledge` 和 `NavigationObservationAdapter` 的现行语义：`UNKNOWN -> AIR` 继续进入 `changed_cells`。变化是否允许当前身体继续，由持有该路线的执行层根据动作类型和原接纳条件判断。

首版只允许下面的路线在依赖相交后继续：

- 当前动作是精确类型 `WalkSegment`；
- 所有相关点同高；
- `traversal_plan is None`；
- 该段使用普通 `MovementMode.WALK`；
- RouteAdmitter 能把该段逐腿映射回接纳时实际使用的 surface edge 或 standable connection 证明；
- 受影响的每一腿在当前 `WorldView` 上按原 query kind 重放后仍为 `FEASIBLE`；
- recipe 重放次数、路线身份和动作身份都满足本决定的有界要求。

下面情况继续沿用原来的安全停止和重新规划：

- `StepSegment`、`JumpUpSegment`、`JumpGapSegment`、`ControlledDropSegment`；
- 带 `traversal_plan` 的 Walk；
- 任意连续高度或点高不同的 Walk；
- Crawl、Sprint 或其他非普通 Walk；
- 无法精确映射到原查询、缺信息、阻塞、不支持、身份不匹配或查询预算耗尽。

这不是增量寻路，也不是路线平滑。它只回答一个问题：相关知识变化后，当前已接纳的普通同高 Walk 是否仍满足原来的几何条件。

## 最小重验计划

RouteAdmitter 必须先完成最终 `ActionRoute`，包括 D057 跳过首边、终点替换、Walk 合并和严格动作切分；随后才生成一个不可变 `ActiveRouteValidationPlan`。不能在候选图边阶段先生成单个 recipe，再假设它仍对应最终路线。

计划按最终 action index 保存多个 action plan。每条 Walk 腿同时绑定最终 `fixed_route_id`、point index 和累计 progress 区间，因此 `Walk -> Step/Jump -> Walk` 的前后两段不会混用身份。owner 和 recipe 都是有类型记录；`owner_id` 与 `recipe_id` 只是不可解析的引用键：

```python
@dataclass(frozen=True, slots=True)
class GroundCapabilityIdentity:
    profile_id: str
    environment_id: str
    ground_model_id: str | None
    support_materials: frozenset[str]
    catalog_environment_id: str | None
    minecraft_version: str | None

class WalkValidationQueryKind(Enum):
    SURFACE_EDGE = "surface_edge"
    STANDABLE_CONNECTION = "standable_connection"

@dataclass(frozen=True, slots=True)
class SurfaceEdgeQueryArgs:
    start_surface: SupportSurface
    end_surface: SupportSurface
    body_height_blocks: float

@dataclass(frozen=True, slots=True)
class StandableConnectionQueryArgs:
    surface: SupportSurface
    position: tuple[float, float, float]
    connection_from: tuple[float, float, float]
    body_width_blocks: float
    body_height_blocks: float

@dataclass(frozen=True, slots=True)
class WalkValidationRecipe:
    recipe_id: str
    query_kind: WalkValidationQueryKind
    surface_edge: SurfaceEdgeQueryArgs | None
    standable_connection: StandableConnectionQueryArgs | None
    ground_profile: GroundMotionProfile
    capability: GroundCapabilityIdentity
    dependencies: tuple[BlockPos, ...]

class DependencyOwnerKind(Enum):
    WALK_LEG = "walk_leg"
    INITIAL_CONNECTION = "initial_connection"
    STRICT_ACTION = "strict_action"
    NON_RECIPE = "non_recipe"

@dataclass(frozen=True, slots=True)
class DependencyOwner:
    owner_id: str
    kind: DependencyOwnerKind
    action_index: int
    fixed_route_id: str | None
    recipe_ref: str | None

@dataclass(frozen=True, slots=True)
class InitialConnectionValidation:
    owner_ref: str
    retire_after_progress_blocks: float

@dataclass(frozen=True, slots=True)
class WalkLegValidationBinding:
    owner_ref: str
    recipe_ref: str
    start_point_index: int
    end_point_index: int
    start_progress_blocks: float
    end_progress_blocks: float

@dataclass(frozen=True, slots=True)
class WalkActionValidationPlan:
    action_index: int
    fixed_route_id: str
    legs: tuple[WalkLegValidationBinding, ...]

@dataclass(frozen=True, slots=True)
class DependencyProvenance:
    position: BlockPos
    owner_refs: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class ActiveRouteValidationPlan:
    action_plans: tuple[WalkActionValidationPlan, ...]
    recipes: tuple[WalkValidationRecipe, ...]
    owners: tuple[DependencyOwner, ...]
    initial_connection: InitialConnectionValidation | None
    dependency_provenance: tuple[DependencyProvenance, ...]
```

`DependencyOwner` 是唯一的依赖归属表。`DependencyProvenance` 只保存 `position -> owner_refs`，每个引用必须指向该表中的一个 owner；每个 `recipe_ref` 必须指向唯一 recipe。`WalkLegValidationBinding` 单独保存最终路线的 point 和 progress 区间，初始 connection 不伪造一对路线 point。构造计划时校验引用完整、owner 与 binding 的 recipe ref 一致、action／fixed route 身份一致，以及 query kind 与参数恰好匹配。任何悬空、重复或矛盾引用都使路线没有继续资格。同一格可以同时有多个 owner，provenance 必须全部保留。

### 两类查询重放和能力身份

现有两类证明不能合并成一种新几何查询：

- `SURFACE_EDGE` 严格重放现有 `_surface_walk_query`。它核对同高，执行原 sweep，并在 `.25`、`.5`、`.75`、`1.0` 四个位置检查至少 `0.5` 的支撑。首版仍使用现行 `0.6` 格身体宽度；其他宽度无法精确重放时失败关闭。
- `STANDABLE_CONNECTION` 严格重放现有 `query_standable_connection`。它保留精确 endpoint region、原 clearance／support／connection sweep，并按不超过 `0.1` 格的间隔检查整段支撑。不能用四点采样替代，也不能把它改写成 surface edge。

两者外围只共用 `revalidate_walk_recipe(...)`：先核对实际 `GroundMotionProfile`、材质、trait 和 capability identity，再按 `query_kind` 调用原查询，并复用当前 `WorldView` 的 `WorldQueryCache`。cache 只复用底层事实和碰撞结果，不改变原查询的采样点、endpoint region、状态合并、依赖集合或查询成本口径。预算仍按 recipe 重放计数，cache 命中也占一次，不能借 cache 扩大四次上限。`query_kind` 与参数不匹配、原查询没有可复用入口或无法得到与首次证明相同的参数时，直接失败关闭。

每条 recipe 保存首次查询实际使用的冻结 `GroundMotionProfile`，同时保存可比较的能力身份。RouteAdmitter、规划器和 RouteControl 必须收到同一个正式 profile，不允许 tracker 根据字符串重新查配置。重验时，RouteControl 正在使用的 profile 必须和 recipe 的 profile、environment、ground model、support materials、catalog environment 和 Minecraft version 全部一致。身份不一致时直接 `STOP/CAPABILITY_IDENTITY_CHANGED`。材质与 `BlockMotionCatalog + ground_model_id` 的 trait 检查放在两类原查询的共同外围；各自的 sweep 和 support 算法仍只有原实现一份。

现有普通表面图边只生成 `SURFACE_EDGE` recipe。首次确由 `query_standable_connection` 证明的 D057 前向接入或终点 tail 只生成 `STANDABLE_CONNECTION` recipe。如果现有终点 tail 或 D057 接入无法精确重放首次查询，就没有 recipe，相关依赖变化时失败关闭。带 traversal proof 的 tail、简单追加的 tail 和映射不唯一的 tail同样不进入白名单。

### 初始 connection、action 切分和进度退役

初始 connection 单独保存，不伪装成普通图边。它的 `retire_after_progress_blocks` 必须等于最终 `ActiveRoute.connection_length_blocks`。如果它本来就由两类原查询之一精确证明，可以通过 owner 的 `recipe_ref` 引用 recipe；现有通用 `_connection()` 无法精确重放时，`recipe_ref=None`，走完前任何自身依赖变化都 `STOP`。确认 progress 达到退役值后，connection owner 和依赖一起退出。

每个 Walk action 从最终 `FixedRoute.points` 建 plan。终点替换发生后重新计算最后一腿的 point index 和 progress；D057 跳首边后不保留被跳过边的 recipe；`Walk -> strict -> Walk` 生成两个不同 action plan，中间 strict action 的依赖作为不可重验 owner 保存。

普通 `FixedRouteDecision.progress_blocks` 通过 RouteControl 决策结果提供给 tracker，不另算路线投影。tracker 只按 `DependencyOwner.action_index`、`kind`、`fixed_route_id` 和 plan 内的有类型引用退役 owner：`INITIAL_CONNECTION` 按 `InitialConnectionValidation.retire_after_progress_blocks` 退役；`WALK_LEG` 按对应 `WalkLegValidationBinding.end_progress_blocks` 退役；action index 前进时，前一 action 的所有 kind 一起退役。不得解析 `owner_id`，也不得在执行时回读 `ActionRoute` 猜测归属。进度缺失、回退、引用缺失或 fixed route identity 不匹配时失败关闭。

### 依赖 provenance

不能把 dependencies 当作一个集合做差。对每个 affected cell，tracker 必须取出所有仍活动的 owner：

- 所有 owner 的 kind 都是当前 action 的 `WALK_LEG` 或尚未退役的 `INITIAL_CONNECTION`，都有有效 `recipe_ref`，并且所有去重后的 recipe 按各自 query kind 重验通过，才可 `CONTINUE`；
- 任一 owner 来自 Step、JumpUp、JumpGap、ControlledDrop、traversal plan、初始不可重验 connection、严格动作或其他 non-recipe，立即 `STOP`；
- 查询成功后，只替换该 recipe 自己的 dependency 引用。其他 owner 对同一格的引用保持；
- 新 dependencies 重新登记到对应 recipe owner，不能覆盖现有 strict／non-recipe owner。

这保证共享依赖不会被一个成功 Walk 查询误删，也支持最终 ActionRoute 中多段 Walk 和严格动作交错。

## 路线拥有者和有类型结果

`ActiveRoute` 增加可选的 `validation_plan` 字段。RouteAdmitter 在最终 ActionRoute 构造完成后把 plan 放入这个不可变字段，因此现有 `AdmissionResult -> PlanningUpdate -> NavigationSession` 不需要另传一份可变对象。Session 创建 RouteControl 时必须同时创建 `ActiveRouteTracker(active_route)`；本地路线和完全无法映射的路线使用 `validation_plan=None`，但仍有 tracker，相关依赖变化时返回失败关闭。

每个 `RouteControl` 拥有且只拥有一个 `ActiveRouteTracker`。tracker 保存该控制者的不可变 plan、当前 action 的 provenance、当前有效依赖和路线身份。现有只在测试中使用的 candidate／corridor 进度入口可以保留为可选输入，但正式 D058 验证不依赖 source candidate，也不能从 candidate 临时重建 plan。tracker 不拥有规划、恢复、输入或业务终态。

路线身份至少包含：

```python
@dataclass(frozen=True, slots=True)
class ActiveRouteValidationIdentity:
    world_session: str
    route_id: str
    route_revision: int
    source_request_id: str
    goal_id: str
    goal_revision: int
    planning_generation: int
    work_identity: AsyncWorkIdentity | None
    action_index: int
```

tracker 返回：

```python
class ActiveRouteValidationDisposition(StrEnum):
    UNAFFECTED = "unaffected"
    CONTINUE = "continue"
    STOP = "stop"

@dataclass(frozen=True, slots=True)
class ActiveRouteValidation:
    disposition: ActiveRouteValidationDisposition
    reason: ActiveRouteValidationReason
    identity: ActiveRouteValidationIdentity
    affected_cells: tuple[BlockPos, ...]
    refreshed_dependencies: tuple[BlockPos, ...] = ()
    missing_cells: tuple[BlockPos, ...] = ()
    queries_used: int = 0
```

权限由 enum 和身份字段决定。`reason` 字符串只用于诊断。

`ActiveRouteTracker.validate()` 先计算 `changed_cells ∩ effective_dependencies`。交集为空时返回 `UNAFFECTED`，不创建查询缓存，也不重放 recipe。交集非空时，先展开每个 affected cell 的全部活动 owner。只有所有 owner 都通过有类型字段指向当前 action 的可重验 Walk recipe，才按 query kind 重放去重后的 recipe；一个 strict／non-recipe owner、悬空引用或身份矛盾就足以返回 `STOP`。一次 RouteControl 最多重放 2 条 recipe；incumbent 与 pending 合计每帧最多重放 4 次。超过上限直接返回 `STOP/QUERY_LIMIT_EXCEEDED`。本上限不自动延后到下一帧，因为旧路线在当前帧已经缺少继续授权。

同一帧的 incumbent 与 pending 复用一个 `WorldQueryCache(frame.world)`。查询结果继续使用该 cache 返回的事实和碰撞盒；不建立长期缓存，也不跨 WorldView 保存 cache。

## ExecutionSupervisor 与 Session 的边界

`ExecutionSupervisor` 在推进身体控制者前分别检查 incumbent 和 pending。顺序固定为 incumbent 在前、pending 在后，因为 incumbent 当前承担身体和在途输入责任。

- incumbent `CONTINUE`：更新其 tracker 依赖，继续原 RouteControl；
- incumbent `STOP`：由 supervisor 对该控制者请求 `StopCause.DEPENDENCY_CHANGED`；
- pending `CONTINUE`：更新 pending tracker，保留现有接替流程；
- pending `STOP`：由 supervisor 丢弃 pending。它尚未拥有身体，不能让 incumbent 因它制动；
- 两者都 `STOP`：丢弃 pending，并让 incumbent 走原安全停止；
- `UNAFFECTED`：保持原状态，查询数必须为 0。

supervisor 返回同时包含 incumbent 和 pending 身份、处置及身体动作的有类型汇总。Session 只消费这个汇总：incumbent 被停止时进入现有 dependency recovery；pending 被丢弃时走现有“保留 incumbent、为当前请求重新规划”入口。Session 不读取 validation plan，不调用几何查询，也不根据 reason 文本决定权限。

现有 Session 中直接用 `active_route.action_route.dependencies` 请求停止的分支由 supervisor 结果替代。世界保护和后续变化交集读取 tracker 的 `effective_dependencies`，避免重验后继续使用旧依赖。

## 失败关闭映射

| tracker 结果 | supervisor 动作 | Session 结果 |
|---|---|---|
| 无依赖交集 | 保留控制者，不查询 | 不改变生命周期 |
| 所有受影响腿 `FEASIBLE` 且身份一致 | 刷新依赖并保留控制者 | 不购买恢复，不提交规划 |
| `NEEDS_INFORMATION` | incumbent 停止；pending 丢弃 | 沿用现有缺信息／依赖恢复入口，不继续旧路线 |
| `BLOCKED` | incumbent 停止；pending 丢弃 | 现有 dependency recovery |
| `UNSUPPORTED` | incumbent 停止；pending 丢弃 | 现有不支持或 dependency recovery 映射，不降级成可行 |
| 动作不在白名单、plan 缺失、存在 strict／non-recipe owner 或 dependency 无法映射 | incumbent 停止；pending 丢弃 | 原安全停止与重规划 |
| 世界、路线、目标、请求、generation 或 action identity 不一致 | 拒绝应用结果并停止对应控制者 | 旧身份不能授权当前路线 |
| 单控制者超过 2 条腿或整帧超过 4 次查询 | incumbent 停止；pending 丢弃 | `QUERY_LIMIT_EXCEEDED`，不延期使用旧许可 |

空中身体、严格动作输入在途和连续高度 Walk 继续由原动作负责人完成安全收尾。本决定不改变它们的依赖规则。

## 反例和同帧停止

核心反例必须使用真实 `WorldKnowledge` 更新，不能只构造 `changed_cells`：

- 把活动 Walk 腿的支撑格从 BLOCK 改为 AIR；
- 在身体 sweep 空间把 AIR 改成 full cube；
- 把当前支撑改成 profile／catalog 不支持的材质；
- 让一个 changed cell 同时由普通 Walk recipe 与 strict／non-recipe owner 引用；
- 构造最终 `Walk -> Step -> Walk`，分别改变三段依赖并推进 action index；
- 覆盖 D057 跳首边临界内外、通用 initial connection 退役前后，以及终点替换 tail 的可映射／不可映射两组。
- 分别把 `SURFACE_EDGE` 的四点支撑和 `STANDABLE_CONNECTION` 的 0.1 格采样／endpoint region 与原查询逐项对齐，证明两类 recipe 没有互相替代；
- 构造不透明 owner id，证明退役和 provenance 只读取 typed owner 字段，不解析 id，也不从 `ActionRoute` 重建归属。

支撑删除、full cube 插入、不支持材质、共享 strict 依赖和无法映射场景必须在 changed 到达的同一控制帧返回 `STOP`。incumbent 本帧不得再提交正向移动；pending 本帧必须被丢弃且不能影响合法 incumbent 的输入。测试同时断言实际 owner、query count、provenance、typed reason 和 refreshed dependencies。

## 性能边界

重验只在 changed 交集时运行。无交集帧必须为零 recipe 重放，并单独报告这一分支的墙钟分布。组件基准在 100 次预热后记录至少 1,000 个受影响样本，分别报告 `SURFACE_EDGE`、`STANDABLE_CONNECTION`、复杂形状、单 incumbent 和 incumbent + pending 四次重放上限。新增重验段报告 P95、P99 和最大值；P95 不高于 1 ms。完整控制准备继续要求 P95 不高于 8 ms、P99 不高于 15 ms、名义最大值小于 30 ms。

若最小实现不能满足 1 ms，停止扩大白名单。不能把完整路线重放移入控制线程，不能提高查询上限，也不能靠过滤 `UNKNOWN -> AIR` 通过。

## 实施与复审结果

D058 已按本决定完成。RouteAdmitter 在最终 ActionRoute 后建立不可变 validation plan；每个 RouteControl 持有自己的 tracker；ExecutionSupervisor 在身体推进前验证 incumbent 和 pending；Session 只消费有类型结果。普通同高 Walk 可以重放原 `SURFACE_EDGE` 或 `STANDABLE_CONNECTION` 查询。严格动作、连续高度、无法映射和身份不一致继续失败关闭。最终审查发现的进度身份、pending boundary、Probe 交接和有界等待问题均已用正式链测试修正，独立复审没有未关闭 P0／P1／P2。

最终相关组件集合为 **226/226**。它覆盖 validation plan、运行时重验、性能工具、路线身体推进、监督中断、闭环量尺和 F1-C；没有运行 Fabric。持久化性能证据见 `evidence/motion_navigation/d058-route-revalidation-v2/`。该批使用项目规定的 `conda.exe run --prefix ... --no-capture-output python` 启动，来源提交为 `295fd942`，运行前工作树干净。六组各预热 100 次并记录 1,000 个样本：

| 组 | P95 / P99 / 最大值（ms） | 查询数 |
|---|---:|---:|
| 非空 changed、无交集 | 0.0056 / 0.0057 / 0.0109 | 0 |
| `SURFACE_EDGE` | 0.2415 / 0.2641 / 0.3750 | 1 |
| `STANDABLE_CONNECTION` | 0.3278 / 0.4145 / 0.5501 | 1 |
| 复杂形状 | 0.2342 / 0.2451 / 0.4087 | 1 |
| 单 incumbent、共享依赖 | 0.1586 / 0.1609 / 0.1833 | 2 |
| incumbent + pending | 0.2777 / 0.2801 / 0.3177 | 2 + 2 |

完整 F1-C 清单捕获 3,513 次 `RuntimeNavigationDriver.prepare_proposals`，丢弃前 100 次后保留 3,413 个原始样本。P95 为 4.5990 ms，P99 为 7.6267 ms，最大值为 9.8917 ms，满足 8／15／小于 30 ms 的门槛。全部原始样本、环境、源码哈希、typed 预期、查询数、身份和量尺结果都在 `performance.json`，`SHA256SUMS` 已复核。

`d058-route-revalidation-v1` 保留原字节。它与 v2 使用同一项目环境，数字也通过门槛，但 `COMMAND.txt` 记录的是环境内 Python 路径，不是项目规定的 launcher，因此只作为历史证据。v2 不覆盖 v1，并记录了实际标准命令。

## F1 的处理

D058 是 F1 暴露的共享导航前置修复，不是跟随层专用分支。F1 初始“十二个核心协调文件零修改”门槛已在 D057 时失败；D058 还会按职责修改路线接纳、路线控制、执行监督和 Session 消费入口。文档必须如实记录这个事实。

D058 的组件、正式链、性能和独立审查已经通过。下一步从新基线重跑 `straight_2_0`。旧失败批次保持不变。只有新批次同时满足原距离、规划比、响应、安全、终态和来源释放门槛，并且四个 `UNKNOWN -> AIR` 事件不再购买 task recovery 或制造 `cancel_braking`，才继续另外四个 F1-D 场景。

## 不做的事

- 不改变保持距离、修订距离、目标速度、0.45 格投影阈值或恢复预算；
- 不增加后台线程、增量寻路器、全局路线缓存或第二套几何；
- 不为 follow 增加 Session 分支；
- 不把缺信息、阻塞或不支持改写为可行；
- 不把一次代表性 Fabric 通过写成统计证明。
