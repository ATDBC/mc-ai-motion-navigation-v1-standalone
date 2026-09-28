# 重构与验证方案：把不变量变成机制

日期：2026-09-28
对象：`origin/main` 提交 [`8898cf9`][base]
依据：[反复出现高优先级问题的原因分析](2026-09-28-recurring-defects-analysis.md)
性质：外部审查者给出的方案。不属于 `docs/motion_navigation/` 四类正式文档；采纳后，应由项目方改写为正式的 architecture、decisions 和 stages 文档。

## 结论

1. **分五步推进，先做工具，再做机制，最后拆分会话。**

   | 步骤 | 内容 | 消除的缺陷类型 | 规模 |
   |---|---|---|---|
   | 0 | 正式路径闭环模拟、扰动注入、不变量监视器 | 让 A 到 D 四类问题在提交前暴露 | 中 |
   | 1 | 身体控制权协议与执行监督者 | A：中断时的控制权交接 | 中 |
   | 2 | 动作边界前置条件 | D：门槛放错层 | 小到中 |
   | 3 | 统一的重试与额度账本 | B：重试、额度和期限的上限 | 小 |
   | 4 | 按状态归属拆分会话，显式状态转移表 | 让以后的新能力不再改会话多条路径 | 大 |

   - 步骤 1、2、3 互相独立，都只依赖步骤 0，可以并行。步骤 4 在它们之后进行。
   - 步骤 0 到 3 完成之前，暂停新增能力，例如新的动作类型、新的地形或战斗新行为。
2. **步骤 0 已经做出可运行的原型，结果说明方案可行。**
   - 原型在 [`2026-09-28-refactor-plan/harness/`](2026-09-28-refactor-plan/harness/)，共约 800 行。它走完整的正式链路：`PlayerRuntimeV1 → RuntimeNavigationDriver → NavigationSession` → 规划 → 接纳 → 执行器 → 动作求解。
   - 只把游戏换成了 1.21 运动计算器，外加一个简化的观察模型，其余都是正式代码。
   - 14 个场景共约 6 秒跑完：
     - 平地、连续小高差、20% 输入迟到、连续整格下降、探边加直接下落、5 格带额度下落、空中取消，全部通过，并与 Fabric 代表结果的行为一致；
     - 第十八轮的两个 P0 在正式链路上重现：探边超时，或探边中改目标，身体都从坑边掉下 11 格，受伤 8 点，而额度为 0；
     - 阶梯下坡失败，与第十八轮第三节一致。
   - **原型还发现了一个此前没人报告的问题：**目标区域已经包含身体所站的支撑面时，规划器抛出契约异常，任务以 `planning_internal_error` 失败。例如“到我身边来”而机器人已经在旁边，或追击目标贴身时。
3. **所有接口都从现有代码中长出来，不引入框架。**
   - `ActionRouteExecutor.requires_safe_handoff` 已经是“安全交出”的雏形。
   - B12-A 的“跨类别失败总数”已经是重试账本的雏形。
   - 方案只是把它们提升为全项目共用的约定，并让会话只通过这些约定工作。

## 一、目标与约束

**目标：**
- 新增一种控制身体的方式时，只实现一个接口。
- 新增一种中断时，只在一处写处理规则。
- 新增一种计数时，只使用一个账本。
- 每项不变量都能在正式链路上被自动检查。

**约束（来自 AGENTS.md 与已有决定）：**
- 只有 Runtime 仲裁后的唯一输入出口写玩家控制。本方案不改变 Runtime、仲裁器和输入账本。
- 每类长期状态只有一个拥有者。
- 不用 `reason` 字符串或散落的布尔位决定生命周期。
- 不提前建立空框架。每个新抽象都对应第二节统计中至少 5 次重复出现的缺陷。
- 每一步都小，能单独验收，并且不降低现有安全门槛。

## 二、步骤 0：正式路径闭环模拟（原型已验证）

### 2.1 结构

