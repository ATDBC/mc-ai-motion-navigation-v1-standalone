# Task 5A-R：修正证明，改用动作元搜索，把成本降到门槛内

日期：2026-10-10。状态：**草稿**。由三方审查者起草，前提是项目方先确认 [D096 草稿](0096-tp-primitive-search-and-performance-gate-draft.md)。确认后，复制到 `.superpowers/sdd/2026-10-10-continuous-short-trajectory-prototype/` 下执行。

## 1. 目标

只做一轮，分三步。每步通过独立审查后，才开始下一步：

| 步骤 | 回答的问题 | 预计 |
|---|---|---|
| R1 修正证明 | 证明是否完整：不遗漏承诺，不把条件资源当已证明 | 0.5 天 |
| R2 动作元前端 | 能否稳定找到：正例全找到，输入集变大不丢解，不可行的有界结束 | 1—1.5 天 |
| R3 降低成本 | 能否算得完：Windows 耗时达到 D096 门槛 | 1 天 |

本任务不写 runner、worker、闭环驱动或 Runtime 接入，也不扩展到完整 18 族。这些属于 5B 以后。

## 2. 必读

- D095、D096（草稿确认后的正式版），TP 的架构、阶段和验收文档。
- Task 5A 的 brief、报告和独立审查。
- 三方审查 [`reviews/2026-10-10-tp-5a-review.md`](../2026-10-10-tp-5a-review.md)，以及同名目录里的下列文件：
  - `repro_findings.py`；
  - `diagnose_gap2_sprint.py`；
  - `matrix_probe.py`；
  - `primitive_probe.py`；
  - `proto-5a-fixes.diff`。
- 本目录的 [`template_probe.py`](template_probe.py) 和三份输出。它们是夹具可行性、模板和排序实验的依据。
- 正式代码中的 `physics_1_21.step()`、`query_support()`、`GoalState.accepts()`。

三方审查的补丁和探针只用作参考，不能直接复制，原因有两个：

1. 它们没有先写 RED；
2. 它们复用了 `_tail`、`_Incomplete` 等私有名字。

正式实现要按本说明重新写，先 RED 后修。

## 3. 全局规则

- 只改 `experiments/motion_navigation/trajectory_proto/`、对应的 `tests/motion_nav/test_trajectory_proto_*.py`，以及 TP 的阶段和验收文档。`git diff -- mc2p` 必须为空。
- 每一项行为修改先写 RED，保存失败输出，再修。每步单独提交一次，提交后交独立审查。
- 决策代码不读墙钟。墙钟只出现在 R3 的测量脚本里。
- 所有物理 step、尾迹、支撑查询和目标检查，共用请求内同一个 `CountedPhysics`。
- 不复制运动公式。需要预测运动时，一律调用 `step()`。
- 原型代码总量约 2500 行以内，测试、夹具和报告不计入。
- Task 3／4／5A 的报告、审查、原数字和失败证据都不改写。新结果另行记录。
- 任一步的独立审查发现证明完整性问题，或该步门槛不通过，就按 D096 结束 TP，不再加修正轮。

## 4. R1：修正证明

### 4.1 文件范围

`commitment.py`、`contracts.py`、`reference_search.py`，以及对应测试。

### 4.2 先写 RED

1. **未关闭的风险区间。** 用独立审查的“落点只剩 1／12 支撑”反例，搜索不得返回 FOUND。另写一个组件测试，直接构造“风险已经打开、后面再没有能关闭它的边界”的停车尾迹序列，验证扫描器返回 `CANDIDATE_REJECTED/unrecovered_risk`，并且不发证明。这条拒绝在统一判据后通常走不到，所以必须直接测，不能只靠第 2 项间接覆盖。
2. **统一的安全停住判据。** 停住时支撑比例为 0.1426 的停车尾迹，必须判为 UNSAFE。现有测试 `test_wait_cannot_cross_last_safe_stop_even_with_known_support` 的对应断言改为 UNSAFE，加注释说明原因。这个测试真正要测的“等待支拒绝”，断言保持不变。
3. **目标检查用同一判据。** 末态支撑为 1／12 时，`goal_checks` 中的 `accepted` 必须为 False。
4. **资源目标。**
   - 非空的 `minimum_resources` 返回 `NEEDS_INFORMATION/unproven_resources`，不做任何物理计算。
   - 空资源目标的三个代表，结果不变。
