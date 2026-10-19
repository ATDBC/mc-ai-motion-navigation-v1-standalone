# 连续高度地面移动实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task by task. 当前对话默认由主 agent 顺序执行。

**Goal：** 在已知地形中实现方向正确的动作交接、连续小高差步行、按真实 tick 计价，以及可由任务入参控制伤害额度的一格升降、连续整格下降和直接多格下落。

**Architecture：** 支撑面继续提供地形事实，后台规划只枚举有必要条件依据的连接。路线接纳使用 1.21 运动计算器生成正式地面或空中证明，局部控制器只执行已经接纳的走廊和动作；现有静止 Step、JumpUp、JumpGap 和 ControlledDrop 保留为后备。

**Tech Stack：** Python 3.11、NumPy 2.4.6、OpenJDK 21、Minecraft Java 1.21、Fabric Loader 0.15.11、Fabric API 0.100.6+1.21、`unittest`。

**Spec：** [连续高度地面移动 V1](../architecture/continuous-height-ground-movement-v1.md)

## 2026-09-27 执行记录

任务 1 至任务 7 的核心实现已经完成，并分别提交。任务 8 已完成组件检查、代表性 Fabric 验证、B03／B07／B09／B10／C1-B／B11／B12-B 回归、文档更新和整理后公开版导出校验。完整速度带、全部授权形状、100 次随机迟到闭环和持续前进参照矩阵仍按验收文档保留为后续补证据项，不因代表场景通过而自动关闭。

本轮保留了 C1-B 性能超限的两次失败记录。修正普通路线减速候选的重复几何验证后，C1-B、B11 和 B12-B 均恢复到现行控制预算内。详细批次和数值见验收文档。

## 全局约束

- 正式实机只使用独立 Fabric；不增加 CraftGround 阶段门槛。
- `TaskDamageBudget` 由任务请求传入，默认允许伤害为 0；修改额度必须增加请求修订并使旧候选失效。
- 未知支撑、未知净空和未知落点继续返回缺信息，不能用临时代价或旧证明放行。
- 已离地的动作始终由原执行器负责到确认落地或明确的不可恢复终态。
- 不静默升级依赖，不增加新进程、GPU、通用轨迹优化器或第二套 1.21 物理规则。
- 平地快速路径保持轻量；控制计算 P95 不超过 8 ms、P99 不超过 15 ms、最大值小于 30 ms。
- 每项功能先写失败测试，再写最小实现。每个任务只提交列出的文件和由其直接更新的文档。

## 审查重点

- **动作交接晚一 tick：** 入口仍在已验证范围内时继续，超出窗口时进入原动作恢复，不能放大入口容差。
- **空中修改目标或伤害额度：** 未离边的旧候选失效；已经离边的执行器继续负责落地，但不能沿用旧任务权限开始新动作。
- **落点从已知变为未知或被占用：** 未开始动作撤销；空中动作报告依赖变化并保持身体责任。
- **半砖、楼梯和短暂离地：** 只有计算器预测过的迈步或腾空可以继续，连续两帧无法解释的离地必须失败。
- **允许伤害的下落：** 缺少生命观察、预计伤害超过额度、预计会耗尽生命或出现未覆盖的减伤机制时必须拒绝。

---

### 任务 1：任务伤害额度与接纳身份

**文件：**

- 新建：`mc2p/motion_nav/motion_risk.py`
- 修改：`mc2p/motion_nav/known_map_planner.py`
- 修改：`mc2p/motion_nav/online_motion.py`
- 修改：`mc2p/motion_nav/motion_residual.py`
- 修改：`mc2p/motion_nav/motion_candidate.py`
- 修改：`mc2p/motion_nav/motion_coordination.py`
- 修改：`mc2p/motion_nav/route_admission.py`
- 修改：`mc2p/motion_nav/action_route_executor.py`
- 修改：`mc2p/motion_nav/navigation_session.py`
- 修改：`mc2p/motion_nav/__init__.py`
- 新建：`tests/motion_nav/test_motion_risk.py`
- 修改：`tests/motion_nav/test_b10_motion_candidate.py`
- 修改：`tests/motion_nav/test_navigation_session.py`

**接口：**

