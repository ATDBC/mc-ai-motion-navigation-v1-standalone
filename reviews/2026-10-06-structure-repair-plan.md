# F1 后结构整理：S0 问题的修正方案与验收标准

- 日期：2026-10-06
- 对象：`main` 上的 [`1f0fefe`][new]，对照 [`b1d43c2`][old]（公开版中 F1 开始前的提交；12 个核心文件到 `1f0fefe` 净增 2,399 行，与 D069 记录的 `931ea8b` 起的净增相同）
- 依据：[S0 审查](2026-10-06-structure-s0-review.md)
- 原型与输出：[`2026-10-06-structure-repair-plan/`](2026-10-06-structure-repair-plan/)

## 结论

**不要直接开始 S1。** 先插入一步"S0-R 修正基线"，不计入四个整理批次，然后再按新门槛执行 S1—S4。S0-R 做四件事：

1. **测试修绿：** 修掉 5 项失败，并修复本轮新发现的运行时失败缺陷。
2. **基线覆盖要整理的代码：** 加入跟随集合和世界变化集合，再用"路径矩阵"证明每个候选都有基线走到。
3. **清单补到 F1 增长的位置：** 覆盖 `route_admission.py`、`route_validation.py` 和 Session 中由 F1 新增的部分，并补齐第二、三类候选。
4. **门槛改为逐项关闭：** 每个候选都要被删除、合并或写明经过验证的保留理由。合并前先做"变异检查"，证明保留下来的那处检查确实有测试保护。

做完这四步，S1—S4 才有意义：删改的代码有基线走到，删错了会被发现，门槛也不会因为删掉 34 行死代码就自动通过。

**这一轮核实后，有两处需要更正或补充：**

1. **更正我上一轮的判断。** 上轮我推测 R28-C-02 和补充故障的失败是因为 F1 让任务改走地面直走（D064），这个判断不对。实际原因是：F1 的行走复核（D058 起）会判断材料变化是否影响行走。石头和草方块对行走没有差别，所以路线被判定为"继续"，测试想检查的重规划和恢复路径就走不到了。

   | 场景（地面格 `(0,63,5)` 交替变化） | `b1d43c2` | `1f0fefe` |
   |---|---|---|
   | C-02：石头 ↔ 草方块 | 失败（符合测试预期） | **成功**（测试因此失败） |
   | C-02：石头 ↔ 下半砖 | 失败（符合预期） | 失败（符合预期） |
   | 补充故障：石头 ↔ 草方块，`begin_recovery` 次数 | 7 | **0** |
   | 补充故障：石头 ↔ 下半砖，`begin_recovery` 次数 | 10 | 10 |

   在 `1f0fefe` 上，草方块版本调用行走配方复核 16 次，路线复核没有一次给出"停止"；下半砖版本有 10 次给出"停止"，因此进入恢复。这是产品变好了，但测试没有发现自己已经失效。所以修法不只是换材料，还要加前提断言（R1-2）。

   seed=163 是另一回事：时序变化后，任务落进了一个真实的恢复缺口。身体还在空中时，"缺少状态"就被当成"运动无解"，任务被判定失败（R1-4）。

2. **新发现一个产品缺陷：后端 I/O 失败一次，正式链就抛出未捕获异常。** 在点任务和跟随任务中各注入一次可重试的后端 I/O 失败（模拟桥接读取超时），4 个注入点全部以 `ContractViolation: fixed route frame did not advance` 结束。
   - **现象：** 运行时已经正确标记为失败，处置结论是"重建运行时"；驱动器却进入"停止中、等待证据"，下一拍用旧观测重新规划，于是抛出异常。
   - **根因：** 驱动器状态有两个写入者。`_release_if_quiescent` 写入 `failed/control_unavailable` 之后，调用方又把它覆盖成 `stopping`；`release()` 还会通过会话取消和 `_sync_report` 再覆盖一次。
   - **原型修正：** 增 11 行、删 5 行，修正后点任务以带类型的 `driver_failed` 结束，不再抛异常。
   - **为什么必须在 S1 前处理：** 对一个要常驻服务器的机器人，这是比结构更优先的问题；它所在的正是 S2 要整理的状态与交接代码，而现有测试没有覆盖"执行中后端失败一次、之后驱动器继续被调用"这条路径。

   它不属于结构整理，应作为独立缺陷，单独提交。