```
场景（地形、起点、目标、额度、扰动、事件）
      │
      ▼
PlayerRuntimeV1 ──► RuntimeNavigationDriver ──► NavigationSession ──► 规划器 / 接纳 / 执行器 / 求解器
      ▲                                                                         （全部为正式代码）
      │ 观察 V3 + 输入回执 V3
CalculatorBackend：每个控制帧用 physics_1_21.step 推进一个运动 tick
      │           观察模型：方块 + 空气查询（视野、遮挡、下部可见）
      ▼
InvariantMonitor：每 tick 检查不变量
```

**替换了什么：**

- **游戏：**`CalculatorBackend`（[`sim_backend.py`](2026-09-28-refactor-plan/harness/sim_backend.py)）。
  - 物理使用仓库自己的 1.21 计算器。
  - 观察通过仓库自己的 V3 载荷夹具生成，并经过正式解码器。
  - 回执带逐 tick 的输入应用样本，Runtime 的输入账本、状态锚点和残差都按正式方式工作。
- **后台进程：**使用同步版本，但执行同一个函数：
  - `InlinePlannerWorker` 调用 `planner_worker._execute_job`，错误包装与真实规划进程相同；
  - `InlineMotionWorker` 调用 `motion_worker._execute_job`。

**观察模型的限制（写在文件头）：**
- 没有实体、流体和透明规则；
- 视野按 120°×120°；
- 遮挡用 0.05 格步进检测。

它不能代替 Fabric，也发现不了“计算器与游戏不一致”这一类问题。它的用途是在提交前，找出控制逻辑和生命周期层面的问题。

### 2.2 原型结果（8898cf9）

完整输出见 [`matrix-8898cf9.txt`](2026-09-28-refactor-plan/matrix-8898cf9.txt)。

| 场景 | 期望 | 结果 | 不变量违反 |
|---|---|---|---|
| 平地 8 格 | 成功 | 成功，64 tick | — |
| 平地，起点偏 (0.33, −0.29) | 成功 | 成功 | — |
| **目标区域已包含身体** | 成功 | **失败：`planning_internal_error`** | —（新问题） |
| 连续上下两个半砖 | 成功 | 成功 | — |
| 同上，20% 输入迟到 | 成功 | 成功 | — |
| 连续四级整格下降 | 成功 | 成功 | — |
| 2 格直接下落（探边） | 成功 | 成功，76 tick | — |
| 5 格直接下落，额度 2 点 | 成功 | 成功，实际伤害 2 点 | — |
| 2 格下落，探边刚开始时改目标回起点 | 成功 | **失败：`planning_internal_error`** | —（同上新问题） |
| 2 格下落，空中取消 | 取消 | 取消，安全落地 | — |
| 2 格下落，20% 输入迟到 | 成功 | 成功 | — |
| L 形走道远处落点（第十八轮） | 成功 | 失败：探边超时 | **无主离地；坠落 11 格；伤害 8 点超额度 0 点** |
| 同上，第 30 tick 改目标 | 成功 | 失败：`current_body_cannot_connect` | **无主离地；坠落 11 格；伤害 8 点** |
| 两道 3 格坎的阶梯下坡 | 成功 | 失败：信息等待超时 | — |

**说明：**
- 探边、横移、松开潜行、恢复视角、已验证下落、落地后走到目标的完整链路，在模拟中一次跑通，与第十八轮的 Fabric 描述一致。这说明模拟的保真度足够发现生命周期问题。
- 第十八轮的两个 P0 这次是在正式链路中重现的，不是组件级复现。

### 2.3 产品化要做的事

1. 把 `harness/` 移入 `tests/sim/`，并随 `tests/motion_nav` 一起运行。
2. **为测试替身开放明确的注入点，不再依赖私有字段：**
   - `motion_coordination.py:387` 的 `type(worker) is not MotionSolverWorker` 改为一个只含 `submit`、`poll_available`、`close`、`is_alive` 的小协议；原型目前靠改写模块名绕过；
   - 为适配器增加仅测试可用的“预置世界记忆”入口，原型目前写的是 `adapter._world`；
   - 会话提供只读的 `current_controller`、`request_generation`、`probe_state` 属性，原型目前读的是 `_executor`、`_request`、`_edge_probe`。
