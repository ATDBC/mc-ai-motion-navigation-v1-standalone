# 第二十轮评审：导航协调强化 H1–H7（eb4653c）

- 审查对象：`main` 上的 [`eb4653c`][base]（feat: publish navigation hardening and height matrix）
- 对照：
  - [第十九轮评审](2026-09-28-coordination-refactor-review.md)
  - [下一阶段验收门槛](2026-09-28-next-stage-gates.md)
- 方法：
  - 复跑声称的本地门槛；
  - 用第十九轮全部复现脚本重跑；
  - 跑门槛脚本 `gate_metrics.py`；
  - 在 `tests/sim` 上补"中断 × 阶段 × 晚到"组合。
- 复现：[`2026-09-28-coordination-hardening-review-repro/`](2026-09-28-coordination-hardening-review-repro/)

## 结论

**这一轮方向正确，整改扎实。** 第十九轮 7 项中 6 项已修复，第 7 项（落点支撑移除）大部分修复。转移表真正生效，统计口径也改对了。这是连续几轮以来第一次没有在原问题的同一位置冒出新的同类缺陷。

**但仍有 1 个新 P1。**
- 触发条件：落差动作进行中（第 36–45 帧）改目标或取消，随后再有一帧输入晚到。
- 结果：任务永远不结束。192 次组合里有 80 次如此，凡是"中断后再晚一帧"的组合无一例外。
- 根因：已验证动作被取消后进入 `recovering`，其中"等待命令应用"这一步没有期限。
- 这条路径正是修 P1-2（改目标时先取消在途风险动作）时新增的，属于门槛文档里 A2 子类（收尾无上界）的复发。

**你给出的数字需要补全两点：**
- **整格动作的晚到数据没列出来。** 1100/1100 是小高差的结果。整格动作在 20% 晚到下是 867/900，其中"下一整格" 89/100、"直接下落 1 格" 84/100。项目文档自己有这组数据，只是没放进汇总。
- **"固定晚一帧"的口径前后不一致。**
  - 连续高度验收第 44 行要求"每条指令固定晚 1 tick"下完成"下一整格"，新文字把正式要求写成"首条命令晚一帧"。
  - 按"首条命令晚一帧"：一格下降在我的扫描中全部通过（162 个单帧晚到位置 0 失败）。
  - 按第 44 行"每条指令都晚一帧"：一格下降失败，或进入一个监视器看不见的活锁（P2-A）。

**建议：** 不需要调整方向。进入 M3 全矩阵 Fabric 之前，先做三件事：
1. 修复这个 P1，把"中断 × 阶段 × 晚到"加进固定清单；
2. 补上监视器对"原地来回"活锁的检测；
3. 把第 44 行的口径定下来。

## 一、第十九轮问题复验

第十九轮全部脚本在 `eb4653c` 上的原始输出见 [`round19-repros-on-eb4653c.txt`](2026-09-28-coordination-hardening-review-repro/round19-repros-on-eb4653c.txt)。

| 编号 | 第十九轮 | 现在 | 判断 |
|---|---|---|---|
| P1-1 先走几步再下一格 | 正常输入 18 个变体失败 16 个 | 18/18 成功；各种助跑长度都成功；任意单帧晚到 0 失败 | **已修复**（按"首条命令晚一帧"口径） |
| P1-2 起跳那一帧改目标卡在 planning | 永久停在 planning | 第 36–41 帧改目标全部成功 | **已修复**；但"改目标后再晚一帧"见新 P1 |
| P1-3 风险账本 64 条后卡死 | 第 11 次卡死 | 15 次连续转身下落，每次只占 1 条 | **已修复** |
| P1-4 固定晚 1 tick 永远 stopping | 永久 stopping | 所有 ≥2 格下落以 `edge_probe_acquisition_timeout` 有界结束 | **活性已修复**；常延迟下探边仍取不到证据，属能力缺口 |
| P1-5 落点被移除仍起跳 | 提前 1–12 帧都跳下，8 点伤害 | 提前 ≥6 帧：`landing_support_missing` 安全拒绝；提前 1–4 帧：仍跳下，8 点伤害、I3 | **大部分修复**，见 P2-B |
| P2-1 侧边落点报"取消" | 报 cancelled，驱动永远 stopping | 报 `motion_unsolvable`，第 55 帧结束并释放身体 | **已修复** |
| P2-2 单帧扰动终止任务 | 单帧晚到失败 11/74；丢一帧仲裁即失败并在边缘释放 | 所有场景单帧晚到 0 失败；丢一帧仲裁后潜行退回再成功 | **已修复** |

**成功率（同批种子 `280001..280100`）：**

| 场景 | 20% 晚到 | 5% | 2% | 第十九轮（20%） |
|---|---:|---:|---:|---:|
| `direct_drop_2` | 98/100 | 100/100 | 100/100 | 13/100 |
| `direct_drop_5_budget_2` | 98/100 | 100/100 | 100/100 | 5/100 |
| `half_steps_up_down` | 100/100 | 100/100 | 100/100 | 100/100 |

