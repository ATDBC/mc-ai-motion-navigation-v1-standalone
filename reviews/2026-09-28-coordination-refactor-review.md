# 第十九轮评审：导航协调重构 S0–S5（d34a674）

- 审查对象：`main` 上的 [`d34a674`][base]（refactor: publish navigation coordination S0-S5）
- 对照：上一版 `8898cf9`；上一轮重构方案 [`2026-09-28-refactor-plan.md`](2026-09-28-refactor-plan.md)
- 方法：
  - 复跑提交声称的全部本地门槛；
  - 阅读新增模块和会话改动；
  - 用仓库自带的 `tests/sim` 正式路径模拟构造对抗场景，共约 30 个场景族、上千次运行；
  - 脚本和原始输出在 [`2026-09-28-coordination-refactor-review-repro/`](2026-09-28-coordination-refactor-review-repro/)。

## 结论

**方向基本正确，但本轮最关键的一步 S4 没有真正完成，验收文档却把它标成了"本轮门槛关闭"。**

**1. 做对的部分**

以下几项都按方案落地，其中两处比我给的方案更好：

- 工具先行，模拟走正式链路，且只读诊断；
- 身体控制权收口到一个监督者，交接带类型化证据，并允许移动中交接；
- 重试和伤害分成两本有类型的账本；
- 动作前置条件放到动作边界才检查；
- 跨模块 Fabric 回归。

**2. S4 没有做到**

S4 的目的是让每个状态只能按表转移。现在：

- 会话没有变小，反而翻倍：1840→3076 行，43→92 个方法，直接写 `_state` 从 43 处增加到 75 处。
- "转移表"只记录事件，不给出下一状态。会话里没有任何调用检查它的返回值，写状态也不经过它。

**3. 这不是风格问题**

- 本轮找到的问题里，有两个永久卡死都属于同一种写法：一个分支写了"失败"，下一帧另一个分支又把它改回"执行中"或"等待取证"。
- 如果终态不可离开、状态只能经表写入，这两次写入会被直接拒绝。
- 写入探针在 38 次运行里只抓到这 2 次非法写入。说明大部分逻辑已经对了，缺的正是最后那道结构闸门。

**4. 门槛复现与新问题**

提交声称的门槛全部复现：664/664、固定矩阵 14/14、种子 200/200、相关模块 168 项通过。

但在同一个模拟器上，对抗场景找到 5 个 P1、2 个 P2。对实际使用影响最大的是 P1-1：

- **先走几步再下一格台阶，无扰动时 18 个变体失败 16 个。**
- Fabric 代表场景只覆盖"站在边缘起步"，所以 12/12 没有暴露它。
- 该问题在 `8898cf9` 就存在；我上一轮给的 14 场景矩阵同样漏掉了它。

**5. 验收数字需要换写法**

- **种子扫描**：
  - "200/200"是"落在允许结果内"，不是成功率。
  - 2 格下落在 20% 晚到下实际成功 13/100，5% 晚到下 57/100。
- **固定矩阵**：14/14 里有一项是被标为正例的 `failed/input_lost`。
- **监视器**：有两处豁免，正好挡住了 P1-4 和 P2-1。

**建议：暂不恢复新能力。** 按顺序做：

1. 让转移表真正生效。
2. 修复下面 5 个 P1，每个都以场景族（而不是单例）进入固定清单。
3. 种子扫描改为报告成功率分布。
4. 把监视器豁免改成有界检查。
5. 做三项 Fabric 对照，然后重走 S5。

## 一、方向判断

### 1.1 做对了什么

- **工具先行，并且走正式路径。**
  - `tests/sim` 驱动 `PlayerRuntimeV1 → RuntimeNavigationDriver → NavigationSession → 规划/接纳/执行器` 全链路，只读 `session.diagnostics`。
  - `tests/sim` 中私有字段访问为 0；我的原型里有若干处。
  - 清单带哈希，已知失败单独登记，种子冻结，真实后台进程另有门禁。