3. **场景族生成器**（第十六至十八轮用过的地形都要覆盖）：
   - 宽场地加小高差；
   - 多道坎的阶梯山坡；
   - 路线旁有坑；
   - 平台一侧有墙；
   - L 形走道；
   - 半砖、土径、积雪混合地面；
   - 远处落点。
4. **扰动类型**（原型已实现前四种）：
   - 输入迟到（固定、随机）；
   - 任意时刻改目标或取消；
   - 世界变化（方块出现或消失）；
   - 外力冲量；
   - 缺锚点、观察缺失。
5. **两档运行规模：**
   - 每次提交跑固定矩阵，目标在 60 秒内；
   - 阶段关闭前跑冻结种子的扰动扫描，例如每个场景族 × 每种扰动 × 100 个种子。
6. **每个 Fabric 代表场景都要有一个模拟孪生场景。**两者结论不一致时，先查模拟模型，再判断哪一边有错。模拟不能替代 Fabric 结论。

### 2.4 不变量清单

原型实现了 I1 到 I4，其余在产品化时补齐。

| 编号 | 不变量 | 检查方式 |
|---|---|---|
| I1 | 身体离开支撑时，必须有负责落地的控制者 | 离地 tick 的当前控制者是空中动作，或允许短暂离地的地面证明段 |
| I2 | 非计划坠落为 0 | 无主离地之后落地，且高度差 ≥ 1 格 |
| I3 | 已承受的运动伤害不超过任务额度 | 后端按原版规则统计摔落伤害 |
| I4 | 非终态下，身体不会长时间原地不动 | 连续 100 tick 位移 < 0.01 格 |
| I5 | 同一进度状态下，重规划次数有上限 | 同一支撑面、同一几何修订下的请求数 |
| I6 | 每一帧的移动输入都有明确的控制者 | 提案携带 `controller_id`（步骤 1 之后可以检查） |
| I7 | 接纳的路线中，每条需要证明的边都有证明 | 接纳结果自检 |
| I8 | 只有后备搜索也失败，才能报告“无路” | 失败分类核对 |
| I9 | 不出现契约异常形式的失败 | `planning_internal_error` 或 `ContractViolation` 计为违反 |

**步骤 0 完成条件：**
- 矩阵进入测试；
- I1 到 I9 全部实现；
- 当前代码在矩阵上的失败项与第 2.2 节一致，作为已知失败登记。后续步骤逐项转为通过。

## 三、步骤 1：身体控制权协议（消除 A 类）

### 3.1 接口

```python
class StopCause(StrEnum):
    GOAL_REVISED = "goal_revised"
    ROUTE_REPLACED = "route_replaced"
    CANCELLED = "cancelled"
    DEPENDENCY_CHANGED = "dependency_changed"
    INPUT_LOST = "input_lost"
    TIMED_OUT = "timed_out"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class ControlDecision:
    controller_id: str
    movement: MovementV1
    look: LookV1 | None
    state: ControllerState          # RUNNING / STOPPING / FINISHED / FAILED / NEEDS_INFORMATION / NEEDS_REPLAN
    reason: ControllerReason        # 有类型，只用于报告
    missing_cells: tuple[BlockPos, ...] = ()
    verified_command: VerifiedCommandRef | None = None


class BodyController(Protocol):
    controller_id: str

    def decide(self, frame: NavigationFrame, anchor: StateAnchor | None,
               ledger: InputApplicationLedger | None) -> ControlDecision: ...

    def request_stop(self, cause: StopCause) -> None:
        """只登记，不立刻交出控制；之后每帧由 decide 负责收尾。"""

    def safe_to_release(self, frame: NavigationFrame) -> bool:
        """身体在支撑上、水平速度 ≤ 阈值、没有在途的已验证命令、与支撑重叠有余量。"""
```