**原型验证：**

| 代码状态 | 运动导航检查（1,478 项） |
|---|---|
| `1f0fefe` | 5 项失败 |
| 加入两个测试原型（运行器复制扰动设置、改用下半砖），正序 | 2 项失败：seed=163 与 D058 CLI 性能门槛，正好是方案中还需要动代码或拆测试的两项 |
| 再加驱动器原型，正序 | 同上 2 项，驱动器修正没有引入新失败 |
| 同上，逆序 | 只有 seed=163 失败；D058 CLI 在这个顺序下通过，说明结果取决于运行条件 |
| 同上，正序，另有一组套件并行运行 | 4 项失败，多出的 2 项都依赖实际耗时（R1-5） |

**时间盒：** 10-08 不变。S0-R 若在 10-07 结束前完成，S1—S4 照常用 10-08。如果做不完，就按原计划把本次结构整理如实记为未通过，再用一项新决定从 S0-R 的提交起开一个同样规模的时间盒。不要延长旧时间盒，也不要在基线不完整时开始删改。

## 一、问题与对策总表

| # | 问题（S0 审查） | 根因 | 对策 | 验收要点 |
|---|---|---|---|---|
| 1 | 清单只有 3 个零引用函数（34 行） | 只用静态"零引用"扫描，只能找到第一类 | R3：按 F1 新增函数与逐字相同的条件补第二、三类，每项写完整记录 | 清单覆盖 `route_admission.py`、`route_validation.py` 和 Session 的 F1 新增部分 |
| 2 | 门槛删 34 行就能通过 | 门槛只看"下降" | 改为逐项关闭；合并前做变异检查；"超时未处理"不能算通过 | 第六节 C1—C5 |
| 3 | 基线走不到要整理的代码 | 三组基线都是 F1 之前的场景 | R2：加入跟随集合和世界变化集合；生成路径矩阵 | 每个候选在至少一个集合中被调用，否则只能删除或标为"无保护" |
| 4 | 基线建立在 5 项失败之上 | 测试污染、前提失效、实际耗时进入单元测试、真实恢复缺口 | R1：逐项修绿 | 两种模块顺序都 0 失败 |
| 5 | （本轮新发现）后端 I/O 失败后抛出未捕获异常 | 驱动器状态两个写入者 | R1-6：只由一处决定，会话进入带类型的终态 | 第二节 R1-6 |

## 二、S0-R 第一步（R1）：测试修绿

原则：不删除测试，不放宽断言。每个改写过的测试都要断言"它要检查的路径确实被走到"，这样以后产品行为再变化时，测试会明确失败，而不是悄悄失效。

### R1-1 I3 落点复查：测试互相污染

- **根因：** `test_ready_probe_waits_for_selected_route_and_keeps_late_input_owner` 在运行中改写了 `backend.perturbations.late_ticks`。运行器把场景对象里的 `Perturbations` 原样传给后端，所以改写的是模块级共享场景 `direct_drop_2`。证据见上轮的 `scenario_leak_check`：运行前为 `[]`，运行后为 `[37]`。
- **修法：** 运行器在每次运行开始时复制扰动设置（`proto-runner-perturbation-copy.diff`，改动 1 行）。是否进一步把 `Perturbations` 改成不可变，可以以后再定；本步不需要。
- **验收：**
  - 完整套件按正序和逆序运行，I3 测试都通过；
  - 在套件开始和结束时对共享场景做深度哈希，两者相同（可以作为最后运行的检查，也可以由 `scenario_leak_check` 在 CI 中执行）。

### R1-2 R28-C-02 与补充故障 `active_route_dependency_retry`：测试前提失效

