# F1 后结构整理 S0 审查：方法对了，清单、门槛和基线需要调整

- 日期：2026-10-06
- 审查对象：`main` 上的 [`1f0fefe`][new]（对照 [`b1d43c2`][old]）。重点是 [F1 后结构整理计划](https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1f0fefe/docs/motion_navigation/stages/post-F1-navigation-structure-cleanup-plan.md)的 S0，同时核对 F1 与 D056—D069 的相关部分。
- 复现材料：[`2026-10-06-structure-s0-review/`](2026-10-06-structure-s0-review/)

## 结论

**S0 的方法是对的，可以保留：**

- 只读量尺；
- 带版本的归一化规则（浮点取整到 1e-9，随机身份按出现顺序重新编号）；
- 先冻结基线再动代码，每次只删一组，失败立即停止；
- 设时间盒，删不出来就如实记为未通过；
- 明确写出不做的事（不搬文件、不压缩写法、不包装状态）。

这套量尺可以跨平台复现：我在 Linux 上重跑，v7 的 2,000 项、协调的 1,448 项和 4 个补充故障，与 S0 发布的索引逐项签名一致。

**但 S0 的方向有四个问题。按现在的清单和门槛执行，S1 删掉 34 行就能"通过"结构门槛，而 F1 新增的 2,399 行核心代码完全不会被触及。**

1. **清单找错了地方。** 3 个候选合计 34 行（22 + 2 + 10），全在 Session 和运动协调器里。F1 的增长集中在路线接纳：
   - `route_admission.py` 从 1,052 行增至 2,821 行，其中 `RouteAdmitter` 一个类就有 1,876 行，并存五个接纳入口；
   - 另新增 632 行的 `route_validation.py`。

   静态"零引用"扫描只能找到第一类（死代码），计划中真正的整理对象——第二类"重复检查与转发"和第三类"迁移适配分支"——都没有冻结清单。
2. **门槛太弱。** "核心文件总行数下降、决定分支下降"，删掉这 34 行就满足了。上一轮的问题是门槛定得过不去，这一轮是门槛定得几乎必然通过，而且会签署"代码减少且行为不变"。
3. **行为基线没有覆盖要整理的代码。** 我对各条路径计数：
   - F1 的主要新增路径 `admit_ground_direct`，在跟随场景中被调用 303 次，在 v7 产品任务和协调集合抽样中都是 0 次；
   - `admit_local_direct` 和行走配方复核 `replay_walk_validation_recipe` 在所有基线中都是 0 次。

   S0 冻结的三组基线都是 F1 之前就有的场景，F1 自带的 10 个跟随模拟场景没有纳入。整理这部分代码时，S4"逐项一致"的门槛起不到保护作用。
4. **基线建立在 5 项失败的测试之上，其中 4 项不是"历史失败"。** 它们在 R28 收尾的 `b1d43c2` 上都通过（上轮我在 Linux 上跑的是 1,207/1,207），是 F1 早期引入的：
   - `seed=163` 是 R28-C-05 的回归测试，现在任务失败；
   - `expired_planning_work_is_retired...` 是 R28-C-02 的回归测试，现在任务意外成功，测试原本要检查的退场路径走不到了；
   - 补充故障 `active_route_dependency_retry` 不再进入 `begin_recovery`；
   - landing-support I3 是安全不变量（"受到未经授权的伤害"），单独运行通过、完整套件中失败。我定位到原因：另一个测试改写了模块级共享场景的扰动设置。这处改写在 `b1d43c2` 就已存在，是 F1 的时序变化让它显现出来的。这不是产品缺陷，而是测试互相污染（第四节）。

   计划把它们"冻结为同 5 项，不在本阶段处理"，但这几项恰好对应恢复和安全路径。在这些路径失去测试保护的情况下整理同一子系统，顺序是反的。

**建议：S0 再补一个"修基线"的小步骤，不计入整理批次，然后再开始 S1。**

1. **补全清单：** 对 `route_admission.py`、`route_validation.py` 和 Session 中由 F1 新增的部分，列出第二、三类候选。第三节给出了已找到的具体重复。
2. **门槛改成按清单逐项关闭：** 每个候选要么删除或合并，要么写明保留理由；同时报告相对 F1 开始前（`931ea8b`）的净变化，而不只是相对 S0。
3. **基线加入 F1 的 10 个跟随模拟场景：** 再补一个行走中世界发生变化、能触发 `replay_walk_validation_recipe` 的场景。
4. **先让测试全绿：**
   - 另外 3 项多半是测试前提被 F1 改变（需逐项确认），应改写场景，让它重新走到原本要检查的路径，不能删除测试或放宽断言；
   - 修复测试对共享场景的改写，并让运行器在运行开始时复制扰动设置（第四节）；
   - 把依赖本机耗时的性能门槛移出单元测试的通过条件。