- 产出：`TaskDamageBudget(risk_policy_id: str = "no_expected_damage", maximum_expected_damage_points: float = 0.0)`。
- 产出：`TaskDamageBudget.allows(predicted_damage_points: float, *, health_points: float | None, absorption_points: float | None) -> bool`。
- 产出：`conservative_plain_fall_damage_points(fall_distance_blocks: float) -> float`，普通完整方块落地使用 `max(0, ceil(fall_distance - 3))` 作为不计减伤的上界。
- 修改：`SurfacePlanningRequest.damage_budget: TaskDamageBudget = TaskDamageBudget()`。
- 修改：`StateAnchor` 增加可选的 `health_points` 和 `absorption_points`；正式观察必须填入，旧组件夹具可以省略，但省略时只能接纳零伤害结果。
- 修改：`MotionCandidateContext`、`RouteAdmitter`、`ActionRouteExecutor` 和 `NavigationSession` 传递同一个不可变 `TaskDamageBudget`，不再只用裸字符串授予伤害权限。

- [ ] **步骤 1：写伤害额度失败测试**

  覆盖默认零额度、额度恰好等于预计伤害、少 1 点、非有限值、负值、缺生命观察、伤害会耗尽生命，以及仅改变额度后旧候选被拒绝为 `risk_policy_changed`。

- [ ] **步骤 2：运行测试并确认失败**

  运行：

  ```powershell
  D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_motion_risk tests.motion_nav.test_b10_motion_candidate tests.motion_nav.test_navigation_session -v
  ```

  预期：新类型或新字段尚不存在，测试失败。

- [ ] **步骤 3：实现额度、生命锚点和全链路传递**

  真实锚点从 `ObservationSnapshotV3.self_state` 读取生命与吸收值。几何和轨迹可以跨额度复用，但每次路线接纳都重新调用 `TaskDamageBudget.allows()`。

- [ ] **步骤 4：运行任务 1 检查**

  运行步骤 2 的命令，并追加：

  ```powershell
  D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_b10_online_motion -v
  ```

  预期：全部通过。

- [ ] **步骤 5：提交任务 1**

  提交信息：`feat: add task-scoped motion damage budgets`

### 任务 2：方向性动作入口与同帧交接

**文件：**

- 新建：`mc2p/motion_nav/segment_entry.py`
- 修改：`mc2p/motion_nav/action_route.py`
- 修改：`mc2p/motion_nav/action_route_executor.py`
- 修改：`mc2p/motion_nav/fixed_route.py`
- 修改：`mc2p/motion_nav/route_admission.py`
- 修改：`mc2p/motion_nav/__init__.py`
- 新建：`tests/motion_nav/test_segment_entry.py`
- 修改：`tests/motion_nav/test_b07_step_route.py`
- 修改：`tests/motion_nav/test_b09_air_transitions.py`
- 修改：`tests/motion_nav/test_runtime_navigation_verified_handoff.py`

**接口：**

- 产出：`SegmentEntryWindow`，字段固定为参考点、水平来向、前后范围、最大横向偏差、脚底高度范围、速度范围、最大速度方向误差、允许姿态、允许模式、视角要求和 Profile 身份。
- 产出：`body_fits_segment_entry(window: SegmentEntryWindow, body: BodyState, mode: MovementMode) -> bool`。
- 产出：`physics_fits_segment_entry(window: SegmentEntryWindow, state: PhysicsState, mode: MovementMode) -> bool`。
- 修改：`JumpUpSegment`、`StepSegment`、`JumpGapSegment` 和 `ControlledDropSegment` 携带 `entry_window`；正式 `RouteAdmitter` 必须生成，旧只读夹具允许显式传入 `None`。
- 修改：`FixedRouteConfig.handoff_entry_window: SegmentEntryWindow | None`。存在窗口时，完成条件使用预测落点、速度方向和姿态，不能再压缩为圆形终点容差。

- [ ] **步骤 1：写方向性入口失败测试**

  用当前会失败的“下一整格”复现固定前后范围；再覆盖横向超差、速度过高、来向错误、晚一 tick 后仍在范围内，以及同帧从 Walk 交给下一动作。