- **身体控制权收口。**
  - `ExecutionSupervisor` 是唯一拥有者，交接证据分为 `RETAIN / QUIESCENT / TRANSFERABLE`。
  - 路线替换允许**带速交接**。这比我方案里"先停稳再交"更好，也不会让带速场景变慢。
- **两本类型化账本。**
  - **RetryLedger**：
    - 按 attempt_id 幂等；
    - 只有被见证的进展才清零本轮计数；
    - 同因 2 次、本轮 6 次、任务总计 12 次。
  - **TaskRiskLedger**：
    - 把保留、提交、承诺、结算分开；
    - 首个不可逆输入前先保留额度；
    - 生命下降按保守值记账，缺样本不冒充完整结算。
- **动作前置条件移到动作边界（S3）。**
  - 远处的下降不再在接纳整条路线时提前探边。
  - 第十六至十八轮反复出现的"探边与路线互相等待"，这次从结构上消除了。
  - 我复跑了当时的场景：远落点 L 形、两级和三级台地、探边中改目标，全部通过。
  - 助跑 5 格后下 2 格，在 `8898cf9` 上经原型工具是探边超时，现在通过。两套工具的输入采样模型不同，这一条仅作参考。
- **跨模块 Fabric 回归。**
  - 连续高度、B10、C1-B、B11、B12-B 一起跑。
  - C1-B 实机发现"未获仲裁的待接替路线无法更新"，已记录并修正后复测。
- **文档诚实。** 以下内容都有明确记载：
  - "导航已迁移"与"技能层未迁移"分开写；
  - I7/I8 覆盖缺口；
  - 完整连续高度矩阵未关闭。

### 1.2 S4 没有完成

同一统计脚本（上一轮的 `code_metrics.py`）在两个版本上的结果：

| 指标 | 8898cf9 | d34a674 |
|---|---:|---:|
| `navigation_session.py` 行数 | 2167 | 3488 |
| `NavigationSession` 类行数 | 1840 | 3076 |
| 方法数 | 43 | 92 |
| `__init__` 字段 | 47 | 54 |
| `self._state =` 直接赋值（所在方法数） | 43（14） | 75（20） |
| `self._reason =` 直接赋值（所在方法数） | 42（14） | 78（21） |
| `propose()` 行数 | 215 | 491 |

我上一轮说过"会话文件长度不设指标，以职责归属为准"，所以行数本身不是问题。问题在职责归属：

1. **转移表不决定任何事。**
   - [`SESSION_EVENT_TABLE`][lifecycle] 的值只有 `HANDLE / IGNORE / REJECT`，没有下一状态和动作。
   - 会话里 6 处 `self._lifecycle.record(...)` 全部丢弃返回值。
   - `set_state()` 接受任意状态。
   - 实际转移仍由 20 个方法里的 75 处直接赋值决定。
   - 阶段文档第 146 行写的 `NavigationTransitionTable` 在代码里不存在。
2. **"六类状态所有者"中有三类是空壳。**
   - `GoalRequestLedger`、`PlanningPipelineState`、`InformationAcquisitionState` 是无行为的 dataclass，[文件自述][owners] "deliberately contain no policy"。
   - 策略仍全部在会话里。
3. **伤害状态有两份。**
   - TaskRiskLedger 之外，会话仍保留并继续累加 `_task_damage_budget`、`_movement_damage_spent_points`、`_executor_reported_damage_points`。
   - 账本存在时，这三个字段只进诊断，不参与决定。
   - 这违反 AGENTS.md"每类长期状态只有一个拥有者"。
4. **新增了一批"下一帧再处理"的暗号字段**，例如：
   - `_pending_probe_stop_cause`、`_pending_probe_terminal`、`_pending_probe_terminal_reason`；
   - `_probe_mode_exit_pending`、`_risk_failure_reason`、`_restart_after_active_terminal`。

   它们本应是转移表里的状态或事件。
5. **提交粒度。**
   - 公开仓库只有一个提交：73 个文件，+10432/−589，其中会话文件 +1859 行。
   - 阶段文档第 164 行自己写着"代码提交按可验收行为拆分；不能一次提交所有机制后再寻找回归来源"。
   - 验收文档也记载 S2 是在"未提交源码的脏状态"上运行的。
   - 如果私有仓库有分步提交，请在公开版附上每一步的提交号和对应矩阵结果。