## 一、复现

在 Linux（4 核，Python 3.11.15）上用 `1f0fefe` 运行 S0 记录的命令，再用同一个 `navigation_structure_baseline.py` 生成索引并比较：

| 集合 | Linux 结果 | 与 S0 发布索引的比较 |
|---|---|---|
| v7 产品 2,000 项 | 1,718 项完成，零异常 | **2,000/2,000 签名一致** |
| 补充故障 4 项 | 4/4 通过 | **4/4 签名一致** |
| 协调 1,448 项 | 1,448/1,448 通过 | **1,448/1,448 签名一致** |
| 运动导航检查 | 1,478 项中 5 项失败 | 失败集合与 F1 冻结的不同，见第四节 |

S0 的归一化规则采纳了上轮的建议，并且确实解决了跨平台问题：上轮原样哈希有 92 项不一致，现在全部一致。随机身份的重新编号也没有造成误差。S1—S4 可以放心用这套量尺做"逐项一致"的判断。

公开版的生产源码指纹（`dfb5f6…`）与 S0 记录的不同（`0a795b…`），原因与之前一样：公开版多了导出清单这个配置文件。行为签名不受影响。

## 二、F1 的增长在哪里

用上一轮的 `structure_metrics.py` 统计 12 个核心协调文件：

| 提交 | 行数 | 决定分支 | `raise ContractViolation` | 校验行 | Session 行数 |
|---|---:|---:|---:|---:|---:|
| `b1d43c2`（R28 收尾） | 13,169 | 1,585 | 357 | 1,118 | 4,653 |
| `1f0fefe`（F1 + S0） | 15,568 | 1,808 | 416 | 1,464 | 5,051 |

| 文件 | 行数变化 | 分支变化 |
|---|---:|---:|
| `route_admission.py` | **+1,769** | +160 |
| `navigation_session.py` | +398 | +43 |
| `execution_supervisor.py` | +127 | +11 |
| `route_body_controller.py` | +59 | +5 |
| `planning_coordinator.py` | +31 | +2 |
| `navigation_owners.py` | +15 | +2 |
| `route_validation.py`（新文件，不在 12 个核心文件中） | +632 | — |

`route_admission.py` 的增长来自 D058—D064：行走前复核、终点选择依赖与执行依赖分离、终点节点证明、直走证明统一、完整目标面信息、先用已证明的地面直走。这 7 项决定在两天内依次修补同一条接纳和复核链，正是上次复杂度审查所说的"靠补 case 越写越复杂"，只是这次集中在了一个地方。所以结构整理最该看的就是这里。

另外，`route_validation.py` 是新文件，不在 12 个核心文件之内。如果整理时把代码从 `route_admission.py` 挪到 `route_validation.py`，核心行数会下降，实际却没有减少。计划已经禁止移动文件来制造下降，但量尺本身也应该把这个文件纳入统计。

## 三、清单应补的第二、三类候选

`RouteAdmitter` 有五个接纳入口：`admit`、`admit_surface`、`admit_current_request`、`admit_local_direct`、`admit_ground_direct`。我抽取了各入口中逐字相同的 `if` 条件：

| 入口对 | 逐字相同的条件 | 例子 |
|---|---:|---|
| `admit` 与 `admit_surface` | 9 条 | 请求 ID、世界会话、目标修订、几何版本、依赖与变化格、`not connected`、`action_route is None`、走廊长度上限 |
| `admit_local_direct` 与 `admit_ground_direct` | 6 条 | 入参类型检查、能力身份、世界会话、两处 `QueryStatus.FEASIBLE`、`ground_profile_allows_dependency_blocks` |

两个直走入口的说明分别是"已证明的同支撑直走"和"有界的普通同高直走"，开头的检查完全相同，是第二类的典型：同一个判断在相邻层重复做。

再结合各基线的实际调用次数（`path_exercise_probe.py`，只计数，不改变行为）：

| 入口 | v7 产品（每组前 20 个种子，共 200 项） | 协调集合（每 8 项取 1，共 181 项） | F1 跟随模拟（10 个场景） |
|---|---:|---:|---:|
| `RouteAdmitter.admit` | 0 | 0 | 0 |
| `RouteAdmitter.admit_surface` | 363 | 235 | 6 |
| `RouteAdmitter.admit_local_direct` | 0 | 0 | 0 |
| `RouteAdmitter.admit_ground_direct` | **0** | **0** | **303** |
| `ActiveRouteTracker.validate` | 11,187 | 4,544 | 1,508 |
| `replay_walk_validation_recipe` | 0 | 0 | 0 |
| `NavigationSession._wait_for_active_terminal` | 0 | 0 | 0 |