- **根因：** 见结论中的表。石头和草方块对行走没有差别，F1 的行走复核正确地判断路线可以继续，所以这两个测试失去了作用。
- **修法：**
  1. 把交替材料从草方块改为下半砖（`proto-walkability-changing-perturbation.diff`）。下半砖会改变行走条件（表面降低半格），但不会移走支撑。我也试过改成空气：会移走支撑，导致无法避免的坠落，触发 I3，所以不能用。
  2. **增加前提断言：**
     - C-02 断言路线复核至少给出一次"停止"，并且有新的规划提交；
     - 故障场景断言 `begin_recovery` 至少调用一次（现有测试已断言进入迁移函数）。
- **验收：** 两个测试在 `1f0fefe` 加原型上通过（完整套件中也通过，见 `unit-suite-with-prototypes.txt`）。用原来的草方块版本作反例运行时，前提断言会明确失败，而不是给出一个看似正常的"成功"或"失败"。

### R1-3 补充故障集合随之重冻

故障场景的扰动变了，所以 faults-4 中这一项的签名必然改变。这一项要在 R2 中随其他集合一起从 S0-R 的提交重新冻结，不能拿新签名去对照旧索引。

### R1-4 seed=163（R28-C-05 回归测试）：时序变化暴露了恢复缺口

时间线（`seed163_timeline-1f0fefe.txt`，drop_5 场景，目标在下方 5 格）：

| tick | 状态 |
|---|---|
| 38—39 | 提交前进命令 |
| 40 | 已过期的命令因注入的迟到被实际应用，状态为 `expired_pending_submission_retain_responsibility` |
| 41—44 | 保留落地责任，输入为零 |
| 45 | 身体靠惯性离开边缘；协调器此时在为地面运动重新准备（`repreparing_grounded_verified_motion`） |
| 46 | **人还在空中，会话已判定失败**：`motion_unsolvable:needs_state` |
| 46—55 | 驱动器保留落地责任，等待交接证据 |
| 56 | 安全落地，伤害 2（授权 2），没有违规；停在 x=3.32，离目标 1.18 格 |

判断：**安全没有问题，但失败判定下得太早。** 从边缘下降本来就是这条路线要做的事，只是这次发生在命令过期、身体靠惯性越过边缘的时候。协调器在空中尝试准备一个需要着地状态的地面运动，得到 `needs_state`（缺少可用的状态），会话就把它当作"运动无解"直接失败。按 AGENTS.md 的要求，"缺少信息"和"明确无解"必须分开表达：这里缺的是落地后的状态，应该等落地后从观测状态重新求解，而不是在空中宣布任务失败。同一种子在 `b1d43c2` 上成功，是因为当时的时序没有走到这个窗口；这个缺口是否在 `b1d43c2` 就已存在，我没有验证。

修法分成两部分：

1. **测试本身：** 把"依赖种子恰好在某个时刻迟到"改成"条件触发注入"：当会话处于后台复核并且朝向已经改变时，注入一次迟到。运行器已经支持 `Event(when=..., action=...)`。这样测试稳定地检查 R28-C-05 想保护的"复核期间朝向改变后能重新对准"，不再受时序影响。同一测试中的 seed=15、69、119，以及 `test_motion_start_delivery`、`test_async_gap_revalidation_delivery` 中按挑选种子写的回归测试，也应逐个检查是否需要同样改写。
2. **恢复缺口：** 登记为缺陷，保留种子 163 作为复现。
   - 优先在 S0-R 内修复：空中得到 `needs_state` 时继续保留落地责任，落地后从观测状态重新求解；只有落地后仍然无解才失败。
   - 来不及修就用 `expectedFailure` 加缺陷编号表达。修好后它会变成"意外通过"并提醒去掉标记。这是登记过的已知缺陷，不是冻结的"历史失败"。

### R1-5 依赖本机耗时的测试（D058 CLI 及同类）

