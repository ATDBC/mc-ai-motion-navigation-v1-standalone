# D062：普通 direct Walk 统一建立可重验执行证明

日期：2026-10-05。状态：D062-A 至 D062-D 已按冻结范围实施，提交为 `1edb748`；`deeeec2` 修正复审发现的零长度 local goal 并补正式 Session 正例，D062-E 独立复审确认 P0／P1／P2 为零。D060-F 的新 `straight_2_0` 失败原样保留；本决定只处理两条普通同高 direct Walk 缺少统一证明的问题，不处理冷启动和稳定 P95，也没有运行新的 Fabric。

## 要解决的问题

D060 补齐了“终点等于末图节点”的普通 Walk 证明。修正后的单场 Fabric 仍失败，但失败路径已经变化。原始目录为：

`artifacts/f1-known-world-following/20261005T0408072740471Z-straight-2-0-d060/`

运行源码为干净提交 `6ff9ad16ddaba6f7a41d1fc8ab9981ae7adee8e4`。目标稳定速度为 1.867230 格／秒，修订响应 P95 为 4 tick，规划提交／接受修订为 20／16 = 1.25，最终距离为 0.673621 格。安全、终态、来源释放、host、时间归属和清理均通过。

本批仍有两项失败：

- 稳定 excess mean 为 0.649322 格，符合门槛；P95 为 1.541509 格，超过 1.5 格；
- 出现两次 dependency recovery，没有达到针对性零恢复门槛。

本决定只处理后者。P95 超限来自初始 12 tick 感知／规划后留下的追赶尾部。tick 39、40 时控制器持续 forward=1，恢复次数仍为 0；同一时段与 D059 批次轨迹一致。因此它不是下面两条 proof 缺口的结果，不能借 D062 改速度、保持距离、稳定窗或 P95 算法。

## 两条缺少统一证明的正式路径

### 1. 同支撑 local direct Walk

goal revision 13 进入 `SAME_SUPPORT_LOCAL_GOAL`。`NavigationSession._activate_local_route()` 从当前身体位置直接走向 GoalState 中心。它调用现有 `sweep()` 和 `query_support()`，把依赖合并后手工构造两点 `FixedRoute`、`WalkSegment` 和 `ActiveRoute`。

这条路线没有 `validation_plan`。Observation 64 把 `(0,-59,10)`、`(0,-59,11)`、`(0,-59,12)` 确认为空气时，正式 tracker 返回：

`STOP / PLAN_UNAVAILABLE / queries_used=0`

三格属于身体到目标中心的 direct connection 净空扫掠，不是终点站位选择产生的 selection-only 依赖。现行行为安全，但购买了一次 dependency recovery。

### 2. forward-entry 后只剩一个 graph node

goal revision 16 的原 Observation V3 已离线写入正式 `WorldKnowledge`，并按正式 surface planner 和 RouteAdmitter 重建。重建 route id 与实机完全一致：

`7ce64f4f805eaecae231e427`

路线从 graph node z=11 走向 z=12。接纳时身体已经沿首腿前进，D057 forward-entry 跳过 z=11。最终 Walk 的 fixed points 是当前身体 z=12.081778 到选定终点 z=12.5，但 `node_ids` 只剩 z=12 一个。

这条路线已经拥有一个 initial `STANDABLE_CONNECTION` proof。它精确证明上述 fixed leg，依赖只含 z=11、12。selected terminal 也正是 z=12.5。D060 仍要求最后 Walk 至少有两个 graph node，无法把这份 initial proof 同时认作 terminal execution 来源，于是把 z=13 的 terminal selection dependencies 追加到 action。z=13 只有 `NON_RECIPE` owner。

Observation 79 更新 `(0,-59,13)` 后，正式 tracker 返回：

`STOP / NON_RECIPE_OWNER_CHANGED / queries_used=0`

这不是 direct query 失败、terminal 偏移或 shared owner。缺口是同一条已证明 fixed leg 在“initial connection”和“terminal execution”两个构建阶段没有复用同一证明。

## 决定

普通同高 direct Walk 统一由 RouteAdmitter 建立执行证明。它只处理已经存在的 `STANDABLE_CONNECTION` 查询语义，不新增 query kind、几何算法、缓存或恢复状态。

