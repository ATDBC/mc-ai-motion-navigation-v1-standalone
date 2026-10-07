# Non-Center Ground Route Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让普通地面控制器可靠执行任意浮点坐标折线，并以同一能力完成贴墙和平台边缘终点。

**Architecture:** 保留现有 `FixedRoute`、连续折线投影和开阔地快速预测。快速候选因已知水平碰撞或支撑边界失败时，最多用 1.21 计算器复核三个候选；潜行防坠由路线进度区间显式声明。终点只提供原目标内的完成区域。

**Tech Stack:** Python 3.12、`unittest`、现有 1.21 物理计算器、Windows PowerShell、Fabric 1.21。

**Spec:** `docs/motion_navigation/architecture/continuous-ground-route-execution-v1.md`

## Global Constraints

- Windows 是正式平台；三方 Linux 结果只作可移植性补充。
- 不改变中心点搜索，不实现路线优化器。
- 不扩大 `GoalState`，不降低未知、危险、支撑或严格动作门槛。
- 不修改 Session、ExecutionSupervisor 或 Runtime 生命周期。
- 普通开阔路线保持快速路径；每帧最多完整复核三个候选。
- 候选选择不能依赖墙钟。
- 按 RED→GREEN→REFACTOR 实施；先跑代表小场景，稳定后才跑完整矩阵。

## Review Focus

- 中途正面撞墙必须失败，不能被安全接触误收。
- 完整复核只读取同一观察的正式 `PhysicsState`；缺少状态不能放宽。
- 潜行只能在路线声明区间内启用，取消或离开区间后必须释放。
- 开阔路线不能进入完整复核，也不能出现逐帧变化。
- 完整输入租约安全但停止尾迹越界时，候选必须失败。

---

### Task 1: 冻结 F2-0 基线和门禁

**Files:**
- Modify: `tests/sim/product_cases.py`
- Create: `tests/sim/manifests/navigation-product-f2-ground-route-v1.json`
- Create: `tests/motion_nav/test_f2_ground_route_gates.py`
- Create: `scripts/f2_ground_route_evidence.py`
- Create: compact baseline under `evidence/motion_navigation/F2-ground-route-v1/baseline/`

**Interfaces:**
- Produces: 固定任务 ID、路线点、目标框、迟到模型和错误副本；后续任务不得更改。

- [ ] 写任意坐标路线、玩家站位和错误副本测试；先运行并确认错误副本失败。
- [ ] 记录当前行为、失败原因、候选数量、路线进度、横向误差和耗时。
- [ ] 冻结种子 187、开阔路线、连续高度和严格动作对照。
- [ ] 提交，不修改生产行为。

**Fail-fast checks:**

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_f2_ground_route_gates -v
.\.venv\python.exe scripts/f2_ground_route_evidence.py --baseline --output .tmp/f2-ground-baseline --workers 4
```

### Task 2: 增加通用完整复核

**Files:**
- Modify: `mc2p/motion_nav/safe_ground_control.py`
- Modify: `mc2p/motion_nav/fixed_route.py`
- Create: `tests/motion_nav/test_f2_non_center_ground_route.py`
- Modify: `tests/motion_nav/test_fixed_route_walk.py`

**Interfaces:**
- Consumes: `FixedRouteController.decide(..., physics_state=..., query_cache=...)` 和现有 `_RouteGeometry`。
- Produces: `VerifiedGroundRouteCandidate` 纯结果；包含最终状态、最小支撑、依赖、缺失格、进度、横向误差、step 数和拒绝类别。

- [ ] RED：偏移直线、斜线、中心转非中心、沿墙切线和墙角路线能完成。
- [ ] RED：中途正面墙、危险／未知／流体／未支持墙、走廊越界和停止尾迹越界被拒绝。
- [ ] RED：缺少正式物理状态时不进入完整复核；开阔路线完整复核计数为零。
- [ ] 实现完整输入租约和停止尾迹复核，复用当前帧查询缓存。
- [ ] 在快速候选失败后按稳定顺序最多复核三个候选；只接受仍沿路线进展或进入完成区域的结果。
- [ ] 跑代表场景和微基准；P95 不通过就停止，不进入 Task 3。
- [ ] 提交。

**Fail-fast checks:**

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_f2_non_center_ground_route tests.motion_nav.test_fixed_route_walk -v
.\.venv\python.exe scripts/f2_ground_route_evidence.py --families offset diagonal tangent corner --output .tmp/f2-ground-general --workers 4
```

### Task 3: 增加路线能力区间和潜行防坠

**Files:**
- Create: `mc2p/motion_nav/ground_route_execution.py`
- Modify: `mc2p/motion_nav/fixed_route.py`
- Modify: `mc2p/motion_nav/safe_ground_control.py`
- Create: `tests/motion_nav/test_f2_ground_route_edge_guard.py`
- Test: `tests/motion_nav/test_navigation_supervised_interruptions.py`
- Test: `tests/motion_nav/test_external_motion_recovery.py`