- [ ] **步骤 2：运行测试并确认旧圆形容差失败**

  ```powershell
  D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_segment_entry tests.motion_nav.test_b07_step_route tests.motion_nav.test_b09_air_transitions tests.motion_nav.test_runtime_navigation_verified_handoff -v
  ```

- [ ] **步骤 3：实现 `SegmentEntryWindow` 和交接判断**

  入口窗口只表达身体几何和运动条件。命令最早／最晚生效 tick 继续由已验证候选和 Runtime 账本负责。

- [ ] **步骤 4：验证静止动作后备没有扩大**

  运行步骤 2，并运行：

  ```powershell
  D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_jump_up tests.motion_nav.test_b07_step_runtime -v
  ```

- [ ] **步骤 5：提交任务 2**

  提交信息：`feat: hand off routes through directional entry windows`

### 任务 3：连续地面段证明

**文件：**

- 新建：`mc2p/motion_nav/ground_traversal.py`
- 修改：`mc2p/motion_nav/fixed_route.py`
- 修改：`mc2p/motion_nav/physics_1_21.py`（只有测试证明规则缺口时才修改）
- 修改：`mc2p/motion_nav/physics_adapter.py`
- 修改：`mc2p/motion_nav/action_route.py`
- 修改：`mc2p/motion_nav/__init__.py`
- 新建：`tests/motion_nav/test_ground_traversal.py`
- 修改：`tests/motion_nav/test_b09r_physics_step.py`
- 修改：`tests/motion_nav/test_fixed_route_walk.py`

**接口：**

- 产出：`GroundTraversalStatus`，至少包含 `VERIFIED`、`NEEDS_WORLD`、`UNSUPPORTED`、`BLOCKED` 和 `BUDGET_EXHAUSTED`。
- 产出：`GroundTraversalPlan`，保存路线、入口窗口、出口窗口、参考轨迹、允许走廊、依赖格、正式预计 tick、规则和 Profile 身份。
- 产出：`verify_ground_traversal(entry_state: PhysicsState, route: FixedRoute, world: PhysicsWorldView, profile: GroundMotionProfile, *, maximum_ticks: int) -> GroundTraversalResult`。
- 修改：`FixedRoute` 允许保存不同支撑高度；没有 `GroundTraversalPlan` 时，`FixedRouteController.start()` 仍拒绝变化高度，保留 B03 的旧边界。
- 修改：`WalkSegment.traversal_plan: GroundTraversalPlan | None`。

- [ ] **步骤 1：写连续地面闭环失败测试**

  覆盖连续上下半砖、楼梯、草方块／土径、地毯、B07 三种积雪、自动迈步中的短暂离地，以及未知净空和不支持形状。

- [ ] **步骤 2：运行测试并确认当前固定高度契约失败**

  ```powershell
  D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_ground_traversal tests.motion_nav.test_fixed_route_walk tests.motion_nav.test_b09r_physics_step -v
  ```

- [ ] **步骤 3：实现纯计算验证器**

  使用 `project_movement_command()` 和 `physics_1_21.step()`。验证器只能消费真实可发送输入；物理结果未知或不支持时返回有类型结果，不生成证明。

- [ ] **步骤 4：固定旧 B03 路线边界**

  增加测试证明：普通 `FixedRoute` 仍不能绕过证明直接改变高度；带 `GroundTraversalPlan` 的路线才允许变化高度。

- [ ] **步骤 5：运行任务 3 检查并提交**

  运行步骤 2。提交信息：`feat: verify continuous ground traversal plans`

### 任务 4：小高差规划边与整数 tick 代价

**文件：**

- 修改：`mc2p/motion_nav/known_map_planner.py`
- 修改：`mc2p/motion_nav/planner_worker.py`
- 修改：`mc2p/motion_nav/route_admission.py`
- 修改：`mc2p/motion_nav/navigation_session.py`
- 修改：`mc2p/motion_nav/action_route.py`
- 新建：`tests/motion_nav/test_continuous_height_planning.py`
- 修改：`tests/motion_nav/test_b07_surface_planning.py`
- 修改：`tests/motion_nav/test_known_map_planning.py`
- 修改：`tests/motion_nav/test_motion_benchmarks.py`
- 修改：`scripts/benchmark_surface_planner.py`

**接口：**