**为什么说这是方向问题。** 我给会话的 `_state` 加了写入探针（[`terminal_resurrection.py`](2026-09-28-coordination-refactor-review-repro/terminal_resurrection.py)），覆盖以下 38 次运行：

- 固定矩阵；
- 20 个种子；
- 本轮的对抗场景。

探针统计"离开终态"和"放弃 STOPPING 转去非终态"两类写入：

| 写入 | 位置 | 后果 |
|---|---|---|
| FAILED → EXECUTING | `propose` 第 2186 行 | P1-2 永久停在 planning |
| STOPPING → NEEDS_INFORMATION | `_begin_action_acquisition` 第 1574 行 | P1-3 风险失败被丢弃，永久等待取证 |

另外 4 次 STOPPING → PLANNING 发生在"探边中改目标"，属于设计内的带速交接，不计入。

数量少，说明大部分分支已经写对。缺的是让这类写入**在结构上不可能发生**。这正是 S4 的价值；否则每新增一个控制者或一种失败，都要靠评审逐帧去找。

### 1.3 验收数字的写法

**种子扫描。** 汇总只记"接受/意外"，不记成功率。`direct_drop_2` 家族允许 `failed/input_lost` 等 4 种失败结果。用同一批种子 `280001..280100` 复算成功率（[`late_success_rate.py`](2026-09-28-coordination-refactor-review-repro/late_success_rate.py)）：

| 场景 | 20% 晚一帧 | 5% | 2% |
|---|---:|---:|---:|
| `half_steps_up_down` | 100/100 | 100/100 | 100/100 |
| `direct_drop_2` | **13/100** | 57/100 | 84/100 |
| `direct_drop_5_budget_2` | **5/100** | 46/100 | 76/100 |

**固定矩阵。**

- 清单把 `direct_drop_2_20pct_late` 的期望从原型的 `success` 改成了 `failed/input_lost`，分类仍是 `positive`，计入 14/14。
- 验收文档第 18 行仍写"2 格直接下落、20% 晚一帧 | 2/2 成功 | 保持成功"。

**监视器豁免。**

- I4 对 `stopping/recovery_unresolved` 明确豁免（[monitor.py 第 166–170 行][monitor-i4]）。
- 会话进入终态后，静止计数清零，也不再检查身体来源是否释放。
- 架构文档第 193 行写 recovery_unresolved "不是可无限等待的成功状态"，但实现和监视器都允许它无限持续。
- P1-4 和 P2-1 都因此没有被门槛发现。

这些不是隐瞒，文档写了"允许的安全终态"。但标题数字会被读成"全部通过"。

**建议：**

- 成功与安全失败分两列报告；
- 种子扫描输出结果分布；
- 豁免改成有界检查。

## 二、复现的门槛

在 `d34a674` 检出目录、Linux、Python 3.11 下运行：

| 检查 | 结果 |
|---|---|
| `python -m unittest discover -s tests/motion_nav -p 'test_*.py'` | 664/664 通过（126 s） |
| `python -m tests.sim.run_navigation_matrix --manifest tests/sim/manifests/navigation-coordination-smoke.json` | positive_pass 14，其余 0 |
| `python -m tests.sim.run_navigation_seed_scan --manifest tests/sim/manifests/navigation-coordination-s5-seeds.json` | 200 次，accepted 200，unexpected 0 |
| `tests.test_c1_navigation_session` 等 7 个相关模块 | 168 项通过，1 项跳过 |

本机没有 Fabric，因此未复核实机批次。

原生表面缓存库在 Linux 上用 g++ 编译为同名文件，只为让依赖它的测试能加载。

## 三、新问题

### P1-1 先走几步再下一格台阶，几乎总是失败

**现象。** 场景：一条单宽步道，末端下降一格，终点在下层方块中心（[`fabric_twin_drop.py`](2026-09-28-coordination-refactor-review-repro/fabric_twin_drop.py)）。