### 3.2 执行监督者

从会话中抽出 `ExecutionSupervisor`，它是唯一持有“当前控制者”的对象。

| 事件 | 规则 |
|---|---|
| 改目标、替换路线、取消、依赖变化、超时 | 对当前控制者调用 `request_stop(cause)`；把新控制者登记为待接替者 |
| 每一帧 | 当前控制者尚未 `safe_to_release` 时，只采用它的 `decide`；一旦安全，就换成待接替者（没有则为空），同一帧立即调用新控制者的 `decide`，避免多出一帧中性输入 |
| 控制者报告 `FINISHED`、`FAILED`、`NEEDS_REPLAN` | 只有在 `safe_to_release` 为真后，才把结果交给会话状态机 |
| `INPUT_LOST` | 当前控制者先负责到安全状态，再以终态报告 |

**需要实现这个接口的对象：**

| 对象 | 现状 | 改动 |
|---|---|---|
| `ActionRouteExecutor` | 已有 `requires_safe_handoff`、`cancel` | 包装为 `BodyController`。`safe_to_release` 就是 `not requires_safe_handoff`；地面 Walk 再加上“速度 ≤ 阈值或下一段已接管” |
| `LandingEdgeProbe` | 结束时直接交出（第十八轮 P0） | **新增收尾阶段：**保持潜行，只允许远离边缘的方向键或中性，直到速度 ≤ 0.1 格/秒、与支撑重叠 ≥ 0.15 格，才能 `safe_to_release` |
| 外力恢复、放置站位（技能层） | 各自登记 Runtime 源 | 第二期再迁移，使用同一接口 |

**会话中需要改写的路径**（8898cf9 行号）：
- `_clear_active_execution` 与 `_retire_route` 的 13 处调用（940、1203、1210、1219、1225、1238、1243、1261、1356、1392、1418、1465、1737 行），以及 `_end_edge_probe` 的 8 处调用（776、794、1276、1323、1328、1354、1696、1741 行），全部改为 `supervisor.request_stop(cause)`。只有 supervisor 自己能清空当前控制者。
- `propose` 中针对 `INPUT_LOST`、`NEEDS_REPLAN`、`CANCELLING` 的分支，改为读取 supervisor 报告的结果。
- 第 1211–1216 行的探边输入覆盖删除；探边成为 supervisor 下的一个控制者。

**步骤 1 完成条件：**
- 模拟中的“控制者 × 中断 × 时刻”矩阵上，I1 和 I2 违反为 0。控制者覆盖路线执行器和探边；中断覆盖改目标、取消、依赖变化、超时、缺锚点；时刻取接近、边缘、离地前一 tick、空中。
- 第 2.2 节两个 L 形走道场景不再坠落：可以失败，但必须安全失败。
- `tests/motion_nav` 全部通过，Fabric 代表场景重跑通过。

## 四、步骤 2：动作边界前置条件（消除 D 类中的层级错误）

### 4.1 接口

```python
class PreconditionStatus(StrEnum):
    READY = "ready"
    NEEDS_ACQUISITION = "needs_acquisition"     # 可以由一个获取动作补齐，例如探边
    NEEDS_INFORMATION = "needs_information"     # 只能等观察
    REJECTED = "rejected"                       # 动作不成立，需要重规划


@dataclass(frozen=True, slots=True)
class PreconditionResult:
    status: PreconditionStatus
    reason: PreconditionReason
    cells: tuple[BlockPos, ...] = ()
    acquisition: BodyController | None = None  # 例如绑定本次下落的 LandingEdgeProbe


class ActionPrecondition(Protocol):
    def check(self, frame: NavigationFrame, action: RouteAction,
              risk: TaskRiskLedger) -> PreconditionResult: ...
```

### 4.2 层级划分