RouteAdmitter 内部可以使用私有的 `_DirectWalkProofContext`。它只在一次 `admit_surface()` 或 `admit_local_direct()` 调用栈内创建和消费，不写入请求、结果、Session 或长期状态。context 至少绑定 world session、ground profile、capability、route id、action index、`fixed_route_id`、fixed leg 的 `from/to` 和已有 `_StandableQueryProof`。外层身份或几何有一项不等，就不能消费这份 context。

共享构建代码接收最终 fixed leg、选定 endpoint 的 `SupportSurface`、正式 ground profile／capability、完整路线身份和可选的既有 proof。它必须完成下面的工作：

1. 核对 fixed leg 的 `from/to`、action index、`fixed_route_id`、point/progress、endpoint、world 和 profile identity；
2. 没有既有 proof 时，只调用一次现有 `query_standable_connection()`；
3. 查询为 `FEASIBLE` 时，生成或复用一个 `STANDABLE_CONNECTION` recipe 和一个 owner；
4. 用 recipe 的精确 dependencies 建立 action、corridor、provenance 和 effective dependencies；
5. 无法精确证明时失败关闭，不返回可执行但没有 plan 的 direct Walk。

这些私有数据只传递一次接纳过程里已经验证过的事实，不能根据 `reason` 字符串或最终 dependency 集合反推 proof 来源。现有类型若不能表达上述约束，可以增加一个私有 frozen typed context；不能新增跨帧状态。

builder 是 RouteAdmitter 的私有构建边界，不发展成新的规划框架。`query_standable_connection()` 仍是 sweep、endpoint region、0.1 格支撑采样、材质和 trait 判断的唯一实现。

## 两个入口怎样使用共享 builder

### local route

NavigationSession 继续拥有目标修订、same-support 判断、等待、恢复购买和业务终态。它不再手工拼 `WalkSegment`、`ActiveRouteValidationPlan`、recipe、owner 或 provenance。

当目标属于同支撑 direct Walk 时，Session 只调用下面的公开入口，并消费现有 `AdmissionResult`：

```text
RouteAdmitter.admit_local_direct(typed request, frame, ground profile, capability identity)
```

typed request 复用正式 surface 请求身份和 `SurfaceNodeId`，不携带 Session 手工计算的 `SupportSurface`、sweep 结果、recipe 或依赖。RouteAdmitter 必须用现有 `query_support_surfaces()` 重新取得当前身体下方的 `SupportSurface`，并确认它与 request 的 start `SurfaceNodeId` 匹配；终点表面和整段连接也由 RouteAdmitter 的现有查询证明。Session 仍通过 ExecutionSupervisor 接纳 RouteControl，不增加 local 专用执行或恢复分支。

如果 endpoint、净空、整段支撑、材质、profile 或 capability 无法精确证明，RouteAdmitter 返回 typed 拒绝或信息需求。Session 沿现有信息／规划／失败出口处理，不能退回 `validation_plan=None` 的可执行 local route。查询结果固定映射为：

- `NEEDS_INFORMATION`：`GOAL_STANDING_POINT_NEEDS_INFORMATION`，Session 走现有 `WAIT_FOR_INFORMATION`；
- `BLOCKED`：`CURRENT_BODY_CANNOT_CONNECT`，Session 走现有 local path unavailable 失败出口；
- `UNSUPPORTED`：`ROUTE_CAPABILITIES_CHANGED`，Session 走现有 mode unsupported 失败出口。

local direct 的两点 fixed route 由一个 `WALK_LEG` owner 独占。binding 固定为 point 0→1，progress 0→整腿长度；`connection_length_blocks=0`，不得再建 `INITIAL_CONNECTION` owner。

### forward-entry single-node terminal equality

如果 `_forward_ground_entry()` 已经取得 proof，共享 builder 不再次查询。只有下面各项完全相等时，才能复用：

- proof 的 `connection_from` 和 `position` 等于最终 `FixedRoute` 最后一腿的两个点；
- proof endpoint 等于 selected terminal；
- action index、`fixed_route_id`、world、ground profile 和 capability identity 全部匹配；
- 最终 action 是普通同高 Walk，且没有 traversal plan。