5. **尾迹停不下。** `ScanOptions(max_tail_ticks=1)` 加一个正在走动的身体，结果必须是 `CANDIDATE_REJECTED/tail_not_settled`，不能是请求级的 `NO_TRAJECTORY_IN_BUDGET/trajectory_tick_budget`。物理 step 总预算耗尽，仍然按请求级处理。
6. **保留 Gap1 的恢复点。** Task 5A 找到的跨隙候选输入，冻结成常量写进测试。用固定候选做组件测试，确认最后可放弃、承诺、恢复三个边界仍是 2／3／17。这样以后换搜索前端，这项检查也不受影响。

### 4.3 修法要求

- 安全停住只保留一个判据：着地、水平速度不超过 `ZERO_SPEED_EPSILON`、公开支撑查询结果为 FEASIBLE，且支撑比例 ≥ 0.15。以下四处都调用这一个函数：
  - 停车尾迹的 SAFE_STOP；
  - 风险区间关闭；
  - 等待分支；
  - 目标检查。
- 常量改名为 `MINIMUM_SAFE_SUPPORT_FRACTION`，注释写明来源：`FixedRouteConfig().minimum_support_fraction`。
- 新增 `CandidateRejection.UNRECOVERED_RISK`、`CandidateRejection.TAIL_NOT_SETTLED` 和 `SearchReason.UNPROVEN_RESOURCES`。最后一项归入 NEEDS_INFORMATION。
- 去重问题在 R1 不修。R2 换成树形枚举后，这个问题会自然消失。