从中可以得出：

- `admit` 有生产调用者（非表面规划分支和若干运行脚本），但三组正式场景都没有走到它。它可能属于第三类"只为旧过程保留"的分支：要么证明仍可达并补场景，要么列为删除候选。
- `admit_local_direct` 同样三组都没有走到，应和 `admit_ground_direct` 合并评估。
- `admit_ground_direct` 只有跟随场景才会走到。如果不把跟随场景加入基线，整理这两个直走入口时没有任何行为门槛。

## 四、5 项失败测试

运动导航检查在 Linux 上是 1,478 项中 5 项失败：

| 测试 | 在 `b1d43c2` | 单独运行 | 现象 | 判断 |
|---|---|---|---|---|
| `test_changed_heading_during_background_revalidation_can_realign`（seed=163） | 存在且通过 | 失败 | `motion_unsolvable:needs_state` | R28-C-05 的回归检查失效 |
| `test_expired_planning_work_is_retired_at_every_observed_boundary` | 存在且通过 | 失败 | 预期失败，实际成功 | 场景不再走到 R28-C-02 的退场路径 |
| `test_public_faults_enter_the_migration_function_and_release_body`（`active_route_dependency_retry`） | 存在且通过 | 失败 | 不再进入 `retry_ledger.begin_recovery` | 恢复入口覆盖丢失 |
| `test_removed_landing_support_is_rechecked_four_ticks_before_departure` | 存在且通过 | **通过** | 完整套件中出现 I3"受到未经授权的伤害" | 测试之间共享状态泄漏 |
| `test_cli_writes_schema_and_refuses_overwrite`（D058 基准） | F1 新增 | 失败 | 本机准备 P95 8.27 ms，超过 8 ms 门槛，返回码 1 | 单元测试依赖本机耗时 |

另外，F1 冻结清单中的"公开整理版旧观察来源"（standalone 旧 CraftGround）在 Linux 上通过；D058 基准 CLI 测试则只在 Linux 上失败。两边的失败集合不同，说明"同 5 项"本身与环境有关，不适合作为整理阶段的固定门槛。

**判断：**

- 前三项的直接原因很可能相同：F1 让任务优先走已证明的地面直走（D064），不再经过后台规划，测试为检验恢复、退场和故障入口而设计的路径因此走不到了。这不一定是产品退步，但这三条测试已经失去了保护作用，而它们保护的正是整理阶段可能触碰的恢复路径。应当改写场景（例如选择直走不能证明的几何），让测试重新走到原路径，而不是冻结。
- I3 是安全不变量，单独运行通过、在完整套件中失败。F1 文档已经注意到"只在完整集合出现"，并登记为"时序问题"。我用二分定位了引起它的前序测试。原因是 `test_navigation_route_handoff` 里的 `test_ready_probe_waits_for_selected_route_and_keeps_late_input_owner`。

  - 它的控制步骤执行了 `context.backend.perturbations.late_ticks = frozenset({...})`。
  - 场景通过 `replace(source, ...)` 得到，而 `dataclasses.replace` 是浅复制，所以被改写的是模块级共享场景 `SCENARIOS["direct_drop_2"]` 的同一个 `Perturbations` 对象。
  - `scenario_leak_check.py` 证实：这个测试运行前，`late_ticks` 为空；运行后变成 `{37}`。
  - I3 测试先用同一个共享场景跑一次"干净"参照，以确定起跳时刻，再在起跳前 4 tick 移除落点。参照被注入了迟到，起跳时刻算错，移除可能发生在身体离边之后，于是出现了未经授权的伤害。

  所以这项"安全失败"是测试互相污染，不是产品缺陷。但要立即修复：

  - 运行器已经会对 `events` 做 `replace(event, fired_at=None)`，对 `perturbations` 也应在运行开始时复制一份（或把 `Perturbations` 改成不可变）；
  - 测试不能修改共享场景。

  同样的污染也可能影响在同一进程内连续运行多个任务的基线工具。
- D058 基准 CLI 测试本意是检查输出格式和拒绝覆盖，却把性能门槛的通过情况当作返回码。性能门槛应留在正式性能证据里，单元测试只检查格式。

## 五、对 S0 方向的具体建议