| 条件 | 现在的位置 | 改到 |
|---|---|---|
| 地形已知、证明段存在、额度总量足够 | 规划与整条路线接纳 | 不变 |
| 直接下落的落点近距离下部证据 | 整条路线接纳（[`route_admission.py:153-180`][gate-all]） | **该次 `ControlledDrop` 开始前**，由执行器调用 |
| 探边 | 会话在接纳失败后创建（[`navigation_session.py:1689`][probe-create]） | 前置条件返回 `NEEDS_ACQUISITION` 时，由执行器创建，并作为子控制者交给 supervisor。此时身体已在起始支撑面上 |
| 额度承诺 | 执行器离地记账（已正确） | 不变，并归入 `TaskRiskLedger`（第五节） |
| 入口窗口 | 各动作各自检查 | 统一作为前置条件实现 |

- 这样，远处落点和多道坎路线都可以先被接纳，再在每道坎前各自取证。
- 探边只会在“下一步就是这次下落”的时候开始，第十八轮 P0 的起因在结构上不存在了。

**步骤 2 完成条件：**
- 模拟中，L 形走道远处落点、两道和三道坎的阶梯下坡都成功，I1 到 I3 无违反。
- 第十八轮 Fabric 批次的 12 个场景重跑通过。
- 直接下落的取证与执行时间分开报告（D038 已要求）。

## 五、步骤 3：统一的重试与额度账本（消除 B 类）

### 5.1 接口

```python
@dataclass(frozen=True, slots=True)
class RetryPolicy:
    per_cause_limit: int
    total_limit: int


class RetryLedger:
    def record_failure(self, progress_key: Hashable, cause: StrEnum) -> RetryVerdict: ...
    def record_progress(self, progress_key: Hashable) -> None: ...
    # RetryVerdict: ALLOWED 或 EXHAUSTED(cause | TOTAL)；耗尽时附带全部原因的计数，供报告使用
```

**规则：**
- 以“进度状态”为键，按原因分别计数，同时设总上限。
- 只有测得真实进度才清零，例如身体到达另一个支撑面、几何修订变化，或攻击命中。
- 目标修订不清零，但目标修订引起的新请求也不计数。只有失败引起的重试才计数。

### 5.2 迁移清单（8898cf9）

| 计数 | 位置 | 现在的问题 |
|---|---|---|
| 重规划 | `navigation_session.py:1247-1268`（`_replan_key`） | 只比较上一个键，交替原因可以绕过（第十八轮第五节） |
| 信息等待 | `_INFORMATION_WAIT_LIMIT_FRAMES`，会话内 | 独立实现 |
| 探边期限 | `DIRECT_DROP_EDGE_PROBE_MAX_FRAMES` | 独立实现；同一目标可以反复新建探边 |
| 搭桥额度 | `_bridge_remaining` | 独立实现 |
| 攻击重试 | `attack_evidence.py`（已有跨类别总数） | 作为参考实现，最后迁移 |
| 重新获取视野、瞄准次数 | `moving_melee_driver.py`、`melee_strike_driver.py` | 独立实现 |

`TaskRiskLedger` 与它并列，负责伤害额度：承诺、已承受伤害、接纳前复核。现在这些逻辑分散在会话的 `_task_damage_budget`、`_movement_damage_spent_points`、`_executor_reported_damage_points` 和执行器里。

**步骤 3 完成条件：**
- 账本单元测试覆盖交替原因和进度清零。
- 模拟中注入交替的 `NEEDS_REPLAN` 原因，I5 无违反。
- 会话中的三处计数（重规划、信息等待、探边）迁移完成。技能层计数在第二期迁移。

## 六、步骤 4：按状态归属拆分会话

### 6.1 所有者与字段归属

47 个字段按所有者归属如下（8898cf9 字段名）：