- 修改：`SurfaceWalkEdge` 可以连接高差不超过 0.6 格的已授权支撑面，并标记是否需要正式地面证明。
- 产出：`seconds_to_planning_ticks(seconds: float) -> int`，所有表面规划边使用正整数 tick；`SurfaceRouteCandidate.total_cost_ticks` 成为搜索真值，`total_cost_seconds` 只作为换算后的报告值。
- 产出：有界的 `GroundTraversalProofCache`，键包含规则版本、Profile、入口速度带、方向和归一化局部几何；依赖修订变化后不得命中。
- 修改：所选路线的临时代价必须在接纳前被正式预计 tick 替换。正式代价改变路线优先级时，后台在同一请求预算内重新比较；同一失败边在同一世界修订下最多验证两次。

- [ ] **步骤 1：写规划失败测试**

  固定当前“单块半砖绕路”复现；增加 A* 与零启发 Dijkstra 的 1,000 张固定种子小图，以及不同终点的 100×100 代表图。

- [ ] **步骤 2：运行测试并确认当前 Step 常数代价选择绕路**

  ```powershell
  D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_continuous_height_planning tests.motion_nav.test_b07_surface_planning tests.motion_nav.test_known_map_planning tests.motion_nav.test_motion_benchmarks -v
  ```

- [ ] **步骤 3：实现候选边、正式代价回填和有界缓存**

  缺信息、明确阻塞和不支持的边不能进入临时代价路径。缓存满时按最近最少使用淘汰，不能阻塞控制线程。

- [ ] **步骤 4：验证性能和最优性**

  运行步骤 2，并运行：

  ```powershell
  D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/benchmark_surface_planner.py
  ```

  预期：固定 100×100 平地加小高差代表图不超过 500 ms；A* 与 Dijkstra 的最小 tick 一致。

- [ ] **步骤 5：提交任务 4**

  提交信息：`feat: plan verified low-height ground traversal`

### 任务 5：连续地面在线跟踪

**文件：**

- 修改：`mc2p/motion_nav/fixed_route.py`
- 修改：`mc2p/motion_nav/action_route_executor.py`
- 修改：`mc2p/motion_nav/navigation_session.py`
- 新建：`tests/motion_nav/test_continuous_height_execution.py`
- 修改：`tests/motion_nav/test_navigation_session.py`
- 修改：`scripts/step_transition_runtime.py`

**接口：**

- 修改：`FixedRouteController.start(route, frame, *, traversal_plan: GroundTraversalPlan | None = None)`。
- 平地且远离高度变化时继续使用现有 `predict_ground()` 快路径；接近计划中的高度变化时，用 1.21 计算器对本帧有限候选输入做短期 rollout。
- 计划内短暂离地继续；无对应预测、连续两帧无法解释的离地、越出走廊或依赖变化进入有类型失败／恢复。

- [ ] **步骤 1：写不中停执行失败测试**

  复用第三方基线的五条路线，覆盖正常输入和每条命令 20% 概率晚 1 tick。断言中途不重新降到 0.1 格／秒及以下，用时不超过持续前进参照的 1.3 倍。

- [ ] **步骤 2：运行测试并确认现有控制器停稳或失败**

  ```powershell
  D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_continuous_height_execution tests.motion_nav.test_navigation_session -v
  ```

- [ ] **步骤 3：实现局部完整物理跟踪和恢复**

  每帧只模拟固定数量的输入候选和固定预测长度。后台证明尚未完成时，继续当前已接纳路线或按现有规则停在安全支撑，不能在控制线程等待。

- [ ] **步骤 4：运行平地、战斗和放置回归**

  ```powershell
  D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_fixed_route_walk tests.motion_nav.test_runtime_navigation_verified_handoff tests.motion_nav.test_b11_world_change_navigation -v
  ```

- [ ] **步骤 5：提交任务 5**

  提交信息：`feat: track verified continuous-height routes`

### 任务 6：通用带速升降求解

**文件：**