- **根因：** D058 基准 CLI 测试本意是检查输出格式和拒绝覆盖，但 CLI 把性能门槛的通过情况当作返回码。本机准备耗时 P95 为 8.27 ms，超过 8 ms，测试就失败。
- **同类问题不止这一项。** 我把两组完整套件并行运行、提高机器负载后，正序结果比单独运行多出 2 项失败（`unit-suite-with-prototypes.txt`）：
  - `test_d061_long_session_benchmark`：断言单次最大耗时小于 50 ms，实测 66 ms；
  - `test_r28_persistent_budget_formal` 的第 13 次恢复测试：失败原因从预期的 `task_recovery_rate_exhausted` 变成了 `planning_timeout`。

  这两项单独运行都通过。另外，`test_d058_runtime_validation` 断言 P95 小于 1 ms，也是用实际耗时作通过条件。
- **修法：**
  - 单元测试只检查格式、拒绝覆盖和判定字段是否存在，不断言实际耗时；需要计时的地方用注入的计时器；
  - 性能门槛留在正式性能证据中检查；
  - R28 持久预算测试先查明哪一步读取了实际时间，再改成只依赖模拟时钟。
- **验收：**
  - 运动导航检查中，没有以实际耗时作通过条件的断言；
  - 在有并行负载时运行完整套件，结果与无负载时相同。

### R1-6 新缺陷：后端 I/O 失败后，正式链抛出未捕获异常

**复现**（`io_failure_probe.py`，只让后端的 `step` 抛出一次异常）：

| 注入点 | `1f0fefe` | 加入驱动器原型 |
|---|---|---|
| 点任务，行走第 5 / 20 tick | `ContractViolation: fixed route frame did not advance` | `driver_failed`，没有违规 |
| 跟随任务，第 30 / 120 tick | 同上 | 驱动器为 `failed/control_unavailable`；测试循环继续调用，被拒绝（见下文修法第 3 点） |

**因果链：**

1. `PlayerRuntimeV1` 报告 `BACKEND_IO`，并标记 `retryable=True`。运行时状态变为 `failed`，处置结论为 `recreate_runtime`。这一步是正确的。
2. 在 `RuntimeNavigationDriver.adopt_result` 中，`_release_if_quiescent()` 发现运行时不是 READY，于是写入 `failed/control_unavailable` 并返回 `False`。调用方把 `False` 理解成"还没静止"，又把状态改成了 `stopping/body_handoff_waiting_for_evidence`。
3. 外层循环看到 `stopping`，认为还可以继续 tick。但失败的运行时不会再产生新观测，于是驱动器拿同一帧再次调用 `propose`，固定路线控制器因此报告"帧没有前进"。
4. 驱动器的兜底逻辑会捕获 `ContractViolation`，转入安全停止，然后用**同一帧**再调用一次 `propose`，于是再次抛出，异常最终逃出 `tick`。
5. `release()` 中还有第二个写入者：它会取消会话，`_sync_report` 再把会话的 CANCELLING 映射回 `stopping`。

这正是 AGENTS.md 所说的"每类长期状态只有一个拥有者"被打破了：驱动器状态既由 `_sync_report` 根据会话报告映射，又在三处被直接赋值，而且用一个布尔返回值同时表达"未静止"和"失去控制"两种结果。

**修法：**

1. **只由一处决定**（`proto-driver-control-unavailable.diff`）：
   - `_release_if_quiescent` 自己决定是"释放"、"等待证据"还是"失去控制"，调用方不再覆盖；
   - `release()` 在运行时不是 READY 时，直接返回失败，不再走取消会话的路径。
2. **会话也进入带类型的终态。** 原型只修了驱动器，会话报告仍停在 `executing/tracking_fixed_route`。正式修复要让会话以 `control_unavailable` 之类的类型化原因结束。
3. **外层循环遵守终态。** 跟随测试循环只检查 `driver.source`，不检查驱动器状态。点任务运行器会检查。两个循环要一致。
4. **兜底逻辑不得用同一帧重试。** 如果第一次 `propose` 因为"帧没有前进"失败，重试注定失败。兜底应该只做不依赖新帧的安全停止，或者直接以类型化失败结束。

**验收：**

- 在点任务和跟随任务中，分别在行走中、制动中和空中（跨隙）注入一次可重试的 I/O 失败：
  - 没有异常逃出 `tick`；
  - 驱动器和会话都进入类型化终态；
  - 失败之后不再写输入；
  - 运行时的处置结论为 `recreate_runtime`。
