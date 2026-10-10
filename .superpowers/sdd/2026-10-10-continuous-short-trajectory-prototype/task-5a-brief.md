# Task 5A：P0 有界参考搜索核心

## 目标

先实现可执行的最小参考搜索，只覆盖三个代表正例：

1. 平地从静止走到目标并安全停止；
2. 直线上一整格；
3. 同高一格跨隙。

本任务用于尽早判断“逐 tick 搜索 + 现有计算器 + 承诺扫描”是否可行。三族未全部找到前，不扩到完整 P0 清单，不写 worker，不接入 Runtime。

## 必读

- D095、TP architecture/stage/acceptance
- 总实施计划 Task 5
- Task 3／4 实现、报告和所有独立审查，尤其 `timing-branch-audit.md`
- 正式 `physics_1_21.step()`、`query_support()`、`GoalState.accepts()`

## 文件范围

新增：

- `experiments/motion_navigation/trajectory_proto/reference_search.py`
- `tests/motion_nav/test_trajectory_proto_reference_search.py`

允许修改：

- `experiments/motion_navigation/trajectory_proto/scenarios.py`，增加三个可执行 fixture；
- `contracts.py`／`physics.py`／`commitment.py`，只为共享同一请求预算、保存验证结果或消除经 RED 证明的接口缺口；
- TP stage/acceptance，记录 5A 结果与边界。

禁止修改 `mc2p` 生产代码；本任务不新增 `runner.py`、worker 或完整矩阵输出。

## 先写 RED

1. 平地正例必须由搜索找到，末态满足目标且停车尾迹安全。
2. JumpUp 正例必须由搜索找到，不能调用现有动作类型求解器或写死 Walk→JumpUp 组合。
3. 一格 JumpGap 必须由搜索找到；准时和真实晚一 tick 两支执行同一候选，两支均满足目标和承诺证明。
4. 将晚支 prelude 从 NEUTRAL 改成已知 WALK 后，如果该候选不再安全，搜索不能只保留准时支。
5. 节点、物理 step 或轨迹 tick 任一预算用尽，返回 `NO_TRAJECTORY_IN_BUDGET` 与对应 typed reason；不能返回 BLOCKED／STALE。
6. 搜索和 `scan_commitment()` 必须共享一个总计数预算。构造“搜索本身几乎用完预算、验证还需 step”的场景，证明验证不能重新获得一整份预算。
7. 相同请求重复运行，状态、输入、轨迹哈希、计数和结果逐项相同。
8. 搜索决策不读取墙钟。墙钟只可在 runner/测试外层测量，本任务源码禁止 `time.perf_counter` 等调用。
9. 任何 FOUND 都必须携带实际 `CommitmentProof`，并且两个分支的末态都满足同一 `GoalState`；不能仅因 nominal 分支到达就返回成功。

## 搜索规则

- 直接逐 tick 调用现有 `step()`；不复制物理公式，不调用 JumpUp／JumpGap 现成求解器。
- 每个 fixture 提供有限、固定顺序的真实 `TickInput` 集合。搜索器不能按动作名写 Walk→JumpUp 或 JumpGap 特判。
- 可使用确定性的 best-first／A*。启发只用于排序，不能把可能可达的候选判死；不能声称全局最优。
- 搜索状态至少保留完整 `PhysicsState`、已用 tick 和候选输入。若去重，首版只允许完整状态严格相等；不得用未经证明的速度／位置量化把状态合并。
- 可做与动作名无关的支配剪枝，例如同一完整状态保留更短／更低代价路径。所有裁剪写进 coverage 说明。
- 输入展开顺序固定并进入结果／测试。不得根据 Python set/dict 无序遍历决定结果。
- nominal 分支可用于扩展候选；达到目标候选后，必须调用 Task 4 承诺扫描，从共同 anchor 重放所有声明的真实时序分支，再逐支检查目标。
- 候选必须包含安全停止尾段。只碰到目标区域但速度、支撑、姿态或末态停车不满足，不能 FOUND。
- 目标支撑用现有公开几何查询。UNKNOWN 返回 `NEEDS_INFORMATION`；单个候选碰撞只是淘汰，不是请求级 BLOCKED。

## 统一预算

一个请求的 `max_nodes`、`max_physics_steps`、`max_trajectory_ticks` 覆盖：

- frontier 展开；
- prelude 重放；
- 候选双分支重放；
- 每个边界停车尾迹；
- 目标确认所需的物理／几何读取。

不得让每个候选或 scanner 重新取得完整预算。可以让 scanner 接收共享 counter，或从剩余额度构造子预算并把计数严格汇总；无论实现方式，测试必须证明总数从不超过原请求上限。

`max_trajectory_ticks` 限制共享候选长度。晚支额外 prelude 单独报告总时长，不隐藏为 40 tick 以内。

## 三个 fixture

- 都使用 detached、完全已知的小世界和 Java 1.21 ruleset。
- 入口、目标、输入字母表、目标方向、伤害余额、route guidance 和两支 prelude 明确冻结。
- 晚支入口必须由真实 `step(anchor, known_prelude)` 得到，不能手填位置或只加 tick。
- 一格跨隙包含真实空格与已知落点；JumpUp 包含真实一格高支撑和净空。
- fixture 代码可以在 experiments 内共享；原型源码不能 import `tests`。

## 结果

新增不可变 `ReferenceSearchOutcome`（或等价名字），至少保存：

- Task 3 的五类请求状态和 typed reason；
- 找到时的共享 inputs、`CommitmentProof`、逐支目标检查；
- nodes、physics steps、候选长度、逐支总时长；
- 固定输入顺序和 coverage 说明；
- 稳定结果哈希。

FOUND 之外不得携带 proof。候选全部枚举完但无法证明请求级必要条件时，返回 `NO_TRAJECTORY_IN_BUDGET/SEARCH_EXHAUSTED`，不能叫 BLOCKED。

## 验证

至少运行：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_reference_search -v
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment tests.motion_nav.test_trajectory_proto_reference_search -q
.\.venv\python.exe -m unittest tests.motion_nav.test_b09r_physics_step tests.motion_nav.test_b09r_physics_rollout tests.motion_nav.test_b10_gap_solver tests.motion_nav.test_motion_continuation_quality -q
.\.venv\python.exe -m unittest tests.motion_nav.test_standalone_export -q
```

另做最小变异：跳过晚支目标检查、给 scanner 重置预算、删除真实 prelude 重放，三者都必须使测试失败。

## 交付

- 报告：`.superpowers/sdd/2026-10-10-continuous-short-trajectory-prototype/task-5a-report.md`
- 聚焦提交。
- 若三族任一未找到、共享预算无法正确计数、或搜索需要写动作组合特判，停止并报告，不进入 5B。