- 修改：`mc2p/motion_nav/motion_solver.py`
- 修改：`mc2p/motion_nav/motion_worker.py`
- 修改：`mc2p/motion_nav/motion_coordination.py`
- 修改：`mc2p/motion_nav/motion_candidate.py`
- 修改：`mc2p/motion_nav/action_route.py`
- 修改：`mc2p/motion_nav/controlled_drop.py`
- 修改：`mc2p/motion_nav/route_admission.py`
- 修改：`mc2p/motion_nav/action_route_executor.py`
- 修改：`mc2p/motion_nav/navigation_session.py`
- 修改：`mc2p/motion_nav/__init__.py`
- 新建：`config/motion-navigation/verified-height-transitions-v1.json`
- 新建：`tests/motion_nav/test_air_transition_solver.py`
- 修改：`tests/motion_nav/test_b10_gap_solver.py`
- 修改：`tests/motion_nav/test_b10_motion_worker.py`
- 修改：`tests/motion_nav/test_b10_motion_candidate.py`

**接口：**

- 产出：`MotionSolveKind`，首批为 `JUMP_GAP`、`JUMP_UP`、`CONTROLLED_DROP`。
- 产出：`AirTransitionSolverPolicy`，按动作种类保存有界命令模板、入口速度范围、方向误差和最大求解 tick；从 `verified-height-transitions-v1.json` 加载。
- 产出：`AirTransitionSolveRequest(kind, direction, landing, execution_window, damage_budget, exit_direction, exit_motion_ticks, max_candidates, max_ticks)`。
- 产出：`VerifiedAirTransitionSegment`，供新求解的 JumpUp 和 ControlledDrop 使用；旧 `JumpUpSegment`、`JumpGapSegment`、`ControlledDropSegment` 继续承载原有静止或既有 Profile 路径。
- 修改：`VerifiedMotionResult` 保存 `kind`、对应的 `AirTransitionSolverPolicy` 和所有起步变体中的最大预计伤害；现有 `GapSolveRequest` 作为兼容构造入口，内部转成 `AirTransitionSolveRequest`，B10 证据语义不变。
- 产出：`solve_air_transition(anchor: StateAnchor, world: PhysicsWorldView, request: AirTransitionSolveRequest) -> SolveResult`。
- 修改：候选接纳同时检查轨迹、入口、世界依赖、额度、最新生命和执行窗口。

- [ ] **步骤 1：写带速 JumpUp／ControlledDrop 求解失败测试**

  覆盖低、中、高三个速度带、四方向、晚一 tick、错误来向、未知净空、错误落点、零额度和正额度。

- [ ] **步骤 2：运行测试并确认当前求解器只支持 JumpGap**

  ```powershell
  D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_air_transition_solver tests.motion_nav.test_b10_gap_solver tests.motion_nav.test_b10_motion_worker tests.motion_nav.test_b10_motion_candidate -v
  ```

- [ ] **步骤 3：抽取通用求解和验证路径**

  保留现有 B10 命令投影、延迟起步变体、逐 tick 回执和释放输入后的落地证明。JumpUp 和下降只能增加自己的候选模板与完成条件，不能复制另一套执行器。

- [ ] **步骤 4：验证旧 B10 结果不变**

  运行步骤 2；现有跨隙求解的命令序列、窗口和失败分类保持兼容。

- [ ] **步骤 5：提交任务 6**

  提交信息：`feat: solve verified moving height transitions`

### 任务 7：连续整格下降与直接多格下落

**文件：**

- 修改：`mc2p/motion_nav/known_map_planner.py`
- 修改：`mc2p/motion_nav/motion_solver.py`
- 修改：`mc2p/motion_nav/motion_coordination.py`
- 修改：`mc2p/motion_nav/action_route_executor.py`
- 修改：`mc2p/motion_nav/navigation_session.py`
- 修改：`mc2p/motion_nav/route_admission.py`
- 修改：`config/motion-navigation/verified-height-transitions-v1.json`
- 新建：`tests/motion_nav/test_continuous_descent.py`
- 修改：`tests/motion_nav/test_b09_air_transitions.py`
- 修改：`tests/motion_nav/test_navigation_session.py`
- 新建：`scripts/continuous_descent_runtime.py`

**接口：**