### 4.4 验证与通过条件

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment tests.motion_nav.test_trajectory_proto_reference_search -v
.\.venv\python.exe -m unittest tests.motion_nav.test_b09r_physics_step tests.motion_nav.test_b09r_physics_rollout tests.motion_nav.test_b10_gap_solver tests.motion_nav.test_motion_continuation_quality -q
.\.venv\python.exe -m unittest tests.motion_nav.test_standalone_export -q
```

通过条件：

- 第 4.2 节的 RED 全部转绿。
- 原有检查全部通过。改写过的断言逐项列在报告里，并说明原因。
- 三方审查的 `repro_findings.py` 中，P1 不再给出 FOUND，P2 返回 `NEEDS_INFORMATION/unproven_resources`。
- 变异检查：去掉未关闭区间的拒绝、目标检查退回任意正支撑，两项各自至少让 1 个行为测试失败。

## 5. R2：动作元搜索前端

### 5.1 文件范围

- 改写 `reference_search.py`：保留 `reference_search()` 和 `ReferenceSearchOutcome`，把贪心循环换成下面的枚举。
- `scenarios.py`：加入附录 A 的夹具。
- 新增 `tests/motion_nav/test_trajectory_proto_primitive_matrix.py`。

### 5.2 模板（冻结）：转向动作元

**动作元按“步态”定义，不按单个输入定义。** 步态是去掉朝向后的输入，即 (forward, strafe, jump, sneak, sprint) 这一组。每段只指定一种步态和保持的 tick 数，每个 tick 的朝向按转向规则选。

**转向规则：** 每个 tick，在请求声明的、属于该步态的输入中，选朝向最接近“准时支当前位置指向目标区域中心”方向的那一个；夹角相同时按声明顺序。所有时序分支执行同一条被选中的输入。

候选由四段组成，依次为：

| 段 | 内容 | 取值范围 |
|---|---|---|
| G | 一种非跳步态，按转向规则保持 k1 tick | k1 = 0..20 |
| J | 一种跳步态，按转向规则取一 tick | 可省略 |
| A | 一种非跳步态，按转向规则保持 k2 tick，只在有 J 时出现 | k2 = 0..16 |
| B | 刹车输入保持到所有分支着地且停住 | — |

- 请求的 `input_prefix` 放在 G 之前。
- 候选总长不超过 `max_trajectory_ticks`。
- 非跳步态按以下顺序排列：移动步态在前，零移动步态在后；同类之间按在 `supported_inputs` 中首次出现的顺序。跳步态按首次出现的顺序。
- 刹车输入：按声明顺序取第一个满足以下条件的输入：forward、strafe 都为 0，jump、sneak、sprint 都为 False，朝向等于目标要求的朝向。目标没有朝向要求时，取第一个零移动输入。
- 没有可用的刹车输入时，返回 `NO_TRAJECTORY_IN_BUDGET/SEARCH_EXHAUSTED`，并在 coverage 中写明原因。

这样定义有两个好处：

1. 网格大小只取决于步态数，不随朝向数量增长。三方审查实测，A15 和 A5 的计数相同。
2. 转弯靠逐 tick 选朝向完成，不需要第二个地面段。

**这个模板的限制，要写进 coverage：**

- 转向目标只取目标区域中心，不使用 `route_guidance`，所以不能绕开需要迂回的障碍。
- 每个候选最多起跳一次。
- 输入集变大时不丢解，这一点由矩阵验证，不是由构造保证的。转向规则在朝向更多时可能选到不同的输入。

扩展模板需要新的决定，例如：用引导点作为转向目标、增加第二个地面段、允许多次起跳。本轮都不做。

### 5.3 枚举顺序（确定性，只排顺序，不删网格点）

1. **先枚举全部不跳的候选**，即 G + B：按步态顺序，k1 从小到大。
2. **再枚举带跳的候选。** 每种 G 步态的助跑轨迹只算一次，然后从最后一个可用的起跳点（G 段在碰撞或越过下沿前的最后一个状态）往前试。每个起跳点上，依次按跳步态顺序、A 步态顺序、k2 从小到大枚举。

本目录 `template_probe.py steer` 就是这个模板和顺序。

### 5.4 推进、剪枝与完成

- **两支同步推进。** 每条命令都对全部时序分支调用 `step()`。G 段或 A 段延长时，只要任一分支出现碰撞、计算不完整或低于下沿，这一段就停止延长。下沿为“入口高度”和“目标区域下沿”中较低的一个。
- **遇到 UNKNOWN。** 任一分支返回 `NEEDS_WORLD`，请求结果为 `NEEDS_INFORMATION/unknown_world`，并带上缺失的格子。这和 5A 的语义一致。
- **前缀剪枝（必须可证明不丢解）。**
  - 等待分支的第一条命令：用下面两个保守界限检查停车尾迹，不安全就剪掉。界限一：最终高度不低于目标下沿；界限二：正常伤害不高于剩余额度。
  - 累计伤害超过额度的前缀，剪掉。
- **模板限制。** 低于下沿的前缀不再扩展。这是模板本身的限制，必须写进 coverage。
- **完成判断。** 每个网格点都接上 B 段，然后依次检查：
  1. 所有分支都停在目标区域里；
  2. 支撑满足 R1 的统一判据；
  3. `GoalState.accepts()` 全部通过。

  三项都满足，才调用承诺扫描和逐支目标检查。通过后返回 FOUND。
- **计数。** 每个网格点的完成尝试计 1 个节点；扫描内部照旧计数。
- **不需要去重。** 枚举是一棵共享前缀的树，不会把两条不同路径合并。
- **结果分类。**
  - 网格全部枚举完，返回 `NO_TRAJECTORY_IN_BUDGET/SEARCH_EXHAUSTED`，coverage 写明模板和网格范围。
  - 先触到预算上限的，返回对应的预算类原因。
  - 永远不返回 BLOCKED。

### 5.5 旧测试的处理

凡是依赖贪心搜索具体路径或具体计数的测试，有两种处理方式：

1. 改成固定候选的组件测试；
2. 按新前端重新推导数值。

每一项改动都要列进报告。行为断言不得删除，包括：

- 共享预算；
- 预算类原因；
- UNKNOWN 分类；
- 晚支必须真实重放；
- 结果确定性；
- 源码不读墙钟、不导入旧求解器。

### 5.6 冻结矩阵与预算

R2 的门槛**只看是否稳健，不看耗时**。预算用 P0 默认的 `SearchBudget(4096, 65536, 40, 2)`；三方审查按第 5.2—5.3 节模板实测，全部用例都在这个预算内（附录 B 的 steer 一栏）。如果实现后有正例超出预算，先查明原因，不能直接放大预算。正例与不可行项以附录 A 为准：

| 组 | 项数 | 预期 |
|---|---:|---|
| Task 5A 三个代表 | 3 | FOUND |
| 直线跨隙（两种起点 × 宽 1—3 × A3／A5／A15，去掉无助跑 3 格） | 15 | 14 项 FOUND；“助跑 3 格 A3”返回 `SEARCH_EXHAUSTED`，即网格内无解 |
| `flat_sprint` | 1 | FOUND |
| `turn_90` | 1 | FOUND |
| `jump_up_after_turn` | 1 | FOUND |

“助跑 3 格 A3”如果找到了，并且通过完整验证，也算通过，但必须写进报告并重新审查。因为三方审查的枚举认为它在网格内无解，出现矛盾就要查清原因。

以下反例只在 P0 默认预算下运行：

- 独立审查的“1／12 支撑”反例：不得返回 FOUND。
- 资源目标反例：返回 `NEEDS_INFORMATION/unproven_resources`。
- 落点格子为 UNKNOWN：返回 `NEEDS_INFORMATION/unknown_world`。
- 极小预算：返回对应的预算类原因。
- 只有被挡住的候选：不得返回 BLOCKED。

### 5.7 RED

先写下面几项，在 R1 之后的贪心前端上运行：

- 无助跑 2 格 A5 找到。5A 原版在这项上会耗尽预算。
- 对每一组跨隙，A3 能找到的，A5、A15 也都能找到。
- `turn_90`、`jump_up_after_turn` 找到。
- “助跑 3 格 A3”返回 `SEARCH_EXHAUSTED`。

前两项和最后一项必须真实失败，三方审查的矩阵已经显示它们在贪心前端上会失败。转弯两项如果在贪心前端上已经通过，照实记录，作为回归检查保留。

### 5.8 验证与通过条件

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_primitive_matrix -v
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment tests.motion_nav.test_trajectory_proto_reference_search -v
.\.venv\python.exe -m unittest tests.motion_nav.test_standalone_export -q
```