| 下降 | 助跑 1 格（即 Fabric 代表场景的孪生） | 助跑 2 格 | 3 格 | 5 格 |
|---|---|---|---|---|
| 1 格 | 成功 | **失败** `verified_entry_not_observed` | **失败** | **失败** |
| 2 格 | 成功 | 成功 | 成功 | 成功 |
| 3 格 | 成功 | 成功 | 成功 | 成功 |
| 5 格（额度 2） | 成功 | 成功 | 成功 | 成功 |

- 扩展到 36 个变体（[`isolated_step_down.py`](2026-09-28-coordination-refactor-review-repro/isolated_step_down.py)）：助跑 2/3/5 格、宽 1/3 格、3 种起点偏移，各跑正常输入和固定晚 1 tick。
- 结果只通过 2 个；**正常输入 18 个里失败 16 个**。
- 固定晚 1 tick 的失败原因是 `input_lost` 或 `fixed_route_stalled`。

**不是本轮回归。** 在 `8898cf9` 上用原型工具跑同样的助跑扫描，一格下降的失败原因和失败帧完全相同（[`old_twin-8898cf9.txt`](2026-09-28-coordination-refactor-review-repro/old_twin-8898cf9.txt)）。

**为什么所有门槛都没发现。**

- Fabric 连续高度代表场景 `direct-drop-1` 的起点，就是唯一那块上层方块的中心（`scripts/continuous_height_runtime.py` 的 `_supports`）。这等于站在边缘起步，模拟孪生同样通过。
- 固定矩阵里只有 `stair_descent_4`。连续一格下降会被合成为一段地面证明，不走受控下降。
- 我上一轮给的 14 场景矩阵也没有"先走再下一格"。这是我的遗漏。

**根因**（[`step_down_entry_mismatch.py`](2026-09-28-coordination-refactor-review-repro/step_down_entry_mismatch.py)）。

- 一格下降的动作证明是按入口 `z=1.332` 准备的。
- 第一条命令将要发出时，身体已经走到 `z=1.542`，偏差 0.21 格，约两帧步行距离；而入口容差只有 0.05。
- 于是 `VerifiedMotionExecutor` 判 `verified_entry_not_observed` 并进入 INPUT_LOST（[motion_candidate.py 第 729–745 行][entry]）。
- INPUT_LOST 被映射为任务终态失败。
- 两格以上的落差会先经过探边，探边把身体停在边缘，入口是静止的，所以能通过。

**建议。**

- 首条命令发出前的入口漂移不是失联：身体还没有做任何不可逆的事。应从当前锚点重新准备，计入 RetryLedger 的 EXECUTION。
- 或者在规划时把一格下降的入口定义为带速窗口，而不是一个点。
- 连续高度验收第 44 行要求"下一整格"在正常输入和固定晚 1 tick 下都完成，目前两者都不满足。
- 固定清单应加入以下场景族：助跑 1/2/3/5 格 × 一格下降 × 正常/固定晚 1 tick × 四个方向。

### P1-2 落差起跳那一帧改目标：任务永久停在 planning

**现象**（[`risk_revision_sweep.py`](2026-09-28-coordination-refactor-review-repro/risk_revision_sweep.py)）。

- 场景是 `direct_drop_5_budget_2`：额度 2 点，恰好够一次 5 格落差。
- 第 36–38 帧，也就是保留额度、首条下落命令刚发出时，把目标改到同一下层的相邻格。
- 身体照常落下并走到旧终点，然后会话停在 `planning / replacement_route_pending`，直到运行上限，I4 报警。
- 对照：
  - 同样时机但额度为 4：成功；
  - 第 39 帧以后（已离地）改目标：成功。
- 第 30–35 帧（探边中）改到同一格，会以 `edge_probe_acquisition_timeout` 结束。那是因为新目标在平台侧边那一列（与 P2-1 同一列），探边在那里取不到证据。这是 P2-1 同类的能力问题，与本问题无关。