| 项目 | 现状 | 建议 |
|---|---|---|
| 清单范围 | 只有零引用函数（34 行） | 补齐第二、三类，重点是 `RouteAdmitter` 的五个入口、两个直走入口、`route_validation.py` 中的类型化记录与接纳侧重复的检查，以及 Session 中由 F1 新增的部分 |
| 门槛 | 核心行数和分支"下降" | 按清单逐项关闭：删除、合并，或写明保留理由；另报告相对 `931ea8b`（F1 开始前）的净变化；`route_validation.py` 计入统计 |
| 行为基线 | v7 + 协调 1,448 + 补充故障 4 | 加入 F1 的 10 个跟随模拟场景，以及一个行走中世界变化的场景 |
| 测试状态 | 冻结 5 项失败 | 先修成全绿：3 项改写场景；I3 改为不修改共享场景，运行器复制扰动设置；性能门槛移出单元测试 |
| 时间盒 | 10-08 前最多 4 批 | 保持不变。上面四项属于 S0 的补全，不计入整理批次；如果时间不够，就按计划如实记为未通过 |

不建议做的：

- 为了让行数下降而只删这 3 个零引用函数，然后签署结构门槛；
- 在恢复和安全测试仍然失效的情况下整理路线接纳和恢复路径；
- 把 F1 的增长解释为"共享修复所以不算"。D069 已经如实记录了结构失败，这一点值得肯定，整理阶段也应保持同样的标准。

## 六、整体进度

| 指标 | R28 收尾（10-04） | F1 + S0（10-06） |
|---|---:|---:|
| 正式跟随 | 未开始 | **功能通过**：Fabric 5/5，响应 P95 ≤ 4 tick |
| 薄跟随层 | — | 347 行，只调用 `start`、`replace_goal`、`release` |
| 12 个核心协调文件（行） | 13,169 | 15,568（+18%） |
| 运动导航代码（行） | 35,024 | 38,249 |
| 测试代码（行） | 51,500 | 63,448（+23%） |
| 文档（行） | 21,210 | 24,857 |
| 证据（MB） | 273 | 315 |
| 运动导航检查 | 1,207/1,207 | 1,473/1,478 |

F1 证明了 R28 的方向：一个只用三个公共接口的薄调用方就能实现持续跟随，修订响应也达到了 R28 没完成的 5 tick 门槛。但代价也很明显：两天内 12 项决定（D057—D068），其中 7 项都在修补同一条路线接纳链，测试从全绿变成 5 项失败。

证据瘦身也出现了反复：三份长会话性能明细各约 12 MB，`performance.json` 单文件最多 40 万行，没有按"Git 只存索引和摘要"的约定处理。

结构整理是现在该做的事，D056 和 D069 的安排也是对的。关键是把整理对准增长真正发生的地方，并让行为基线和测试先覆盖那里。

## 七、复现

在 `1f0fefe` 检出目录的仓库根目录运行，`R` 指本目录：

```
python scripts/navigation_coordination_metrics.py baseline --manifest tests/sim/manifests/navigation-product-r28-v7.json --output <p> --workers 4
python scripts/navigation_structure_baseline.py index product --source <p> --output <p.json>
python scripts/navigation_structure_baseline.py compare --baseline evidence/motion_navigation/post-f1-structure-s0/product-v7-index.json --candidate <p.json>
(协调与补充故障同理，使用 navigation_migration_evidence.py 与 index migration)
PYTHONPATH=.:$R python -B $R/path_exercise_probe.py --seeds 20
PYTHONPATH=.:$R python -B $R/path_exercise_probe.py --coordination
python -B reviews/2026-10-04-r28-structure-review/structure_metrics.py b1d43c2 1f0fefe
python -m unittest discover -s tests/motion_nav -p "test_*.py"
```

| 文件 | 内容 |
|---|---|
| `reproduction-1f0fefe.txt` | 三组签名比较、单元检查失败明细、I3 二分结果 |
| `scenario_leak_check.py`、`scenario_leak_check-1f0fefe.txt` | 共享场景被测试改写的直接证据 |
| `path_exercise_probe.py`、`path_exercise_probe-1f0fefe.txt` | 各接纳入口在各基线中的调用次数 |
| `admission_guard_overlap.py`、`admission_guard_overlap-1f0fefe.txt` | 五个接纳入口之间逐字相同的条件 |
| `structure_metrics-1f0fefe.txt` | 12 个核心文件的统计 |

边界：

- 没有运行 Fabric。
- 调用次数中，`query_surface_walk_edge` 在规划器中以别名调用，没有被计入，不能据此判断它未被使用。
- 逐字相同的条件只是重复的线索。能否合并，要由 S0 按"由谁承担、哪组检查能发现误删"逐项判断。

[old]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/tree/b1d43c2
[new]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/tree/1f0fefe