通过条件：

- 第 5.6 节全部满足。
- 同一请求在 `PYTHONHASHSEED` 为 1、17、999 时，结果哈希和计数逐项一致。
- 报告中逐项列出：节点、物理 step、完成尝试次数、扫描次数、候选形状。
- 变异检查：
  - 去掉“不跳优先”，任何正例都不得因此失败。如果有正例失败，说明枚举不完整，要查清原因。
  - 去掉等待分支第一条命令剪枝，结果不变，只是计数增加。
  - 让 A 段跳过某一种步态，至少让一个正例失败。这项证明矩阵确实覆盖到了 A 段。

## 6. R3：降低成本

### 6.1 允许的手段

| 手段 | 条件 |
|---|---|
| 改变枚举顺序 | 随时可以，不影响完整性 |
| 可证明不丢解的剪枝 | 每条都要有测试证明不丢掉矩阵中的解，并做一次变异 |
| 缓存物理结果 | 只缓存 `(全部分支的完整状态, 输入)` 到 `step()` 结果的映射，伤害、高度、支撑等判断每次重新计算；缓存只在单个请求内有效，大小受物理 step 预算约束，请求结束后丢弃 |
| 减少网格点的剪枝 | 必须写进 coverage；矩阵仍须全部通过；不可行项仍须有类型地结束 |
| 复用或跳过停车尾迹 | 只能复用已经算过的同一状态和输入的结果，不能用公式估算停车距离 |