符合条件时，构建期直接把现有 initial connection proof 的 dependencies 作为 terminal execution 依赖来源，并移除本次追加的 terminal selection dependencies。不能重复 query、复制 recipe、复制 owner 或让同一 fixed leg 产生两次运行时重验。

`ActiveRouteValidationPlan` 不新建 terminal `WalkLegValidationBinding`、terminal recipe 或 terminal owner。现有 `INITIAL_CONNECTION` owner 独占完整两点 fixed leg，action dependencies 精确等于该 recipe 的 dependencies；`retire_after_progress_blocks` 精确等于完整 `connection_length_blocks`。terminal 只在构建期说明“selection 依赖由这份 existing exact proof 替换”，运行时仍由 tracker 按这个唯一 initial owner 和 provenance 判断。

## 必须保持的保守边界

下面任何情况都不能借用 direct proof：

- `from/to`、selected terminal、point index 或 progress 不相等；
- world、route、goal、planning、work、action 或 fixed route identity 不相等；
- ground profile 或 capability identity 不相等；
- 既有 proof 的 query kind 不是 `STANDABLE_CONNECTION`；
- 查询为 `NEEDS_INFORMATION`、`BLOCKED` 或 `UNSUPPORTED`；
- 高差 Walk、连续高度 Walk、带 traversal plan 的 Walk；
- Step、JumpUp、JumpGap、ControlledDrop、strict owner 或非 WALK 模式；
- terminal 与最后 fixed point 不同，或没有能精确对应最终 fixed leg 的 proof。

这些情况继续失败关闭。shared `NON_RECIPE` 或 `STRICT_ACTION` owner 与 direct recipe 命中同一格时，仍由前者触发 STOP。proof 复用不能覆盖其他 action 的 provenance，也不能延长已经退休的 owner。

## 依赖、请求和保护

local direct Walk 的执行格不是 selection-only。修正后，它们仍可进入 effective dependencies、正式 observation request 和 `WorldKnowledge.set_protection()`；变化时按同一个 `STANDABLE_CONNECTION` recipe 重验。重验为 `FEASIBLE` 才能继续。

rev16 的 z=13 是 selection-only。复用 initial proof 后，最终依赖使用：

```text
direct_walk_execution_dependencies
  = dependencies of the one exact STANDABLE_CONNECTION proof
```

z=13 不再进入 action、corridor、effective dependencies、observation request 或 protection。若它由其他合法观察来源更新，结果必须为 `UNAFFECTED/NO_INTERSECTION`，affected 为空、查询为 0，也不能购买 recovery。

选定 z=12 endpoint 的支撑、净空、扫掠和 0.1 格采样依赖必须保留。删除支撑、插入净空 full cube 或改变受支持材质时，同帧必须 STOP，不能继续正向输入。

## 身份和状态归属

九字段 `ActiveRouteValidationIdentity` 保持不变：`world_session`、`route_id`、`route_revision`、`source_request_id`、`goal_id`、`goal_revision`、`planning_generation`、`work_identity`、`action_index`。`fixed_route_id` 继续单列检查。任何错配都在身体正向输出前失败关闭。

状态归属保持现行分工：

- NavigationSession 拥有 goal、请求、等待、恢复和终态；
- RouteAdmitter／共享 builder 拥有首次几何证明和不可变 validation plan；
- RouteControl／ActiveRouteTracker 拥有运行时有效依赖、进度退役和 typed validation；
- ExecutionSupervisor 拥有身体前的 incumbent／pending 校验；
- WorldKnowledge 继续报告真实 `UNKNOWN -> AIR`，不为本决定过滤 changed cells。

## 红测矩阵

### A. 冻结两条失败路径

1. 用本批 seq64 的等价合法 WorldKnowledge、身体和目标参数构造 local direct Walk。修前必须复现 `PLAN_UNAVAILABLE`。修后必须生成 exact recipe；三格空气确认时得到 `CONTINUE/REVALIDATED`，同帧仍有合法 forward，task recovery 为 0。
2. 用本批 rev16 的精确参数冻结 route id `7ce64f4f805eaecae231e427`、z=11 -> z=12 candidate、forward-entry 和单 node Walk。修前 z=13 只有 `NON_RECIPE` owner。修后路线几何和 route id 不变，z=13 从 action／corridor／effective dependencies、request 和 protection 移除；显式更新 z=13 得到 `UNAFFECTED/NO_INTERSECTION`、0 query、0 recovery。