- 表面规划器允许相邻列的一格下降形成可求解下降边，也允许同一离边方向连接到中间无计划支撑的更低最终落点。
- 连续 2、4、8 级整格台阶保存为多个下降连接，但协调器至少提前准备下一条证明；当前落地状态已经进入下一条入口时，同一控制链下一帧继续。
- 直接多格下落从离边到最终落点只生成一个 `VerifiedMotionResult`，其中 `maximum_expected_damage_points` 是整段保守上界。
- 已离边后修改目标、额度或世界依赖时，当前证明不再授权新的主动输入序列，但执行器继续按已经验证的释放／落地责任控制身体。

- [ ] **步骤 1：写连续下降和多格下落失败测试**

  场景固定为连续 2、4、8 级整格台阶，直接下落 1、2、3 格，以及至少一个产生伤害的高度。断言直接下落只有一次完成记录，连续台阶没有级间停稳。

- [ ] **步骤 2：运行测试并确认当前链路逐级停稳或拒绝多格下落**

  ```powershell
  D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_continuous_descent tests.motion_nav.test_b09_air_transitions tests.motion_nav.test_navigation_session -v
  ```

- [ ] **步骤 3：实现下降枚举、前瞻求解和空中责任**

  没有任务额度时只生成零伤害候选。正额度候选使用普通完整方块落地的保守伤害上界；遇到未覆盖的特殊落地材质、流体、减伤效果或主动改变世界返回 `UNSUPPORTED`。

- [ ] **步骤 4：验证中断和接续**

  覆盖目标修订、额度修订、首条命令晚一 tick、空中缺输入、落点被占用和落地后立即直行。下一段入口已满足时，确认落地后的下一控制帧必须继续。

- [ ] **步骤 5：提交任务 7**

  提交信息：`feat: execute continuous and multi-block descents`

### 任务 8：Fabric 验收、回归和公开版同步准备

**文件：**

- 修改：`scripts/step_transition_runtime.py`
- 修改：`scripts/air_motion_runtime.py`
- 修改：`scripts/continuous_descent_runtime.py`
- 修改：`docs/motion_navigation/acceptance/continuous-height-ground-movement.md`
- 修改：`docs/motion_navigation/stages/continuous-height-ground-movement-plan.md`
- 修改：`docs/motion_navigation/decisions/0036-continuous-height-movement-before-new-capabilities.md`
- 修改：`AGENTS.md`
- 修改：`config/motion-navigation/standalone-export-v1.json`

**接口：**

- 结构化证据必须记录场景、固定种子、入口速度带、方向、任务伤害额度、预计／实际伤害、动作完成次数、各级落地、控制耗时和输入应用窗口。
- 组件、闭环模拟和 Fabric 结果分别报告，不能互相代替。

- [ ] **步骤 1：运行全部组件检查**

  ```powershell
  D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest discover -s tests/motion_nav -p 'test_*.py' -v
  D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_segmented_trace tests.test_standalone_java_gates -v
  ```

- [ ] **步骤 2：运行分阶段 Fabric 验收**

  先运行入口交接和连续小高差，再运行带速一格升降，最后运行连续整格下降和直接多格下落。任一阶段未通过时停止扩大范围，保留失败证据。

- [ ] **步骤 3：运行既有回归**

  至少覆盖 B03、B07、B09、B10、C1-B、B11 和 B12-B 的现行代表场景。报告控制计算 P50、P95、P99、最大值，以及规划和求解耗时。

- [ ] **步骤 4：更新四类文档和公开清单**

  文档只写实际完成和实测结果。尚未通过的速度带、方向、伤害额度或地形继续标为未授权。

- [ ] **步骤 5：验证整理后公开版**

  ```powershell
  D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/motion_navigation_references.py verify --snapshot-root output/github-navigation-code-share-v2
  D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/public_runtime_evidence.py verify --root evidence/motion_navigation/representative-v1
  git diff --check
  ```

- [ ] **步骤 6：提交任务 8**

  提交信息：`docs: record continuous-height movement acceptance`

## 实施停止条件

出现以下任一情况时，停止扩大范围，保留已经通过的前一任务：

- 需要修改正式视觉权限或把未知当空气；
- 需要新增流体、梯子、缓降、装备减伤或主动放置方块；
- 平地控制性能超过既有门槛且局部优化无法恢复；
- 必须绕开 Runtime 唯一输入出口；
- 旧 B09/B10 静止或跨隙证据因接口修改失效。