不允许的手段：

- 用复制来的运动公式代替 `step()`；
- 用墙钟决定行为；
- 放宽 R1 的判据。

### 6.2 测量

新增 `experiments/motion_navigation/trajectory_proto/measure.py`。墙钟只能出现在这个脚本里。

- 在 Windows 项目环境下运行，每个用例记录：
  - 冷启动：新进程里的第一次搜索；
  - 稳态：预热 3 次后，再连续测 20 次。
- 输出 JSON 到 `evidence/motion_navigation/trajectory-proto-5ar-v1/`，内容包括：
  - 每个用例的 P50、P95、P99、最大值；
  - 节点、物理 step、完成尝试次数、扫描次数、缓存命中；
  - 开环承诺余量，即“首个承诺边界 × 50 ms − 搜索耗时”，只报告，不作门槛。

### 6.3 门槛（按 D096）

**计时集：**

- Task 5A 三个代表；
- 直线跨隙中的 A3、A5 正例共 9 项（无助跑 1、2 格各两种，助跑 1、2 格各两种，助跑 3 格只有 A5）；其中“无助跑 1 格 A3”与跨隙代表是同一请求，只计一次；
- `flat_sprint`、`turn_90`、`jump_up_after_turn`。

计时集共 14 项。所有稳态样本合在一起，即 14 × 20 次，算 P95 和最大值。

| 结果 | 条件 | 处理 |
|---|---|---|
| 通过 | P95 ≤ 150 ms，最大值 ≤ 400 ms，全部正例在 P0 预算内 | 进入 5B |
| 有条件通过 | 150 ms < P95 ≤ 300 ms，最大值 ≤ 800 ms | 进入 5B，但 P1 必须达到 150 ms；P1 期间性能优化最多一轮 |
| 不通过 | P95 > 300 ms 或最大值 > 800 ms | 按 D096 结束 TP |

**A15 直线跨隙的 5 个正例只报告，不计入门槛。** 原因是这 5 项的 3 个朝向里有 2 个对直线跨隙没有用，它们测的是“多余选项会不会破坏稳健性”，这件事已由 R2 把关。转弯的两项使用 15 个输入，属于真实需要，已在计时集内。

**不可行项**必须在 P0 预算内有类型地结束，并报告耗时，不设毫秒门槛。

**R3 还要提出冻结 P1 的预算建议：** 节点、物理 step 和轨迹长度三项，依据是实测 P99 加上余量，并写明计算方法。

### 6.4 参考数据

本目录的 `template_probe.py` 在 Linux 上比较了三种模板和顺序。三者都基于三项修正版，两支同步推进，不限预算：

| 方案 | 计时集最贵一项（物理 step） | 说明 |
|---|---:|---|
| interleaved：固定朝向，每个起跳点先试完整个空中子网格 | 40482（`jump_up_after_turn`） | 不跳的候选排在后面；`turn_90` 靠起跳调整方向 |
| simple-first：固定朝向，按第 5.3 节排序 | 96533（`turn_90`） | 单一固定朝向的地面段落不进转弯目标，只能借起跳，反而更贵 |
| **steer：第 5.2 节的转向动作元加第 5.3 节排序** | **1968**（助跑 3 格 A5） | 计时集 14 项为 311—1968 次 step，Linux 52—244 ms |

