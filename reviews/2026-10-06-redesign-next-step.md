# 下一步方案：M0 收尾与 M1 动作接口验证

- 日期：2026-10-06
- 上位文档：[中层重构总方案](2026-10-06-redesign-master-plan.md)、[整体设计评估](2026-10-06-design-assessment.md)
- 材料：[`2026-10-06-redesign/`](2026-10-06-redesign/)
  - `design_metrics.py` 及其基线输出；
  - `action_spec_sketch.py`；
  - `D072-draft.md`。

> **更新（2026-10-07）：** M0、M1 已实施，见 [M0/M1 审查](2026-10-07-m0-m1-review.md)。M1 的数据否定了本文中与 Session 体积挂钩的目标：M1 的"Session 至少减少 100 行"、M2/M4/M5 的 Session 行数目标，以及"协调层占比 ≤ 30%"。这些都已撤回。动作接口在可扩展性上的目标成立，保留。M2 不再作为独立阶段，改为随产品工作逐步迁移。M3—M5 改为由具体缺陷或产品需要触发。以审查第四、五节为准。

## 结论

| 阶段 | 内容 | 时间盒 | 完成条件 |
|---|---|---|---|
| M0 | 七项收尾 | 1 个批次 | 第一节七项全部满足 |
| M1 | 只迁移受控下降，验证 `ActionSpec` 是否成立 | ≤ 2 个批次，8 个可回退的提交 | M1-a 至 M1-g 与全局 V1—V7 全部满足，才继续 M2；否则按第五节停止 |

选受控下降作为第一个迁移对象，有三个原因：
1. **它是最分散的动作。** 基线中有 21 处类型分支提到它，分布在 5 个文件，其中 Session 有 8 处；`route_admission.py` 另有求解类型映射和构造。
2. **它覆盖了接口的大部分类别。** 包括执行、身体安全、后台求解、前置条件和伤害，即 A—F 中除 F 以外的五类，外加 H 类型登记。
3. **它不牵涉步行的几何证明（G）。** 步行证明留给 M3。

如果下降都能干净地迁移，说明接口设计成立；如果迁移后 Session 没有缩小，或者接口里要开好几个只给下降用的口子，就说明方向不对，应在投入更多之前停下。

## 一、M0 收尾与安全网（1 个批次）

| 编号 | 工作 | 验收 |
|---|---|---|
| M0-1 | 采纳 D072（草案见材料目录）。阶段文档把结构整理标为"未通过、已终止"；AGENTS.md 的当前阶段改为"中层重构 M0" | 决定、阶段和验收文档一致；v2 清单文件不改写，只在验收文档中注明"被中层重构取代，模板结论不作为评审结论" |
| M0-2 | 补回"后台规划不阻塞控制"的确定性检查：把 S0-R 审查中的原型（`proto_nonblocking_checks.py`）的两项检查收进 `test_planner_worker.py`。四个被删了计时断言的测试，改用这两项检查，或在说明中引用它们 | 用两种变异（控制侧规划、阻塞轮询）复跑：每种都至少被一项检查发现。正序、逆序完整检查都是 0 失败。在另一组完整检查并行运行的负载下，连续 3 次结果相同 |
| M0-3 | 按 S1 原定流程删除 3 个死路径：先把函数体替换成 `raise AssertionError`，运行完整检查和五组集合；全部通过且静态上没有调用者，再删除 | 五组集合逐项一致；`design_metrics.py` 中三项目标消失；删除约 34 行 |
| M0-4 | `design_metrics.py` 改名为 `scripts/navigation_design_metrics.py`，并加一个组件测试：确认分类表能覆盖基线的 70 处，未归类为 0 | 基线 JSON 冻结到 `evidence/motion_navigation/redesign-m0/`（小于 1 MB）；此后每一步都用同一脚本报告 |
| M0-5 | 在 R28-C-05 测试中补回 seed 15、69、119（S0-R 改写时删掉了） | 三个种子在当前代码上成功，测试通过 |
| M0-6 | 缺陷台账登记两项待查：① 运行时仍为 READY 的失败（任务级重试、取消、异步追踪）只改驱动器状态、不通知会话；② `_finish_control_unavailable` 中"会话已有业务结论"的分支没有测试 | 两项都有编号，并写明在 M5（驱动器枚举）之前查清 |
| M0-7 | 用 `design_metrics.py` 在 M0 最终提交上出一份报告，作为 M1 的起点 | 报告与 M0-4 冻结的数据一致；M0-3 删除的三项已不在报告中 |

## 二、M1 设计

### 1. 新增内容

只新增一个模块，例如 `mc2p/motion_nav/action_specs.py`，内容包括：