**根因分两段**（[`trace_planner_results.py`](2026-09-28-coordination-refactor-review-repro/trace_planner_results.py)）：

1. **额度。**
   - 替换请求在边缘构建，剩余额度取自账本的 `available_points`。
   - 正在进行的这次落差已保留 2 点，所以替换请求的额度是 0，规划器返回 `unsupported`。
   - 但替换路线要走的，正是身体正在进行的这次落差。
2. **状态覆盖。** 依次发生三次写入：
   - `_advance_planning` 在旧路线仍执行时写入 `FAILED / planning_unsupported`（[第 2942–2945 行][plan-fail]）；
   - 下一帧 `propose` 的"请求已变更"分支写回 `EXECUTING / executing_safe_prefix_during_replan`（[第 2173–2187 行][prefix]）；
   - 旧路线完成后写入 `PLANNING / replacement_route_pending`。此时已经没有规划作业，只能无限等待。

**建议。**

- 替换规划的起点应放在"已承诺动作结束之后"，或者让请求携带这笔保留。
- 旧控制者仍负责身体时，规划失败要成为一个有类型事件："替换失败"。旧控制者结束后，按表进入 FAILED。
- FAILED 必须不可离开。

### P1-3 任务风险账本只增不减，64 条后任务卡死

**现象**（[`risk_record_growth.py`](2026-09-28-coordination-refactor-review-repro/risk_record_growth.py)、[`risk_capacity_same_task.py`](2026-09-28-coordination-refactor-review-repro/risk_capacity_same_task.py)）。

- 每次站到受控下降的边界都会开一条记录。
- 转向对齐期间的每一帧，导航只提视角、没有移动提案，却照样做了额度保留；驱动随后因"移动未被选中"把它当作仲裁失败释放，下一帧再开新记录。
- 实测记录条数：

  | 情形 | 记录数 |
  |---|---:|
  | 直行一次落差 | 1 |
  | 转 90° 后落差 | 4 |
  | 转 180° 后落差 | 6 |
  | 连续 8 个 2 格落差 | 7 |

- 已释放、已结算的记录永不回收，`_MAX_RISK_ACTIONS = 64` 按任务计。
- 用同一个任务账本反复"转身后下 2 格"（0 伤害）：
  - 第 11 次时账本满；
  - 会话停在 `needs_information / landing_lower_evidence_required`；
  - I4 在第 121 帧报警。
- 跟随、近战这类长任务在整个过程中共用一个任务号。在丘陵地形里，几十次落差就会触顶。

**根因。**

- 容量拒绝由 `_risk_checked_decision` 处理，写入 CANCELLING 并记下 `_risk_failure_reason`。
- 下一帧 `_begin_action_acquisition` 发现探边仍属于这个动作，写回 `NEEDS_INFORMATION`（[第 1573–1574 行][acq]）。
- 风险失败的原因就此丢失，探边一直等取证。

**建议。**

- 从未提交输入、已释放的记录不应占容量：可以直接删除，或按同一动作键复用。
- 或者只限制"未结算"的记录数。
- 容量耗尽必须成为有类型的终态，并且不能被取证状态覆盖。

### P1-4 固定晚 1 tick：所有受控下降场景永远停在 stopping

**现象**（[`fixed_latency.py`](2026-09-28-coordination-refactor-review-repro/fixed_latency.py)）。每条命令都晚一帧应用，即恒定一帧延迟：

- 平地、半砖、四级楼梯都通过。
- 所有含受控下降的场景在 600 帧内都不结束：
  - 状态停在 `stopping / recovery_unresolved`；
  - 身体从边缘后退半格，蹲着不动；
  - 身体来源一直不释放。

**根因**（[`why_not_clear.py`](2026-09-28-coordination-refactor-review-repro/why_not_clear.py)）。

- 探边在 STOPPING 期间每帧都重发"潜行保持"命令，常延迟下总有一条在途。
- `input_responsibility_status` 只跳过完全中性的命令（[online_motion.py 第 45 行][resp]），所以结果永远是 `IN_FLIGHT`。
- 监督者要求 `CLEAR` 才给出 `QUIESCENT`。
- 恢复期限耗尽后，报告 `recovery_unresolved` 并继续保留身体。
- 也就是说，控制器自己在不断制造阻止自己退场的证据。