完整数据见附录 B。按 5A 报告的折算，Windows 上每次 step 约 0.11 ms，150 ms 约等于 1360 次 step。按 steer 方案估算，计时集中最贵几项（1400—1968 次 step）在 Windows 上约 150—220 ms。也就是说，模板和顺序已经把成本拉到门槛附近；R3 还需要大约 1.5 倍的降幅，主要靠缓存和减少停车尾迹的重复计算。

这是 Linux 上的补充数据，不能代替 Windows 测量。

## 7. 交付与审查

**每一步的报告**放在 `.superpowers/sdd/2026-10-10-continuous-short-trajectory-prototype/task-5a-r{1,2,3}-report.md`，内容包括：

- RED 和通过的输出；
- 改写过的旧测试；
- 变异结果；
- 计数表；
- 证据边界。

**每一步的独立审查**写明是否通过，并且必须覆盖：

- 证明是否完整；
- 本步门槛；
- 规模测试：至少用附录 A 的矩阵重跑，不能只看三个代表。

**全部通过后**，更新：

- TP 的阶段和验收文档；
- D096 正式版中“5A-R 结果”一节。

结果写为“5A 经 5A-R 修正后通过”。5A 原记录不改写。

## 附录 A：矩阵夹具定义

所有夹具的公共设定：

- 世界：Java 1.21 规则，detached 小世界，坐标范围 x −3..3、y −5..6、z −3..12。
- 入口：沿用 `representative_fixture` 的 anchor，即 tick 10、位置 (0.5, 1, 0.5)、静止、朝向 0。
- 晚支：由真实 Neutral 等待一步得到。
- 目标：Walk 模式、standing 姿态、SOLID 支撑、末速 0、朝向 0、朝向误差 0；伤害额度为 0。

除非另外说明，地面为 y=0 全部实心。

| 夹具 | 世界改动 | 入口 | 目标区域（x／y／z） | 输入集 |
|---|---|---|---|---|
| 直线跨隙 `gap(width, start)` | y=0 层 z ∈ [start, start+width) 为 AIR | 公共入口 | .35—.65 ／ 1—1.05 ／ start+width+.3 至 start+width+1.3 | A3、A5、A15 |
| `flat_sprint` | 无 | 公共入口 | .35—.65 ／ 1—1.05 ／ 5.3—6.3 | A5 |
| `turn_90` | 无 | 见下 | 2.35—2.65 ／ 1—1.05 ／ 3.3—4.3 | TURN15 |
| `jump_up_after_turn` | y=1 层 z ≥ 3 全部实心 | 见下 | 2.35—2.65 ／ 2—2.05 ／ 3.3—4.3 | TURN15 |

直线跨隙的 start 取 1（无助跑）或 4（3.5 格助跑），宽度取 1—3，去掉 start=1、宽 3 这一组。

**输入集：**

- A3 = (Walk, Walk+Jump, Neutral)，朝向 0。
- A5 = A3 + (Sprint, Sprint+Jump)，朝向 0。
- A15 = 朝向 (−π/4, 0, π/4) × (Walk, Walk+Jump, Neutral, Sprint, Sprint+Jump)，按这个顺序展开。
- TURN15 = 朝向 (−π/2, −π/4, 0) × 同样 5 种。

物理里朝向 −π/2 表示向东（+x）。

**转弯入口：** 从公共入口开始，用 `Walk, yaw=−π/2` 真实 step 8 次，得到 anchor。它的 tick 为 18，位置 (1.969321, 1, 0.5)，速度 (0.116928, −0.0784, 0)。晚支等待输入为 `Neutral, yaw=−π/2`，等待后位置为 (2.08625, 1, 0.5)。入口必须由真实 step 构造，不能手填数值；上面的数值只用于核对。

## 附录 B：可行性与排序参考（Linux，三方审查探针）

三份输出：`template-probe-interleaved-output.txt`、`template-probe-simple-first-output.txt`、`template-probe-steer-output.txt`。三者都基于三项修正版，两支同步推进，不限预算，在 Linux 上运行。