| 所有者 | 字段 |
|---|---|
| `GoalRequestLedger` | `_request`、`_pending_goal`、`_intent_sequence`、`_source` |
| `PlanningPipeline` | `_planner`、`_owns_planner_worker`、`_planning_snapshot`、`_planning_snapshot_request_id`、`_planning_changes`、`_planning_margin`、`_snapshot_builder`、`_snapshot_cells_per_step` |
| `ExecutionSupervisor`（步骤 1） | `_active_route`、`_executor`、`_coordinator`、`_motion_worker`、`_owns_motion_worker`、`_last_decision`、`_restart_after_active_terminal`、`_cancel_reason`、`_edge_probe` |
| `InformationAcquisition` | `_snapshot_missing`、`_residual_missing`、`_information_statuses`、`_information_lower_required`、`_information_wait_*` 三项 |
| `TaskRiskLedger`（步骤 3） | `_task_damage_budget`、`_movement_damage_spent_points`、`_executor_reported_damage_points` |
| `RetryLedger`（步骤 3） | `_replan_key`、`_replan_attempts` |
| `WorldInteraction` | `_required_interaction`、`_interaction_approach_pending`、`_bridge_policy`、`_bridge_remaining` |
| 会话本身 | `_state`、`_reason`、`_closed`、`_frame`、`_adapter`、`_admitter`、`_motion_residual`、`profiles`、`session_id`、`_clock` |

### 6.2 显式状态机

- 会话只保留一张转移表：状态 × 事件 → 下一状态和动作。
- **状态：**`READY`、`PLANNING`、`NEEDS_INFORMATION`、`EXECUTING`、`HANDOFF`（等待旧控制者安全交出）、`CANCELLING`、`REQUIRES_INTERACTION`、`COMPLETE`、`CANCELLED`、`FAILED`、`CLOSED`。
- **事件：**
  - `StartGoal`、`ReviseGoal`、`Cancel`、`Close`；
  - `PlannerResult`、`AdmissionResult`；
  - `ControllerReport`（来自 supervisor）；
  - `InformationArrived`、`InformationTimedOut`；
  - `DependencyChanged`、`InteractionConfirmed`；
  - `RetryExhausted`。

**关键转移：**

| 当前状态 | 事件 | 下一状态 | 动作 |
|---|---|---|---|
| EXECUTING | ReviseGoal | EXECUTING | 新请求进入规划；当前控制者继续，不清空 |
| EXECUTING | AdmissionResult(接纳) | HANDOFF | 对当前控制者 `request_stop(ROUTE_REPLACED)`，新路线作为待接替者 |
| HANDOFF | ControllerReport(可以安全交出) | EXECUTING | supervisor 切换控制者 |
| 任意非终态 | Cancel | CANCELLING | `request_stop(CANCELLED)` |
| CANCELLING | ControllerReport(可以安全交出) | CANCELLED | — |
| EXECUTING | ControllerReport(NEEDS_REPLAN) | PLANNING 或 FAILED | 先 `RetryLedger.record_failure`；耗尽则进入 FAILED |
| EXECUTING | ControllerReport(INPUT_LOST，已安全) | FAILED | — |
| PLANNING | PlannerResult(目标区域已包含身体) | COMPLETE | **修复原型发现的新问题** |

**其他要求：**
- 转移表做穷举测试：每个状态下每个事件，要么有定义，要么明确拒绝。
- **`reason` 只用于报告：**
  - `_reason` 改为有类型的 `SessionReason`；
  - 仓库中 19 处以原因字符串作判断的地方，改为读取有类型的状态或结果（见 [`code_metrics-8898cf9.txt`](2026-09-28-recurring-defects-data/code_metrics-8898cf9.txt)）。
- 合并两个几乎相同的函数 `_replan_from_current`（第 1406 行）和 `_restart_request_from_current`（第 1449 行），成为 `GoalRequestLedger.reissue_from(body)`。

### 6.3 规划器中的同类整理

- 把证明回路从 `plan_known_surface_snapshot` 中抽出，成为独立的“路线证明”步骤：
  - 证明段划分；
  - 连续下降合成；
  - 有界替代搜索。