**Interfaces:**
- Produces: `GroundRouteCapability.SNEAK_EDGE_GUARD`、`GroundRouteCapabilityInterval`、`GroundRouteExecutionContract`。
- Contract location: optional field on `FixedRoute`; intervals use route cumulative progress.

- [ ] RED：声明区间的非中心边缘路线使用潜行并完成。
- [ ] RED：未声明、区间外、支撑移除、低顶、取消、修订、失联和外力有界退出。
- [ ] 实现不可变能力区间和查询；不增加 Session 状态。
- [ ] 用完整复核确认边缘裁剪、路线进展和停止尾迹。
- [ ] 离开区间或终点后停止潜行，正式观察确认允许姿态后完成。
- [ ] 提交。

**Fail-fast checks:**

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_f2_ground_route_edge_guard tests.motion_nav.test_navigation_supervised_interruptions tests.motion_nav.test_external_motion_recovery -v
```

### Task 4: 以完成区域交付终点接近

**Files:**
- Modify: `mc2p/motion_nav/ground_route_execution.py`
- Modify: `mc2p/motion_nav/support_surfaces.py`
- Modify: `mc2p/motion_nav/action_route.py`
- Modify: `mc2p/motion_nav/route_admission.py`
- Modify: `mc2p/motion_nav/action_route_executor.py`
- Create: `tests/motion_nav/test_f2_ground_completion_region.py`
- Modify: `tests/motion_nav/test_r28_screening_retirement.py`
- Test: `tests/motion_nav/test_d059_terminal_selection_dependencies.py`
- Test: `tests/motion_nav/test_d060_terminal_node_exact_proof.py`
- Test: `tests/motion_nav/test_d062_direct_walk_validation.py`
- Test: `tests/motion_nav/test_d064_ground_direct_handoff.py`

**Interfaces:**
- Produces: `GroundCompletionRegion` attached to the final `FixedRoute` contract.
- Completion: region membership plus the original `GoalState`; reference point only guides tracking.

- [ ] RED：开阔非格心、正面墙、沿墙、墙角、走廊尽头、边缘 0.1／0.2 通过。
- [ ] RED：无合法交集、危险墙、未知墙、低顶和中途墙保持拒绝。
- [ ] 实现精确矩形交集、入射投影、边界余量和平手顺序。
- [ ] 最终 Walk 绑定同一份区域、能力区间和依赖；不恢复后台筛选。
- [ ] 完成判断读取整个区域，随后仍由原 `GoalState` 正式确认。
- [ ] 重跑 D059、D060、D062、D064 和 screening retirement。
- [ ] 提交。

**Fail-fast checks:**

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_f2_ground_completion_region tests.motion_nav.test_r28_screening_retirement -v
.\.venv\python.exe -m unittest tests.motion_nav.test_d059_terminal_selection_dependencies tests.motion_nav.test_d060_terminal_node_exact_proof tests.motion_nav.test_d062_direct_walk_validation tests.motion_nav.test_d064_ground_direct_handoff -q
```

### Task 5: 正式组合与实机验收

**Files:**
- Modify: `docs/motion_navigation/architecture/continuous-ground-route-execution-v1.md`
- Modify: `docs/motion_navigation/stages/F2-non-center-ground-route-execution.md`
- Modify: `docs/motion_navigation/acceptance/F2-non-center-ground-route-execution.md`
- Modify: `docs/motion_navigation/decisions/0074-generalize-ground-route-execution-before-terminal-approach.md`
- Modify: `AGENTS.md`
- Create: `scripts/f2_ground_route_runtime.py`
- Create: compact evidence under `evidence/motion_navigation/F2-ground-route-v1/`

**Interfaces:**
- Produces: Windows 正式行为来源、性能记录、Fabric 结果和证据清单。

- [ ] 运行非中心路线和玩家站位全矩阵。
- [ ] 运行 Windows 完整正序、逆序和五组行为集合。
- [ ] 运行或依法沿用 D058、D061，并报告完整复核专项性能。
- [ ] 运行 Fabric 非中心中途路线、墙面终点、边缘终点和冻结反例。
- [ ] 更新四类文档，保留原失败和旧基线。
- [ ] 提交并保持工作树干净，不推送远端。

**Formal checks:**

```powershell
.\.venv\python.exe scripts/run_motion_navigation_checks.py --output .tmp/f2-forward.json
.\.venv\python.exe scripts/run_motion_navigation_checks.py --reverse --output .tmp/f2-reverse.json
.\.venv\python.exe scripts/navigation_s0r_evidence.py --set faults --output .tmp/f2-faults --workers 4
.\.venv\python.exe scripts/navigation_s0r_evidence.py --set world_changes --output .tmp/f2-world --workers 4
.\.venv\python.exe scripts/navigation_s0r_evidence.py --set follow --output .tmp/f2-follow --workers 4
.\.venv\python.exe scripts/navigation_s0r_evidence.py --set coordination --output .tmp/f2-coordination --workers 4
.\.venv\python.exe scripts/navigation_s0r_evidence.py --set product --output .tmp/f2-product --workers 4
.\.venv\python.exe scripts/f2_ground_route_runtime.py --output-root artifacts/f2-ground-route
```