- `ActionSpec` 协议、`ActionSpecBase`（带类型的中性默认值）和 `ActionRegistry`；
- `BodyCommitment`、`SolutionDisposition`、`StopHold` 三个小类型，用来替换现有的布尔判断；
- 5 个实现：
  - `ControlledDropSpec` 完整实现 A—F；
  - 其余 4 个只实现组合判断需要的分类成员：`body_commitment`、`requires_safe_handoff`、`solve_kind`。

接口成员只引入 M1 用得到的那些。G 类（`revalidate`）留到 M3。成员列表以 [`action_spec_sketch.py`](2026-10-06-redesign/action_spec_sketch.py) 为起点，实际实现可以更少，但不能更多（规则见 M1-g）。

### 2. 迁移清单：基线中提到受控下降的 21 处

| 类别 | 位置（`e108a32`） | 改为 |
|---|---|---|
| H | `ActionRoute.__post_init__`（1 处，与跨隙组合） | `ActionRegistry.supports()` |
| E 伤害 | Session `_route_expected_damage_points`、`_reserve_route_risk`；执行器 `_commit_drop_damage_if_started` | `expected_damage_points()`、`damage_committed()` |
| D 前置条件与信息 | `action_preconditions.check_action_precondition`；Session `observation_request`、`_current_action_precondition`、`_upcoming_action_precondition_index`、`_begin_action_acquisition` | `precondition()`、`observation_needs()` |
| B 停止安全 | 执行器 `stop_protection`；Session `_needs_same_frame_stop_protection`（与跨隙组合）、`_prepare_route_action`（采集时潜行）；运动协调 `decide`（超过 1 格的下降跨过动作边界） | `body_commitment()`、`stop_hold()`、`requires_safe_handoff()` |
| C 后台求解 | 运动协调 `_air_action_direction`、`_air_action_landing`（与跨隙组合）、`_prepare_upcoming_from_applied_state` | `solve_request()`、`accept_solution()`、`solve_kind()` |
| A 执行 | 执行器 `_activate` 2 处、`_landed_on_current_action_destination`、`decide` 2 处（其中 4 处与跨隙组合，一并改为问接口） | `controller()`、`arrived()` |

另外有两个按受控下降分支、但不是类型判断的位置，也要一起迁移：
- `route_admission.py` 中 `{ControlledDropSegment: MotionSolveKind.CONTROLLED_DROP}` 这类映射；
- 由规划边构造 `ControlledDropSegment` 的位置。

迁移后，前者改为 `solve_kind()`；后者改为由动作实现提供的构造函数，或者留在接纳中，但必须计入 M1-d。

S0R-C-01 的"空中得到 `needs_state` 先等待落地、落地后重新锚定"，现在写在运动协调里。M1 中它改由 `accept_solution()` 返回 `WAIT_FOR_LANDING_THEN_REANCHOR` 来表达：规则归属到动作，协调代码只执行这个结果。

### 3. 原则

- **只搬移，不改写。** 每处分支原来的判断逻辑原样搬进动作实现，不顺手修改条件或数值。
- **无状态。** 动作实现不保存运行状态。用一项组件测试保证：动作实现的实例没有可变字段。
- **生产代码不加开关。** 新旧逻辑的等价比对写在测试侧。

## 三、M1 提交顺序

每个提交都是一个可以单独回退的单元。每个提交都按顺序跑：直接测试 → 正序与逆序完整检查 → 五组集合（单轮）→ `design_metrics.py`。任一项失败就回退这个提交。

| 提交 | 内容 | 提交时的检查重点 |
|---|---|---|
| M1-1 | 新增 `action_specs.py`：接口、默认值、登记，以及 5 个实现的分类成员；`ActionRoute` 改用登记表判断类型 | H 类分支 2 → 0；五组一致 |
| M1-2 | 测试侧的等价比对：在五组集合的所有动作实例上，逐一比较旧分支的判断与新接口的结果（旧逻辑复制在测试中作为对照） | 比对 0 处不一致；这时生产代码还没有改用接口 |
| M1-3 | 迁移 E（伤害，3 处） | 风险和伤害相关测试；产品集合中的严格下降组 |
| M1-4 | 迁移 D（前置条件与信息，含 Session 4 处） | 下降采集、落点信息和探边相关测试；协调集合 |
| M1-5 | 迁移 B（停止安全，4 处） | 中断、交接、落地责任相关测试；故障 16 项 |
| M1-6 | 迁移 C（后台求解，3 处，以及 `MotionSolveKind` 的映射）；把 S0R-C-01 规则改为 `accept_solution()` 的返回值 | seed 163 与 v4"下降迟到"组；五组一致 |
| M1-7 | 迁移 A（执行器 5 处，组合判断一并改为问接口）；删除旧分支 | 动作实现之外不再出现提到受控下降的类型分支 |
| M1-8 | 把等价比对结果冻结为回归语料，删除测试中的旧逻辑副本；补齐 `ControlledDropSpec` 的约定测试；写架构文档 `action-spec-v1.md`；出量尺报告和判定记录 | 第四节全部 |