- 不可重试的 I/O 失败也照此检查，处置结论应为 `end_episode`。
- 把这些场景加入补充故障集合（R2）。
- 这是行为修复，不是结构整理：单独提交，不计入结构的行数变化，并补一条设计决定或缺陷记录。

**边界：** 没有在 Fabric 上复现。真实桥接超时是否走同一条 `BACKEND_IO` 路径，需要读 Fabric 后端的异常映射来确认；但模拟器和正式驱动器是同一套代码，驱动器部分的结论不依赖后端。

## 三、S0-R 第二步（R2）：基线覆盖要整理的代码

### R2-1 加入第四、五组集合

| 集合 | 内容 | 原型结果 |
|---|---|---|
| 跟随 10 项 | F1 的 10 个跟随模拟场景，用同一个 `structure_signature` 签名（`follow_baseline_index.py`） | 10/10 通过；两次运行签名逐项一致 |
| 世界变化 N 项 | 行走中在路线上放置整块方块，在不同 tick 放置，在点任务和跟随任务中各做一组；再加 R1-2 的下半砖场景 | 点任务在第 10 tick 放置石头：成功，`replay_walk_validation_recipe` 调用 1 次 |
| 补充故障（扩充） | 原 4 项（含 R1-2 修正后的场景），加 R1-6 的 I/O 失败场景 | — |

注意：

- **用整块方块，不用下半砖。** 我试过在跟随路线的地面上放下半砖：模拟器的运动计算器在身体走上下半砖时返回 `unsupported`，然后触发了 R1-6 的缺陷。这是模拟器的能力边界。在计算器支持这一步之前，世界变化集合先用整块方块。
- **更正上轮"所有基线都是 0 次"的说法：** `replay_walk_validation_recipe` 在补充故障 `active_route_dependency_retry` 中被调用 16 次（`faults_path_exercise-1f0fefe.txt`）。但这期间路线复核没有一次给出"停止"；R1-2 换成下半砖后，"停止"分支才会被走到。

### R2-2 路径矩阵

把上轮的 `path_exercise_probe.py` 扩展为对**所有集合**、**所有清单候选**计数，输出一张矩阵：

```
候选 × {v7、协调、补充故障、跟随、世界变化} → 调用次数
```

规则：

- 候选在所有集合中都是 0 次，就不能合并或改写，只有三种处理：
  - 补一个能走到它的场景，放进集合；
  - 静态和动态都证明不可达，然后删除；
  - 记为"无保护，本阶段不修改"。
- 矩阵由脚本生成，作为证据提交，不手工填写。

用现有数据，已知需要补场景或做出判断的有：

| 候选 | 各集合调用 | 说明 |
|---|---|---|
| `RouteAdmitter.admit` | 全部为 0 | 调用者有 `admit_current_request` 的非表面分支，以及 `known_map_navigation_runtime.py`、`jump_up_navigation_runtime.py` 两个早期运行脚本 |
| `RouteAdmitter.admit_local_direct` | 全部为 0 | 只有在跟随策略下目标与机器人处于同一支撑面（`SAME_SUPPORT_LOCAL_GOAL`）时才会走到。10 个跟随场景都保持距离，所以走不到。应补一个"目标走近并停在同一支撑面"的跟随场景 |
| `NavigationSession._wait_for_active_terminal` | 全部为 0 | S0 已列为保留；仍需要一个公开路径场景，或者决定它是否可达 |

### R2-3 重新冻结

R1 修改了测试场景，R1-6 修改了驱动器行为，所以所有集合都要从 **S0-R 的最终提交**重新生成索引，并满足：

- 同一平台连续两次运行逐项一致；
- Linux 与 Windows 之间逐项一致。

S0 原来的索引保留作历史，不覆盖。S1—S4 一律对照新索引。

## 四、S0-R 第三步（R3）：补全清单

### R3-1 每项记录的字段

在 `deletion-inventory.json` 现有字段（调用者、状态拥有者、替代者、正式检查）之外，补充下列字段：