- `verify_ground_traversal` 的失败要区分类型：
  - **与路线无关**（入口状态不适用）：一次退回到后备边；
  - **与路线有关**：只禁用该段。
  这样可以关闭第十八轮第四节的残留问题。
- 规划前先判断“目标区域已包含起点”，直接返回零长度的完成结果，不构造代价为 0 的候选。

**步骤 4 完成条件：**
- 转移表穷举测试通过；
- 模拟矩阵和 `tests/motion_nav` 结果不变或更好；
- 以原因字符串作判断的地方为 0。
- 会话文件长度不设指标，以职责归属为准。

## 七、验收分级与工作流程

1. **五级验收**（每项能力在 stages 文档中逐级标注，不能跳级）：已实现 → 组件通过 → 正式路径模拟矩阵通过（含扰动）→ Fabric 代表场景 → 完整矩阵关闭。
2. **修复规则：**每个缺陷，无论来自审查、模拟还是 Fabric，都要提交三样东西：
   - 一个覆盖这类缺陷的模拟场景或性质测试，而不只是这个实例；
   - 一次同类搜索记录：其他控制者、计数、门槛是否有同样写法；
   - 一条不变量；如果现有不变量没有覆盖这类缺陷，就补一条。
3. **对抗测试与实现分开：**实现完成后，由另一个会话或 agent 只用模拟器寻找反例（扩展场景族、换扰动、扫种子），然后才交外部审查。
4. **小步提交：**每个提交只改变一个可验收行为。重构步骤不与新能力混在同一次交付中。

## 八、风险与对策

| 风险 | 对策 |
|---|---|
| 模拟保真度不足，给出假通过 | 每个 Fabric 代表场景都有模拟孪生；不一致时先修模型；模拟结论不能代替 Fabric |
| 重构引入回归 | 先完成步骤 0，用矩阵作为特征测试；每一步都要求矩阵结果不变或更好 |
| 接口过度设计 | 每个接口只包含本方案列出的方法；第二期迁移（技能层）按需再做 |
| 暂停新功能影响进度 | 步骤 1 到 3 可以并行；它们消除的正是最近每轮都会出现的返工 |
| 私有字段被测试依赖 | 步骤 0 就提供只读属性和测试注入点；原型中的私有访问全部替换 |

## 九、原型文件与运行方式

目录：[`2026-09-28-refactor-plan/harness/`](2026-09-28-refactor-plan/harness/)

| 文件 | 内容 |
|---|---|
| `sim_backend.py` | `CalculatorBackend`：计算器驱动的游戏替身、观察模型、输入回执、扰动 |
| `sim_runner.py` | 同步规划与求解替身、场景与事件、`InvariantMonitor`（I1 到 I4）、运行器 |
| `scenarios.py` | 第 2.2 节的 14 个场景 |

在 `8898cf9` 检出目录的仓库根目录运行：

```
PYTHONPATH=.:<harness 目录> python -B <harness 目录>/scenarios.py            # 全部场景
PYTHONPATH=.:<harness 目录> python -B <harness 目录>/scenarios.py far_landing_L_walkway
```

依赖仓库自带的测试夹具（`tests/follow_v3_fixtures.py`、`tests/test_action_receipt.py`、`tests/test_player_runtime.py`）和正式配置 `config/motion-navigation/`。

## 十、后续

按本方案实施后的复审见 [第十九轮评审：导航协调重构 S0–S5](2026-09-28-coordination-refactor-review.md)。其中 P1-1（先走几步再下一格台阶）是本方案第 2.2 节的 14 个场景漏掉的情形。

[base]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/tree/8898cf958cf31ad231fcb3953df240ce83bb6292
[gate-all]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/8898cf958cf31ad231fcb3953df240ce83bb6292/mc2p/motion_nav/route_admission.py#L153-L180
[probe-create]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/8898cf958cf31ad231fcb3953df240ce83bb6292/mc2p/motion_nav/navigation_session.py#L1685-L1702