表中数字为“物理 step／Linux 毫秒”，“—”表示没找到。

- 形状记法：W 为步行，S 为疾跑，N 为中性，J 为起跳（步行跳和疾跑跳都记作 J）；后面的数字是朝向（度），再后面的 x 是 tick 数。
- “无助跑 1 格 A3”与跨隙代表是同一请求，计时集只计一次。
- `flat_sprint` 的目标已改为 z 5.3—6.3。原先 6.3—7.3 的目标超出了“疾跑 20 tick 再刹车”能停到的距离（最远 z=6.107），只能借起跳补足，测不到疾跑加刹车。

| 用例 | interleaved | simple-first | steer | steer 找到的形状 |
|---|---:|---:|---:|---|
| 代表 flat_walk（计时） | 241／44 | 271／59 | 311／52 | W0x5 N0x7 |
| 代表 jump_up_straight（计时） | 451／78 | 544／81 | 566／87 | J0x1 W0x5 N0x10 |
| 代表 jump_gap_1＝无助跑 1 格 A3（计时） | 633／97 | 630／103 | 662／114 | W0x5 J0x1 N0x17 |
| 无助跑 1 格 A5（计时） | 633／96 | 679／96 | 711／95 | W0x5 J0x1 N0x17 |
| 无助跑 1 格 A15 | 633／84 | 1163／132 | 711／95 | W0x5 J0x1 N0x17 |
| 无助跑 2 格 A3（计时） | 2024／211 | 766／102 | 784／103 | W0x5 J0x1 W0x7 N0x11 |
| 无助跑 2 格 A5（计时） | 5390／564 | 815／99 | 833／101 | W0x5 J0x1 W0x7 N0x11 |
| 无助跑 2 格 A15 | 15252／1498 | 1299／175 | 833／100 | W0x5 J0x1 W0x7 N0x11 |
| 助跑 1 格 A3（计时） | 9760／907 | 1150／151 | 1182／167 | W0x19 J0x1 N0x17 |
| 助跑 1 格 A5（计时） | 14640／1481 | 1393／167 | 1425／170 | W0x19 J0x1 N0x17 |
| 助跑 1 格 A15 | 122800／11933 | 2713／283 | 1425／171 | W0x19 J0x1 N0x17 |
| 助跑 2 格 A3（计时） | 11241／1053 | 1291／157 | 1311／166 | W0x19 J0x1 W0x6 N0x12 |
| 助跑 2 格 A5（计时） | 21926／2115 | 1534／187 | 1554／182 | W0x19 J0x1 W0x6 N0x12 |
| 助跑 2 格 A15 | 200732／19451 | 2854／306 | 1554／191 | W0x19 J0x1 W0x6 N0x12 |
| 助跑 3 格 A3（不可行） | 网格无解 26073／2533 | 网格无解 26073／2217 | 网格无解 26073／2392 | — |
| 助跑 3 格 A5（计时） | 26959／2427 | 1944／227 | 1968／244 | W0x19 J0x1 W0x4 N0x15 |
| 助跑 3 格 A15 | 248551／23986 | 4116／414 | 1968／244 | W0x19 J0x1 W0x4 N0x15 |
| flat_sprint（计时） | 4877／500 | 1123／134 | 1163／153 | S0x18 N0x8 |
| turn_90（计时） | 941／129 | 96533／9560 | 724／109 | W0x14 W-45x1 N0x7 |
| jump_up_after_turn（计时） | 40482／4213 | 7067／679 | 1224／170 | W0x9 J0x1 W0x5 W-45x3 N0x8 |

三种方案都找到了全部 19 个正例，不可行项也都把网格枚举完后报告无解。差别只在成本。不可行项三种方案都要 26073 次 step，因为它必须把整个网格走完；按 D096，它只需在 P0 预算内有类型地结束，不设毫秒门槛。