**为什么门槛没发现。**

- 监视器对这个状态明确豁免 I4。
- 种子扫描把它列为允许结果；20% 晚到的 100 个种子里已经出现 1 次。

**现实性。**

- B10、B12 的 Fabric 实测严格窗口晚到率为 0%，Wilson 上限 8.76%。当前机器上没有出现常延迟。
- 但连续高度验收把"固定晚 1 tick"列为必须通过的条件。

**建议。**

- 停稳后发一条中性命令，等它应用后再判静止；或者在责任判断里认可"与已应用状态相同的保持命令"。
- `recovery_unresolved` 必须有上界，并向父层报告有类型的结果（身体仍保留）。
- 监视器改为检查"父层在 N 帧内收到结果"，不再豁免。

### P1-5 已知落点被移除后仍然起跳（需 Fabric 对照）

**现象**（[`landing_removed_timing.py`](2026-09-28-coordination-refactor-review-repro/landing_removed_timing.py)）。

- 在正常运行的首个离地帧之前 k 帧，移除整片下层地面。
- k 取 1、2、3、4、6、8、12 帧时结果都一样：身体仍然起跳，跌落 9 格，受伤 8 点，I3 报"伤害没有已授权的承诺"。
- k = 12 时，距下落命令开始还有 8 帧，探边还在取证。

**根因。**

- 前置条件只看落脚那一格（空气）的下部有没有新鲜的可见证据（[action_preconditions.py 第 183–191 行][precond]）。
- 它不检查下面的支撑方块是否仍在，也不看支撑上次被看到是什么时候。
- 支撑被移除后，落脚格仍是空气、下部仍然可见，证据照样成立；世界模型里的支撑仍是旧的"方块"。
- 模拟传感器与正式语义一致：看不到的方块不会被报告成空气。所以移除只能通过"本该每帧刷新的方块不再刷新"来发现。

**不确定性。**

- 结论依赖 Fabric 表面深度会不会把洞里看到的更低表面转成落点处的空气证据。
- 请做一次"起跳前 10 帧移除落点"的 Fabric 对照。
- 如果 Fabric 同样起跳，这是安全问题：AGENTS.md 规定安全不能被路径收益抵消。

**建议。**

- 对有落差的下降，要求落点支撑方块的观察时间足够新，例如与空气证据同为 80 帧窗口；在视野内时应每帧刷新。
- 不满足时进入取证。

### P2-1 角落落点：求解失败被报告成"取消"，之后驱动永远 stopping

**现象**（[`corner_and_late.py`](2026-09-28-coordination-refactor-review-repro/corner_and_late.py)、[`corner_reason.py`](2026-09-28-coordination-refactor-review-repro/corner_reason.py)）。

- 在 3 格宽平台上，把终点放在侧边那一列（x=1.5，前沿与侧边相交的角落）。2 格和 5 格落差结果相同。
- 第 44 帧求解器返回 `no_solution_within_search`。
- `MotionRouteCoordinator._accept_result` 调用 `executor.cancel()`（[motion_coordination.py 第 495–500 行][coord]），路线变为 CANCELLED。
- 会话把它映射为 `CANCELLED / "cancelled"`（[第 3061–3072 行][map]）。实际上没有任何人取消任务。
- 随后探边仍持有身体，驱动停在 `stopping / body_handoff_waiting_for_evidence`，256 帧直到关闭。

**为什么门槛没发现。** 会话一进入终态，监视器就清零静止计数，也不检查来源是否释放。

**建议。**

- 求解失败走有类型的失败，例如 `FAILED / motion_unsolvable`。
- 会话终态后，仍持有身体的控制者必须在有界时间内完成 STOPPING → QUIESCENT。
- 监视器增加"会话终态后 N 帧内释放来源"。
- 该落点本身无法求解（探边把身体停在 x=1.29，距侧边只有 0.29 格），这是能力问题，可登记为未覆盖。