| 字段 | 内容 |
|---|---|
| `category` | 1 死路径 / 2 相邻层重复检查或转发 / 3 旧迁移适配 |
| `duplicate_of` 或 `legacy_of` | 与哪段代码重复；或者为哪个旧过程保留 |
| `kept_owner` | 合并后由哪个拥有者保留这个判断 |
| `entering_sets` | 路径矩阵中的调用次数（由脚本生成） |
| `detecting_check` | 变异检查的结果：去掉保留的那处检查后，哪个测试或集合会失败 |
| `expected_delta` | 预计减少的行数和分支数 |
| `risk` | 会触及哪些安全、恢复或交接路径 |
| `closure` | `deleted` / `merged` / `retained:<理由>` |

保留理由只能从下面几种中选择：

- `independent_responsibility`：有独立职责，附说明；
- `unprotected`：路径矩阵为 0，本阶段不修改；
- `merge_changes_behavior`：附上合并后不一致的集合与条目；
- `out_of_time`：时间不够，没有处理。

### R3-2 已找到的候选（S0-R 中逐项核实）

**`route_admission.py` 与 `route_validation.py`**（F1 新增 21 个函数，共 1,404 行）：

| 编号 | 线索 | 建议的处理顺序 |
|---|---|---|
| K1 | `admit` 与 `admit_surface` 有 9 条逐字相同的条件（请求 ID、世界会话、目标修订、几何版本、依赖与变化格、连通性、走廊长度等） | 先判断 `admit` 的非表面分支是否属于第三类：正式任务都走表面规划，各集合中 `admit` 都是 0 次。两个早期运行脚本如果仍是已签署阶段的证据工具，就以 `independent_responsibility` 保留。不要为了合并新建公共辅助层 |
| K2 | `admit_local_direct` 与 `admit_ground_direct` 有 6 条逐字相同的条件 | 先按 R2-2 补同一支撑面的跟随场景，再合并开头的共同检查，由同一处保留 |
| K3 | Session 中 `_can_attempt_ground_direct_request`（7 个条件）与 `_can_attempt_local_direct_request`（6 个条件）有 5 个条件相同，主要差别是 `surface_search_need` 的取值 | 改成一次分类：同一个函数返回"地面直走 / 同支撑直走 / 都不适用"三种结果，调用方按结果分派。这一项与 K2 一起做 |
| K4 | `RouteAdmitter._connection` 与 `route_validation.query_surface_walk_edge` 都判断 `movement`、`support` 是否 `FEASIBLE` | 行走边可行性在两个模块各算一次，属于第二类。确定由谁拥有这个判断（建议 `route_validation`），另一处直接使用它的结果 |
| K5 | `route_admission` 与 `route_validation` 的记录类 `__post_init__` 有 3 条逐字相同的校验（如 `recipe_ref`、`action_index`） | 判断这些记录是否本来就是同一个事实的两份副本；如果是，就属于"复制状态"，应该只保留一份 |
| K6 | `_surface_validation_plan` 单个函数 299 行；`_surface_action_route` 从 177 行增至 308 行 | 不作为删除候选。在审查 K1—K5 时，检查其中有没有重复判断 |

**`navigation_session.py`（F1 新增 6 个函数，共 170 行；`propose`、`_prepare_route_action`、`update_goal`、`_accept_goal_request` 各增长 22—31 行）：** 用 `git diff b1d43c2 1f0fefe -- mc2p/motion_nav/navigation_session.py` 按代码块归类：新能力、共享修复、重复检查或旧适配。前两类不进入清单，后两类逐项记录。

**`execution_supervisor.py`（`_validate_routes` 51 行，`advance_body` 从 37 行增至 68 行）：** 检查它与 `ActiveRouteTracker.validate` 之间是否存在重复的路线复核。

**原有 3 个第一类候选**（34 行）：保留在清单中，风险低，可以作为 S1 的第一批。

## 五、S1—S4 的执行规则

1. **一项一提交。** 提交信息写清单编号。一批可以包含同一职责的几项，但每项单独提交，方便回退。
2. **三道检查依次进行，任何一道不通过就回退该项：**
   - 直接相关的测试；
   - 完整运动导航检查，正序和逆序都要跑；
   - 五组集合与 S0-R 索引逐项一致。