## 四、M1 验收标准

### 量尺（`design_metrics.py`，对照 M0 报告）

| 编号 | 标准 |
|---|---|
| M1-a | 提到受控下降的类型分支：21 → **0**（不计动作实现） |
| M1-b | 类型分支总数：70 → **≤ 49** |
| M1-c | `navigation_session.py` 中 `ControlledDropSegment` 的引用：**0** |
| M1-d | 提到受控下降的生产文件：5 → **≤ 1**（不计 `action_route.py` 和动作实现） |
| M1-e | 协调层（`design_metrics.py` 的 coordination 组）行数净减少；Session 至少减少 **100** 行；运动导航总行数增加不超过 **300** 行（接口的一次性投入） |
| M1-g | 只服务受控下降的接口成员 **≤ 1**，并写明理由（判断标准：该成员在其余 4 个实现中都只用默认值） |

### 行为与测试

| 编号 | 标准 |
|---|---|
| M1-f | `ControlledDropSpec` 通过约定测试：前置条件覆盖每个原因码；身体承诺的分类与执行器的实际阶段一致；预计伤害不低于实际提交的伤害；任意一拍中断后，身体都处于已声明的承诺状态 |
| V1 | 五组集合（产品 2,000、协调 1,448、故障 16、跟随 10、世界变化 19）在 Windows 连续两次运行，与 M0 的索引逐项一致 |
| V2 | 正序、逆序完整检查都是 0 失败、0 错误、0 预期失败，共享场景哈希不变 |
| V3 | Windows 正式性能门槛通过：D058 完整控制准备 P95 ≤ 8 ms、P99 ≤ 15 ms、最大值 < 30 ms；复核 P95 ≤ 1 ms；D061 长会话门槛通过。报告与 M0 的差值，P95 变化超过 +10% 时必须说明原因 |
| V4 | M1 触及的、五组集合都不进入的目标（按路径矩阵），要做直接测试变异检查 |
| V5 | 动作实现无状态（组件测试）；没有新增线程、开关或兼容分支；旧分支已全部删除 |
| V6 | 新增 `action-spec-v1.md`，写清接口、规则和扩展方法；AGENTS.md 中"新增跳跃主要扩展……"一节，改为指向 `ActionSpec` |
| V7 | 量尺报告附在验收文档中；回归语料小于 1 MB |

**不需要 Fabric 复核。** M1 是行为不变的搬移，V1 已经证明这一点。如果 V1 出现无法解释的差异，回退对应提交，而不是去 Fabric 上找证据。

## 五、判定与止损

M1 结束时写一条判定记录。

**继续 M2，需要同时满足：**
- M1-a 至 M1-g 与 V1—V7 全部通过；
- 迁移过程中没有在协调代码里新增任何特殊处理。

**停止重构，满足任一条即可：**
- Session 没有缩小（M1-e 不满足）；
- 需要 2 个或更多只服务受控下降的接口成员（M1-g 不满足）；
- 性能门槛失败，且两次调整内修不好；
- 2 个批次用完，M1-7 仍未完成。

**停止后如何处理 M1 的改动：**
- 行为一致、且量尺有改善的提交保留，写明"接口验证未通过，保留已完成部分"；
- 否则回退到 M0 的最终提交。

两种情况都要写决定，并把总方案标为"在 M1 停止"。

## 六、命令

在仓库根目录运行（Windows 用项目的 conda 环境）：

```text
python scripts/navigation_design_metrics.py . --json <m1-metrics.json>
python scripts/run_motion_navigation_checks.py --output <forward.json>
python scripts/run_motion_navigation_checks.py --reverse --output <reverse.json>
python scripts/navigation_s0r_evidence.py --set <product|coordination|faults|follow|world_changes> --workers 4 --output <run>
python scripts/navigation_structure_baseline.py compare --baseline <m0-index.json> --candidate <run>/index.json
python scripts/navigation_structure_paths.py --workers 4 --output <path-matrix.json>
python scripts/benchmark_d058_route_revalidation.py ...      # 按 D058 验收记录的参数
python scripts/benchmark_d061_long_session.py ...            # 按 D061 验收记录的参数
```

## 七、M1 不做的事

- 不迁移其余 4 种动作的完整逻辑（只做组合判断需要的分类成员）。
- 不改步行的几何证明与复核（M3）。
- 不合并两个协调器，不改 Session 的生命周期结构（M4、M5）。
- 不修复任何行为缺陷，不调整任何数值或阈值。发现的问题登记到缺陷台账，在产品轨道或对应阶段处理。
- 不删除任何历史回归测试（总方案第六节第 3 条）。