**复跑声称的门槛**（Linux、Python 3.11，见 [`checks-eb4653c.txt`](2026-09-28-coordination-hardening-review-repro/checks-eb4653c.txt)）：
- `tests/motion_nav` 708/708 通过；
- 固定矩阵：13 个任务成功、1 个有界安全失败、0 个意外结果；
- 种子扫描：198/200 成功、2 个有界安全失败、0 个意外结果。

这比文档记录的 192/200 更好，可能是文档写于最后几次修正之前。本机没有 Fabric，实机批次未复核。

## 二、新 P1：落差中被中断后再晚一帧，任务永远不结束

**现象**（[`interrupt_late_family.py`](2026-09-28-coordination-hardening-review-repro/interrupt_late_family.py)）

- 场景：`direct_drop_2`、`direct_drop_5_budget_2`。
- 在第 30–45 帧改目标或取消，晚到取"无 / 中断后第 1 帧 / 中断后第 2 帧"三种。
- 共 192 次，其中 80 次永远不结束，全部是"第 36–45 帧中断 + 随后晚一帧"的组合；没有晚到的组合都正常结束。

| 中断 | 卡住的状态 | 身体位置 |
|---|---|---|
| 改目标 | `executing / executing_safe_prefix_during_replan`，300 帧 | 第 36–37 帧中断时停在边缘（z=3.07），还没掉下去；更晚的已落地 |
| 取消 | `stopping / recovery_unresolved`，驱动也停在 `stopping` | 已落在下层，速度为 0，来源仍被占用 |

**根因**（[`revise_then_late_inner.py`](2026-09-28-coordination-hardening-review-repro/revise_then_late_inner.py)、[`cancel_then_late_inner.py`](2026-09-28-coordination-hardening-review-repro/cancel_then_late_inner.py)）

- 中断后，`VerifiedMotionExecutor` 进入 `recovering`。
- 被晚到的那条命令最终在窗口外生效，执行器随后一直返回 `awaiting_application`，路线执行器因此一直停在 `cancelling`。
- 取消路径上，会话在期限后把原因改成 `recovery_unresolved`，但状态仍是 STOPPING，驱动也仍是 `stopping`。父层收不到终态，而此时身体其实已经安全地站在下层。

**为什么门槛没发现。**
- 固定矩阵有"空中取消""探边中改目标"，但都没有晚到。
- 种子扫描有晚到，但没有中断。
- 两者的组合不在任何清单里。
- 我在门槛文档里写的场景族是"中断维度 × 阶段"，没有明确要求再乘以扰动维度，这是我的遗漏。

**建议。**
- `recovering` 下等待命令应用要有上界：超过命令的最晚生效帧后，按客户端输入样本判定"已应用"或"已丢失"，再从观察到的状态重新锚定。
- 身体已在稳定支撑上时，直接给出 `QUIESCENT`。
- `recovery_unresolved` 需要给驱动一个终态事件，例如"失败，但身体仍保留"，不能只改原因字符串。
- 把"中断 {改目标、取消、丢仲裁} × 阶段（探边到落地）× 晚到 {无、+1、+2}"加入固定清单。

## 三、按门槛复核

`gate_metrics.py` 输出见 [`gate_metrics-eb4653c.txt`](2026-09-28-coordination-hardening-review-repro/gate_metrics-eb4653c.txt)。

| 门槛 | d34a674 | eb4653c | 说明 |
|---|---|---|---|
| G1-1 直接写 `_state` 的方法 | 20 个 | **0**（只经 `_transition`） | 通过 |
| G1-2 丢弃 `record()` 返回值 | 6/6 | 6/6 | 事件表仍只记录不决定；可删去，或让它决定是否处理 |
| G1-3 监督者依赖具体控制者 | 3 个模块 | 3 个模块 | 统一控制者协议未做；以后加攀爬、游泳时会再碰监督者 |
| G1-4 新增会话字段 | 27 | 2（`_last_support_fraction`、`_replacement_failure_reason`） | 已大幅收敛 |
| G1-5 新增写状态的方法 | 9 | 0 | 通过 |
| G1-6 会话新增行占比 | 42% | 32%（467/1454） | 仍偏高 |
| G2 终态不可离开 | 38 次运行 2 次非法写入 | 转移表强制，708 项测试和矩阵无非法转移 | 通过 |
| G3 检测前移 | 7/7 由外部评审发现 | 新 P1 仍由外部评审发现 | 缺组合场景族 |
| G4 同类不复发 | — | A2（收尾无上界）复发 | 见第二节 |