3. **变异检查**，在提交前做：
   - **合并（第二类）：** 临时去掉保留的那处检查，至少有一个测试或集合条目必须失败。如果都不失败，说明这项检查没有保护，不能合并，只能先补场景，否则记为 `unprotected`；
   - **删除（第一、三类）：** 先把函数体替换成 `raise AssertionError`，五组集合和完整检查必须全部通过，同时静态上没有生产调用者。
4. **出现差异时，回退该项**，记为 `merge_changes_behavior` 并附上不一致的条目。不能把差异解释成"新预期"再去改写基线。
5. **不移动文件、不压缩写法、不新增状态类或公共接口。** 这与原计划相同。

## 六、验收标准

### S0-R 完成（开始 S1 的前提）

| 编号 | 标准 | 证据 |
|---|---|---|
| A1 | 运动导航检查正序和逆序（按模块）都是 0 失败、0 错误，在 Linux 和 Windows 上都满足 | 两种顺序的运行输出；逆序运行可参考 `reverse_suite.py` |
| A2 | `expectedFailure` 只用于登记过的缺陷，并附缺陷编号。目标为 0 项；如果 seed=163 的恢复缺口不在本步修复，最多 1 项 | 清单 |
| A3 | 共享场景在完整套件前后深度哈希相同 | leak check 输出 |
| A4 | 改写过的测试都断言了它要保护的路径确实被走到（R1-2、R1-4） | 测试代码；用旧材料或旧种子作反例，运行时断言明确失败 |
| A5 | I/O 失败注入（R1-6）全部以类型化终态结束，没有异常逃出 | 故障集合条目 |
| A6 | 单元测试不依赖本机耗时；有并行负载时完整套件结果不变 | 检查结果与负载下的运行输出 |
| A7 | 五组集合从 S0-R 提交冻结，两次运行一致、两个平台一致 | 索引与比较输出 |
| A8 | 路径矩阵覆盖所有候选，由脚本生成 | 矩阵文件 |
| A9 | 清单覆盖第四节列出的范围，每项字段齐全 | `deletion-inventory.json` v2 |

### 每个整理项

| 编号 | 标准 |
|---|---|
| B1 | 单独提交，提交信息写清单编号 |
| B2 | 依次通过直接测试、两种顺序的完整检查、五组集合逐项一致 |
| B3 | 变异检查结果记入该项的 `detecting_check` |
| B4 | 有差异就回退，并记录原因 |

### S4 结构签署

| 编号 | 标准 |
|---|---|
| C1 | 清单闭合率 100%：每项都是 `deleted`、`merged` 或 `retained:<理由>` |
| C2 | **结构门槛通过的条件：** K1—K5 这类第二类候选中，没有 `out_of_time` 或 `unprotected`；同时 13 个文件（12 个核心文件加 `route_validation.py`）相对 S0-R 净减少。另外报告相对 `b1d43c2`（即 `931ea8b`）的净变化，以及接纳入口数量（现在 5 个）和逐字相同的条件数量（现在 9 + 6 + 5）的变化。这些只报告，不设数字目标 |
| C3 | 五组集合逐项一致；完整检查两种顺序都是 0 失败；跟随结构审计通过；五个已签署 Fabric 目录的哈希不变 |
| C4 | 没有新增生产状态字段或公共接口，没有移动文件 |
| C5 | 新证据在 Git 中只保存索引和摘要，单个文件不超过 1 MB；原始性能明细放在 Git 之外 |

C2 的用意：如果所有第二类候选最后都以"时间不够"或"无保护"保留，那么即使删掉了 34 行死代码，结构门槛也不算通过。这样门槛就不会再"几乎必然通过"。

### 时间盒与如实记录

- 10-07 结束时检查 S0-R：A1—A9 全部满足才开始 S1。
- 10-08 或第四个批次先到，就停止整理。C1—C5 未全部满足的，如实记为未通过，并列出每项的 `closure`。
- S0-R 没有按时完成时，同样记为未通过。之后若要继续，要写一项新决定，从 S0-R 的提交起开新的时间盒，并说明原因。不能把旧时间盒往后延。