fixture 必须在仓库内自行构造合法事实，不能读取本地 artifact 或 `.tmp`。

### B. 安全和身份反例

- 删除 selected support；
- 在 selected clearance 插入 full cube；
- 改成 profile 不支持的材质；
- 九个 validation identity 字段逐项错配；
- fixed route identity 错配；
- initial proof 的 endpoint／from／to 与最终 fixed leg 不同；
- progress 分别位于退役前、恰好达到完整 connection length 和退役后。

每项都必须同帧 STOP、无正向输入。initial owner 在 progress 小于完整 connection length 时仍负责依赖；progress 恰好等于或超过该长度后必须退休，之后同一格变化不得再借旧 proof 授权或停止新的 action。旧身份或倒退进度继续失败关闭。local `WALK_LEG` owner 也按自己的 0→1 binding 退役，不能借用 initial owner 的期限。

### C. no-proof 与多 action

覆盖 direct query 的 `NEEDS_INFORMATION`、`BLOCKED`、`UNSUPPORTED`，以及 traversal、strict、高差和 endpoint 不同。它们保持失败关闭。再用 Walk→strict→Walk 或等价多 action 路线证明 direct owner 只覆盖自己的 action；推进后按 action／progress 退役，不能复制或保留其他 action 的依赖。

## 实施顺序

### D062-A：测试

先落上述 local 和 rev16 红测，再补安全、身份、no-proof 和多 action 反例。修前失败必须分别落在 `PLAN_UNAVAILABLE` 与 single-node terminal selection `NON_RECIPE`，不能为了符合方案改写 fixture。

### D062-B：共享 builder

在 RouteAdmitter 现有构建边界内提取最小 private direct-Walk builder。它只复用 `query_support_surfaces()`、`query_standable_connection()`、现有 profile／capability identity 和 validation plan 类型。私有 proof context 只在同次 admission 栈内消费。不得新增 query kind、长期缓存或第二套 provenance。

### D062-C：接入两个入口

local route 改为调用 `admit_local_direct()` 并消费正式接纳结果；NavigationSession 不再查询支撑、扫掠或构造 plan。local 使用唯一 `WALK_LEG` owner。forward-entry single-node equality 在严格身份和 endpoint 对齐后只保留已有 `INITIAL_CONNECTION` owner，不重复 query／recipe／owner，也不创建 terminal leg。

### D062-D：回归和性能

先运行 route admission、validation、Session、ExecutionSupervisor、F1-C 和正式跟随聚焦集合，再运行完整 motion_nav，并与 D060 已登记的既有失败逐项对照。任何新增失败或签名变化先查根因。

本批不重复 D061 长跑，也不刷新已通过的不可变性能证据。先运行 D061 两条工具的低成本回归和短参数正式入口，确认独立场景清理、长 Session 判定、trace、命令记录和门槛代码没有退步。direct builder 只在 admission 运行；运行时无交集帧的查询数保持为 0，forward-entry proof 复用不发起第二次 query。若低成本回归暴露性能口径变化，再单独决定是否建立新证据，不能靠反复长跑刷结果。

### D062-E：独立复审

复审核对 local 路线是否仍有无 plan 的可执行出口、single-node 是否真正复用唯一 proof／owner、selection-only 是否从消费者移除、selected 安全依赖是否保留、身份和退役是否失败关闭，以及性能 source identity 是否覆盖实际修改文件。P0／P1／P2 清零后结束 D062。

D062 不运行 Fabric。它完成后仍要单独处理冷启动／稳定 P95；在该问题有正式方案并通过组件与性能审查前，不再次运行 `straight_2_0`，也不运行其余四个 F1-D 场景。

## 不做的事

- 不修改目标速度、Walk 速度、保持距离、修订节流或规划期限；
- 不修改稳定窗、P95 算法或 1.5 格门槛；
- 不过滤 `UNKNOWN -> AIR`；
- 不允许 local route 绕过 RouteAdmitter／tracker；
- 不增加 follow 专用 proof、恢复预算或 reason 字符串授权；
- 不改写 D060-F 失败目录和原数字。