**G2 还有三处细节：**
1. **"交接"两个动作不看证据。** 转移表允许 `STOPPING → EXECUTING / PLANNING` 只走 `*_AFTER_HANDOFF` 动作，但会话选哪个动作只看"当前是不是 STOPPING"（[第 1246 行][handoff1]、[第 2698 行][handoff2]），不看交接证据。建议让这两个动作带上 `HandoffEvidence`，由表检查。
2. **正式运行中遇到非法转移会直接抛 `ContractViolation`。** 驱动没有捕获，异常会直接交给父层。门槛文档要求的是"进入有类型的内部失败，并由监督者继续持有身体"，目前没有实现。
3. **监视器 I11 只要求释放时支撑比例 > 0**，路线释放判定仍是 `minimum_support=.01`（[execution_supervisor.py 第 183–186 行][quiesce]）。门槛文档约定的是 ≥ 0.8 或保持潜行。这一轮丢仲裁的场景已改为"潜行退回后再判定"，实际表现安全，但门槛本身仍然偏松。

## 四、其他问题

### P2-A 每条指令都晚一帧时，一格下降进入"原地来回"活锁，监视器看不见

- 复现：[`constant_latency_livelock.py`](2026-09-28-coordination-hardening-review-repro/constant_latency_livelock.py)
- 助跑 3 格后下一格，每条命令都晚一帧：入口恢复在 `settling / recovering_grounded_verified_entry` 之间以 6 帧为周期前后挪动（前进 -1、0、+1 并潜行），直到 300 帧上限。
- 重试计数停在 2，不再增加。
- 身体每帧都在动，I4 的静止计数永远达不到阈值，所以没有报警。
- 这属于 A2 子类。

**建议：**
- 入口恢复次数计入 RetryLedger，或者给一个明确的上限；
- 监视器增加"同一动作阶段持续 N 帧没有进展"的检查，不只看身体是否静止。

是否要求这种条件下成功，取决于第 44 行口径如何确定；但无论口径如何，都不能无限持续。

### P2-B 首条下落命令发出后、离地前约 4 帧，支撑被移除仍会跳下

- 首条下落命令在第 36 帧，离地在第 40 帧。支撑在这之间被移除时，仍会跳下 9 格，受伤 8 点。
- 文档把这段时间视为"物理上无法撤回"。但这 4 帧里身体还在地面上，按下潜行就可以停在边缘。
- 建议：已验证执行器在身体仍着地的帧继续复查支撑，失效时改为潜行停住。
- 需要 Fabric 对照确认实际能挽回的帧数。

### P3 半砖和楼梯路线每次开局都作废首条路线

- 复现：[`first_dependency_change.py`](2026-09-28-coordination-hardening-review-repro/first_dependency_change.py)
- 无扰动时，`half_steps_up_down` 和 `stair_descent_4` 都在第 2 帧因 `ground_traversal_dependency_changed` 放弃首条路线并重规划。这会消耗一次 EXECUTION 重试。
- 种子扫描里半砖"首次成功 0/100、每次恰好 1 次恢复"就来自这里，与晚到无关。
- 第 0 帧观察更新了 63 个格子：模拟先注入记忆，再用真实观察覆盖。
- 请先确认这是模拟注入顺序造成的假象，还是正式运行里"先用记忆规划、再被首帧观察作废"的真实行为。如果是假象，应修正模拟器，否则"首次成功率"这个指标没有意义。

## 五、复现文件

从 `eb4653c` 检出目录的仓库根目录运行 `PYTHONPATH=. python -B <脚本>`。

| 文件 | 内容 |
|---|---|
| `interrupt_late_family.py`、`.out` | 新 P1：中断 × 阶段 × 晚到，192 次 |
| `revise_then_late.py`、`revise_then_late_inner.py`、`cancel_then_late_inner.py` 及 `.out` | 新 P1 的轨迹和执行器内部状态（后两者读取私有字段，仅用于诊断） |
| `step_down_single_late.py`、`.out` | 一格下降：单帧晚到扫描与每条指令晚一帧 |
| `constant_latency_livelock.py`、`.out` | P2-A 活锁 |
| `first_dependency_change.py`、`.out` | P3 开局路线作废 |
| `round19-repros-on-eb4653c.txt` | 第十九轮 `run_all.sh` 在 `eb4653c` 上的完整输出 |
| `gate_metrics-eb4653c.txt` | 门槛脚本输出（以 `d34a674` 为基） |
| `checks-eb4653c.txt` | 单元测试、固定矩阵、种子扫描摘要 |

说明：第十九轮的写入探针 `terminal_resurrection.py` 包装的是 `_state` 的写入口。现在状态只能经 `_lifecycle.transition` 改变，探针报告的 0 次不再有意义。非法转移由转移表直接抛异常，监视器 I13 负责计数。

[base]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/tree/eb4653c
[handoff1]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/eb4653c/mc2p/motion_nav/navigation_session.py#L1244-L1250
[handoff2]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/eb4653c/mc2p/motion_nav/navigation_session.py#L2696-L2701
[quiesce]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/eb4653c/mc2p/motion_nav/execution_supervisor.py#L148-L190


## 后续

对第二十轮整改（`340cac6`）的复核见 [第二十一轮评审](2026-09-29-review20-remediation-review.md)。