## 七、需要同步更新的文档

- `stages/post-F1-navigation-structure-cleanup-plan.md`：
  - 插入 S0-R；
  - 把门槛替换为第六节的 C1—C5；
  - 删去"不修复五项历史失败"，改成"S0-R 修绿，已登记的缺陷除外"。
- `acceptance/post-F1-navigation-structure-cleanup.md`：
  - 记录五组集合的冻结结果；
  - 写入完整命令，包括逆序运行。
- 新增一项决定：
  - S0 不完整，所以插入 S0-R；
  - 时间盒规则；
  - 运行时失败缺陷的处理。
- 缺陷记录：R1-4 的恢复缺口，R1-6 的驱动器状态写入冲突。

## 八、不建议做的事

- 先删 34 行、签署结构门槛，再说其余的。
- 用 `expectedFailure` 或"冻结失败"来代替修复测试前提（R1-2）。
- 为了合并重复检查新建一层"公共校验器"。应该先确定由哪个拥有者保留这个判断，其余位置直接使用它的结果。
- 把 R1-6 的修复算进结构整理的行数变化。
- 在补全基线之前，修改接纳或复核路径中的任何代码。

## 九、原型与复现

在 `1f0fefe` 检出目录的仓库根目录运行；`R` 指本目录，`S0R` 指 `reviews/2026-10-06-structure-s0-review/`：

```
git apply $R/proto-runner-perturbation-copy.diff $R/proto-walkability-changing-perturbation.diff
git apply $R/proto-driver-control-unavailable.diff
python -m unittest discover -s tests/motion_nav -p "test_*.py"
PYTHONPATH=. python -B $R/io_failure_probe.py
PYTHONPATH=. python -B $R/material_variants.py          # 分别在 b1d43c2 和 1f0fefe 上运行
PYTHONPATH=. python -B $R/seed163_timeline.py
PYTHONPATH=. python -B $R/world_change_exercise_probe.py
PYTHONPATH=.:$S0R python -B $R/faults_path_exercise.py
PYTHONPATH=. python -B $R/follow_baseline_index.py --output a.json   # 再运行一次得到 b.json，然后：
python -B $R/follow_baseline_index.py --compare a.json b.json
```

| 文件 | 内容 |
|---|---|
| `proto-runner-perturbation-copy.diff` | R1-1：运行器复制扰动设置 |
| `proto-walkability-changing-perturbation.diff` | R1-2：改用下半砖（前提断言尚未写入原型） |
| `proto-driver-control-unavailable.diff` | R1-6：驱动器状态只由一处决定（会话终态和外层循环尚未修改） |
| `unit-suite-with-prototypes.txt`、`reverse_suite.py` | 加入原型后完整检查的正序、逆序结果；逆序运行脚本 |
| `io_failure_probe.py`、`io_failure_probe-1f0fefe.txt` | R1-6 的复现，以及修正前后的对比 |
| `material_variants.py`、`material_variants-b1d43c2-1f0fefe.txt` | R1-2 的根因：不同材料在两个提交上的结果 |
| `seed163_timeline.py`、`seed163_timeline-1f0fefe.txt` | R1-4 的时间线 |
| `world_change_exercise_probe.py`、`world_change_exercise_probe-1f0fefe.txt` | R2-1：世界变化场景能否走到行走复核 |
| `faults_path_exercise.py`、`faults_path_exercise-1f0fefe.txt` | R2-2：补充故障集合的路径计数（更正上轮） |
| `follow_baseline_index.py`、`follow_baseline_index-1f0fefe.txt` | R2-1：跟随集合原型，两次运行一致 |

边界：

- 没有运行 Fabric，所有结果都来自模拟器。
- 只在 Linux 上运行，Windows 的一致性没有验证。
- seed=163 只定位到行为层面（空中把"缺少状态"当作"运动无解"），没有定位到具体代码行。
- R3-2 中的候选是线索，能否合并要按第五节的变异检查逐项确认。

[old]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/tree/b1d43c2
[new]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/tree/1f0fefe