### P2-2 单帧扰动就终止整个任务，失败后把身体留在边缘

**现象。**

- **单帧晚到扫描**（[`corner_and_late.py`](2026-09-28-coordination-refactor-review-repro/corner_and_late.py)）：

  | 场景 | 导致任务失败的晚到位置 |
  |---|---:|
  | `direct_drop_2` | 11/74 |
  | `direct_drop_5_budget_2` | 16/79 |
  | `far_landing_L_walkway` | 2/120 |
  | `stair_descent_4` | 0 |
  | `half_steps_up_down` | 0 |

- **单帧丢仲裁**（[`lost_arbitration_at_drop.py`](2026-09-28-coordination-refactor-review-repro/lost_arbitration_at_drop.py)）：
  - 另一来源以 SAFETY 优先级提交一次中性移动，模拟共享身体的出招或安全守卫。
  - 结果是 `failed / verified_command_window_expired`。
  - 身体在 z=3.29 被释放。边缘在 z=3.0，碰撞箱半宽 0.3，离掉下去只剩 0.01 格。
  - 释放时身体仍在滑动，没有潜行。
  - 2/5/8 格落差结果相同，这次都没有掉下去。

**根因。**

- INPUT_LOST 和窗口过期被设计为终态，不进 RetryLedger（架构文档第 59 行）。
  - 第十八轮把 INPUT_LOST 改为终态，是为了堵住无界重规划。
  - 现在 RetryLedger 已经提供上界，这个理由不再成立。
- 路线的静止判定 `evaluate_quiescence` 用 `minimum_support=.01`（[execution_supervisor.py 第 183–186 行][quiesce]），而探边自己用 .80。

**建议。**

- 未离地时窗口失效：重新准备，计入 EXECUTION。
- 已离地：落地后按 RetryLedger 重规划。
- 释放身体前的静止证据，至少要求与探边相同的支撑比例；或者释放时保持潜行。

## 四、次要问题与正面结果

**次要问题：**

- 固定晚 1 tick 下，`half_steps_up_down` 报 I9：会话宣布完成时，晚一帧应用的前进命令仍在推动身体。
- 会话以外仍有 17 处用字符串比较 reason 做分支：
  - `external_motion.py` 4 处；
  - `melee_strike_driver.py` 3 处；
  - 其余分散在放置、跟随、交战记忆和协议解析中。

  协议解析里的可能是正当用法；技能层的部分，阶段文档列为后续迁移，这里记下以免遗漏。会话内只剩 1 处，比较的是有类型的 `AdmissionReason`，不算问题。

**正面结果：**

- 探边保持边缘时取消：安全取消，来源释放，无违规（[`probe_interruptions.py`](2026-09-28-coordination-refactor-review-repro/probe_interruptions.py)）。
- 探边时朝边缘击退 0.25 格/帧：没有跌落，结果是有类型的 `edge_probe_acquisition_timeout`（同一脚本）。可以考虑用 RetryLedger 允许一次重新探边。
- 空中取消、探边中改目标：安全（固定矩阵）。

## 五、建议的顺序

1. **让转移表生效。**
   - 表给出 `(下一状态, 动作)`，`_state` 只能由 `transition(event)` 写入，终态不可离开，STOPPING 只能去终态或经表批准的交接。
   - 做状态 × 事件穷举测试。
   - 把本轮的写入探针变成门禁：固定矩阵和种子扫描中非法写入为 0。
2. **修复 P1-1 至 P1-5。** 每项附一个场景族，并按上一轮方案第七节做同类搜索：其他控制者、其他账本是否有同样写法。
3. **监视器。**
   - 删除 `recovery_unresolved` 豁免，改为有界检查；
   - 增加"会话终态后来源在 N 帧内释放"；
   - 增加"释放时支撑比例"；
   - 增加"任务风险记录只含未结算项"。
4. **种子扫描输出成功率分布。** 固定矩阵把"安全失败"和"成功"分开计数。
5. **Fabric 对照三项：**
   - 助跑一格下降；
   - 起跳前移除落点；
   - 固定晚 1 tick（若客户端可注入）。
6. 然后重走 S5，再恢复新能力。

## 六、复现方式

所有脚本都在 [`2026-09-28-coordination-refactor-review-repro/`](2026-09-28-coordination-refactor-review-repro/)，从 `d34a674` 检出目录的仓库根目录运行：

```
bash <复现目录>/run_all.sh > <复现目录>/outputs-d34a674.txt 2>&1
```

| 文件 | 用途 |
|---|---|
| `fabric_twin_drop.py`、`isolated_step_down.py`、`step_down_entry_mismatch.py` | P1-1 |
| `old_twin.py`、`old_twin-8898cf9.txt` | P1-1 在 `8898cf9` 上的对照（经上一轮原型工具） |
| `risk_revision_sweep.py`、`trace_revise_in_drop.py`、`trace_planner_results.py` | P1-2 |
| `risk_record_growth.py`、`risk_capacity_same_task.py`、`risk_capacity_trace.py` | P1-3 |
| `fixed_latency.py`、`trace_fixed_latency.py`、`why_not_clear.py` | P1-4 |
| `landing_removed_timing.py` | P1-5 |
| `corner_and_late.py`、`trace_corner.py`、`corner_reason.py`、`who_cancels_executor.py` | P2-1 及单帧晚到扫描 |
| `late_success_rate.py`、`lost_arbitration_at_drop.py` | P2-2、成功率分布 |
| `terminal_resurrection.py` | 第 1.2 节写入探针 |
| `probe_interruptions.py` | 第四节正面结果 |
| `code_metrics-d34a674.txt` | 第 1.2 节统计 |
| `outputs-d34a674.txt` | 全部原始输出 |

**说明：**

- 除 `lost_arbitration_at_drop.py` 读取驱动当前准备好的提案用于注入竞争者外，其余脚本只通过 `tests/sim` 的公开入口运行，并读取诊断或轨迹。
- 诊断根因的几个脚本会临时包装内部函数：`_state` 写入、`_accept_result`、`_state_fits_entry`、`input_responsibility_status`。它们只打印，不改变返回值。

[base]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/tree/d34a67473538cca2f69c2c0fce1e55b07a8692fd
[lifecycle]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/d34a67473538cca2f69c2c0fce1e55b07a8692fd/mc2p/motion_nav/navigation_lifecycle.py#L81-L103
[owners]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/d34a67473538cca2f69c2c0fce1e55b07a8692fd/mc2p/motion_nav/navigation_owners.py#L1-L10
[monitor-i4]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/d34a67473538cca2f69c2c0fce1e55b07a8692fd/tests/sim/monitor.py#L146-L171
[entry]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/d34a67473538cca2f69c2c0fce1e55b07a8692fd/mc2p/motion_nav/motion_candidate.py#L729-L745
[plan-fail]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/d34a67473538cca2f69c2c0fce1e55b07a8692fd/mc2p/motion_nav/navigation_session.py#L2942-L2945
[prefix]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/d34a67473538cca2f69c2c0fce1e55b07a8692fd/mc2p/motion_nav/navigation_session.py#L2173-L2188
[acq]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/d34a67473538cca2f69c2c0fce1e55b07a8692fd/mc2p/motion_nav/navigation_session.py#L1561-L1576
[resp]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/d34a67473538cca2f69c2c0fce1e55b07a8692fd/mc2p/motion_nav/online_motion.py#L35-L50
[precond]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/d34a67473538cca2f69c2c0fce1e55b07a8692fd/mc2p/motion_nav/action_preconditions.py#L183-L191
[coord]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/d34a67473538cca2f69c2c0fce1e55b07a8692fd/mc2p/motion_nav/motion_coordination.py#L484-L501
[map]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/d34a67473538cca2f69c2c0fce1e55b07a8692fd/mc2p/motion_nav/navigation_session.py#L3061-L3073
[quiesce]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/d34a67473538cca2f69c2c0fce1e55b07a8692fd/mc2p/motion_nav/execution_supervisor.py#L148-L190
