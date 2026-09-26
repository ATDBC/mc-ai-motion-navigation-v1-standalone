# B12 持续追击复审（第十一轮）

日期：2026-09-26
审查对象：`origin/main` 提交 [`3a1fdf0`][base]（D033：追逐阶段持续瞄准、导航原因记录、伤害缓冲清理、持续追击实机场景）
性质：外部审查意见，不属于 `docs/motion_navigation/` 四类正式文档，不改变任何阶段结论或验收门槛
前几轮：[整体审查](2026-09-24-architecture-review.md)、[C1-R 复审](2026-09-25-c1r-rereview.md)、[第三轮](2026-09-25-c1r-rereview-2.md)、[第四轮](2026-09-25-c1r6-review.md)、[第五轮（B11）](2026-09-25-b11-review.md)、[第六轮（D026）](2026-09-25-d026-review.md)、[第七轮](2026-09-25-final-review.md)、[第八轮（D027）](2026-09-25-d027-review.md)、[第九轮（B12）](2026-09-26-b12-review.md)、[第十轮](2026-09-26-b12-hardening-review.md)

本文代码链接都指向 `3a1fdf0`。本分支仍基于旧快照，请不要按本分支的相对路径查看代码。

## 结论

1. **上一轮四项都处理了，主体做法对。**
   - **B03：** 已改称“地面控制器回归”，不再用来证明 `NavigationSession` 的一帧授权。
   - **伤害缓冲：** 采样时移除没有追踪编号的非玩家目标事件（[`ClientDamageEventBuffer.java:54`][buffer-remove]）。用上一轮的脚本重跑：11、64、70 轮的丢弃计数都是 0，70 次机器人命中全部送达。
   - **追逐阶段的视角：** 战斗层每帧给出目标视角，导航按这份视角计算普通步行（[`moving_melee_driver.py:389`][pursuit]）。开始攻击、停止接近、接近失败、外力恢复和取消时都会释放这个视角来源，我逐一核对过。
   - **实机持续追击：** 目标用原版 AI、没有瞬移。43 个控制帧中，15 个转头帧有 9 帧同时移动。导航带移动的 30 帧里，28 帧绑定的正是当帧胜出的战斗视角，没有一帧对不上。另外 2 帧在进入攻击后，视角已经释放。
2. **中等问题一：导航原因只在“已有路线”时记录。**
   - 规划中的帧照样提交中性移动，但没有任何原因记录。
   - 正式证据里，诱发转向试验 52 帧中有 28 帧没有记录，持续追击 43 帧中有 8 帧没有。
   - 持续追击里“转头但没移动”的 6 帧中，有 4 帧属于这类。
   - 报告列出的“5 帧取消制动、1 帧停止后取消”，发生在评估窗口之后的命中收尾阶段，不能解释窗口内的任何一帧。
   - 所以 D033“中性导航帧全部能归到三类原因”与证据不符（第二节）。
3. **中等问题二：诱发转向试验开头，机器人原地站了 1.39 秒。**
   - 这 28 帧里，僵尸自己走近了 1.7 格，导航才给出第一条路线。
   - 上一批公开证据 `780e0386` 同样是 28 帧，说明它可以稳定复现。
   - 第 1 版目标 18 帧都没有路线，第 4 版目标只用 1 帧就被接纳。
   - 这些帧恰好没有原因记录，所以公开证据无法判断是规划慢、在等缺失信息，还是别的原因。
   - 对实时战斗伙伴来说，这是反应时间问题（第三节）。
4. **中等问题三：追到 30 秒任务上限时，会抛出契约异常。**
   - 新的追逐路径在期限已过时抛出 `ContractViolation("moving melee pursuit window expired")`（[`moving_melee_driver.py:396`][pursuit-raise]）。
   - 旧代码则会无视上限继续追。
   - 两者都没有把“任务超时”作为独立结果交给任务层（第四节）。
5. **两个小问题：**
   - 导航执行跳跃、台阶等动作时会自带视角。这份视角和战斗追逐视角谁胜出，目前由提交时间隐式决定（第五节）。
   - 公开证据中的持续追击，目标是迎面走来。目标远离或横穿的情况还没有覆盖；C1-B 回归批次也没有公开。

## 审查范围

- 阅读了以下文档：
  - D033；
  - B12-B 验收第 9 节；
  - D032 的补充段；
  - B12 整改计划的新表格。
- 阅读了以下代码的全部改动：
  - `navigation_session.py`、`moving_melee_driver.py`、`player_runtime_v1.py`；
  - `ClientDamageEventBuffer.java`；
  - `b12b_partial_combat_runtime.py`、`public_runtime_evidence.py`；
  - 对应测试。
- 为了核对行为，另读了以下代码：
  - `arbiter_v1.py` 的排序与视角绑定；
  - `fixed_route.py` 的转弯控速；
  - `NavigationSession` 的规划推进与缺信息等待。
- 在 Linux 容器（Python 3.11、OpenJDK 21）中按 README 运行了全部检查，并重跑了第九、第十轮的复现脚本。
- 解开新的 B12-B 公开批次 `1e50bcb7` 和上一批 `780e0386`，逐帧核对了以下内容：
  - 两场活动目标的仲裁结果；
  - 导航原因记录；
  - 目标位置和目标版本。
- 没有运行 Fabric 实机。

## 一、上一轮问题复核

| 上一轮问题 | 修复 | 我的复核 |
|---|---|---|
| B03 回归没有经过 `NavigationSession` | 改称“地面控制器回归”；`NavigationSession` 路径改由持续追击和 C1-B 证明 | 文档表述已一致。公开证据中能看到的 `NavigationSession` 连续步行，只有持续追击的平地部分（见第五节第 2 条） |
| 导航中性帧没有原因 | 新增 `navigation_route_decision` 任务事件（[`navigation_session.py:1471`][route-event]） | 有路线时记录完整；没有路线时不记录（第二节） |
| 活动目标只有一次瞬移诱发的试验 | 新增 `sustained-active-target-01`，原版 AI，不瞬移 | 数字与归档一致：43 帧、2.150 秒、目标自行移动 2.056 格、15 个转头帧中 9 帧移动，第 873 帧攻击，第 875 帧得到 `minecraft:player_attack` |
| 伤害缓冲留着无法送达的事件 | 采样时移除 | 上一轮 Java 脚本：丢弃计数全程为 0；上游新增的 Java 回归也通过 |

**追逐视角的生命周期，我逐项核对过：**
- 开始攻击前会释放。攻击帧 872–873 的仲裁结果里，只有移动和攻击，没有追逐视角。
- 停止接近、接近失败、外力恢复、取消时，都会调用 `_release_pursuit_look`。
- 补看只在接近驱动已经释放后才会发生，所以两个视角来源不会同时存在。
- 目标暂时不在观察中时，追逐视角返回空，导航改用 5° 观察朝向规则，不会拿旧几何去瞄准。

## 二、导航原因记录漏掉“还没有路线”的帧

**代码：**
- `propose` 在终态或还没有路线时，直接调用 `_proposal(MovementV1(), None, 1, deadline)`（[`navigation_session.py:825`、`:832`][early-return]）。
- 这时 `route_decision` 为空，`decision_events` 是空元组（[`:1369`][decision-events]）。
- 但本帧仍会提交一条中性移动意图。仲裁时，它作为移动组胜者，把移动压成零。

**最小复现：** [`planning_frame_has_no_decision_event.py`](2026-09-26-b12-pursuit-repro/planning_frame_has_no_decision_event.py) 用上游测试里的内联规划器挂起第一份规划，连续三帧的输出都是：

```
state=planning reason=planning_submitted intents=1 movement=MovementV1(forward=0, ...) task_events=0 route_decision=None
```

会话自己知道状态是 `planning`，原因是 `planning_submitted`，只是没有写出来。

**正式证据：** 用 [`active_trial_reason_coverage.py`](2026-09-26-b12-pursuit-repro/active_trial_reason_coverage.py) 统计 `1e50bcb7`：

| 试验 | 评估窗口 | 没有导航记录的帧 | 转头但没移动的帧 | 窗口内原因 | 窗口外、但计入报告的原因 |
|---|---|---|---|---|---|
| 诱发转向 | 767–818（52 帧） | 767–794，共 28 帧 | 1 帧：809 转弯控速 | 跟踪 21、转弯控速 3 | 819–824：取消制动 5、停止后取消 1 |
| 持续追击 | 832–874（43 帧） | 832–839，共 8 帧 | 6 帧：832–835 无记录，866、871 转弯控速 | 跟踪 30、转弯控速 5 | 875–880：取消制动 5、停止后取消 1 |

**评估脚本的两个口径问题：**
- 原因计数取的是整场导航会话的记录（[`b12b_partial_combat_runtime.py:271`][eval-reasons]），包含命中后的收尾帧。转头比例却只按控制窗口计算，两边统计的帧不一致。
- 验收条件只要求“至少有一条原因”且“原因不为空”（[`:311`][eval-reasons]），无法发现某一帧完全没有记录。

所以 D033 的第 4 条“导航每帧记录……”（[L17][d033-l17]）和验证结果中“中性导航帧全部能归到三类原因”（[L30][d033-l30]），以及验收第 9 节的对应表述（[L161、L169][acc-reasons]），都比证据说得更满。

**建议：**
- 没有路线的帧也写同一类事件。内容包括会话状态、原因、缺失格子数，以及规划是否仍在后台。
- 评估时只统计窗口内的记录，并要求窗口内每个导航帧恰好有一条。

## 三、诱发转向试验开头静止 1.39 秒

| 帧 | 时间 | 机器人 z | 目标位置 (x, z) | 采纳的目标版本 |
|---|---|---|---|---|
| 767 | 0.00 s | −2.50 | (0.50, 4.50) | 第 1 版（站位 z≈2.3） |
| 785 | 0.89 s | −2.50 | (0.56, 3.95) | 第 2 版 |
| 792 | 1.24 s | −2.50 | (0.52, 3.16) | 第 3 版 |
| 794 | 1.34 s | −2.50 | (0.52, 2.93) | 第 4 版 |
| 795 | 1.39 s | −2.50 | (0.52, 2.82) | 第一条导航记录（第 4 版）；开始前进 |

**要点：**
- 这 28 帧中，导航每帧都提交中性移动，机器人没有动；僵尸自己从 7.0 格走到 5.3 格。
- 第 1 版目标持续了 18 帧，一直没有路线。第 4 版在下一帧就被接纳，说明在这块平地上规划本身可以很快。
- 上一批 `780e0386` 的同一场试验：目标在 785 帧确定，813 帧才开始移动，同样是 28 帧；目标版本分别在 803、810、812 帧更新。这是可复现的行为，不是偶发抖动。
- 持续追击场景的对应等待是 8 帧（0.40 秒）；墙体遮挡边界是 3 帧。

**可能原因（未能验证）：**
- 规划得到“缺少已知信息”时，会话进入 `NEEDS_INFORMATION`。之后 `_advance_planning` 直接返回（[`navigation_session.py:1119`][needs-info]），要等缺失格子发生变化或目标换版本才会继续。
- 这和“目标换到第 4 版才出路线”的现象吻合。
- 但归档里没有任何会话状态记录，无法排除后台规划延迟或快照重启。第二节的记录补上以后，这个问题就能直接看出来。

**为什么值得查：**
- 真人看到怪物后，一般 0.2–0.3 秒内就会开始移动。
- 机器人等待 1.4 秒，而且等的是“目标走过来让目标换版本”，而不是自己拿到了信息。
- 如果目标不靠近，比如站着射箭的骷髅，这个等待可能会更长。

**建议：** 把“目标确定到首次移动”的时间作为 B12-B 的正式指标，并先查清这 28 帧的原因。

## 四、追到 30 秒任务上限时抛出契约异常

**代码：**
- 新的 `_tick_visible_approach` 把期限设为 `min(调用方期限, 任务期限, now + 500 ms)`。
- 期限已过时，直接抛出 `ContractViolation("moving melee pursuit window expired")`（[`:396`][pursuit-raise]）。
- 在这之前，追逐走 `approach_driver.tick(profile, owner_deadline_ns)`，根本不看任务期限。

**复现：** [`pursuit_task_limit_raises_contract_violation.py`](2026-09-26-b12-pursuit-repro/pursuit_task_limit_raises_contract_violation.py) 用上游 `test_moving_melee_driver.py` 的夹具：
- 目标保持在 6 格外；
- 进入追逐后，把任务期限放在 120 ms 之后；
- 按 50 ms 一帧推进。

```
3a1fdf0: frame 2 (-30 ms to task limit): ContractViolation: moving melee pursuit window expired
211af42: frame 2..5 (-30..-330 ms to task limit): status=running, driver=pursuing
```

说明：夹具的假 Runtime 撑不到 600 帧，所以脚本直接改了任务期限，而不是真的推进 30 秒。

**影响：**
- 追一个一直后退或绕圈的目标，30 秒并不罕见。
- 到时任务层收到的是“代码契约错误”，而不是“超时”。
- 追逐视角来源和导航来源也没有经过正常的停止路径释放。
- 这违背 AGENTS.md“明确阻塞和超时必须分别表达”的要求。

**建议：**
- 在 `tick` 开头检查任务期限。
- 期限已到时，走正常的停止接近路径，释放视角和导航来源。
- 然后进入已有的 `NEEDS_TASK_DECISION` 阶段，原因写成“任务时间上限”。外力恢复触及上限时已经这样处理（[`:1070`][task-decision]）。
- 补一条测试。

## 五、其他观察

1. **导航自带视角和战斗视角冲突时，胜负是隐式的。**
   - 普通步行时导航不带视角，所以没有冲突。
   - 跳上、台阶、跨隙等动作会自带视角，并要求视角对齐。这时两个 `TASK` 优先级的视角按“提交时间晚者胜”排序（[`arbiter_v1.py:28`][rank]）。
   - 导航在战斗视角之后生成，所以通常导航胜出，这也是安全的选择；提交时间完全相同时，改由意图编号决定胜负。
   - 建议在 D033 或架构文档里写明：导航动作要求的视角优先于追逐视角。再用明确规则实现，不依赖调用顺序。
2. **证据边界。**
   - 持续追击中，距离从 8.94 格降到 2.53 格，其中约 2 格是僵尸自己走近的。它证明了“目标横向移动时边转头边走”，但没有覆盖目标远离或走出视野的情况，而这正是 D033 要解决的失败类型。
   - C1-B 回归批次 `9fa68417` 没有公开。因此公开证据中 `NavigationSession` 一帧步行的实机样本，只有这一段平地追击。
3. **追击效率（不是缺陷，供后续设计参考）。**
   - 机器人基本沿 x≈0.5 直线前进，最后 8 帧里有 5 帧因为“转弯控速”松开按键（[`fixed_route.py:568`][corner]），速度降到约一半。
   - 目标状态要求到达时速度不超过 0.6 格/秒，全程只允许步行。
   - 对付僵尸，这样稳妥；但以后要做“最优战斗”时，需要一个战斗专用的接近配置，包括疾跑、带速度进入攻击距离、侧移等。

## 六、检查结果

在 `3a1fdf0` 检出目录中按 README 运行：

| 检查 | 结果 |
|---|---|
| `export_motion_navigation_standalone.py check-java` | JDK 21 |
| `export_motion_navigation_standalone.py verify --root .` | `STANDALONE_EXPORT_OK files=662` |
| `unittest discover -s tests/motion_nav` | 456 项通过 |
| C1 证据与运行测试 | 43 项通过 |
| Runtime、仲裁、战斗驱动测试 | 172 项通过 |
| B10–B12 运行与注入测试 | 58 项通过（跳过 1 项） |
| `tests.test_standalone_java_gates` | 3 项通过 |
| `public_runtime_evidence.py verify` | `PUBLIC_RUNTIME_EVIDENCE_OK archives=9` |
| 第九轮 `alternating_attack_failures_never_exhaust.py` | 交替失败后返回 `NO_PROGRESS_RETRY_EXHAUSTED` |
| 第十轮 `UnregisteredDamageTargetsFillBuffer.java` | 11／64／70 轮丢弃计数均为 0，70 次命中全部送达 |

## 七、对照总体设想

- **分工方向是对的。**
  - 战斗层决定看哪里，导航层负责身体怎样安全前进，仲裁器保证移动只在对应视角胜出时生效。
  - 以后 Jev 下达“追击、后撤、侧移”这类战术目标时，可以沿用同一条链：Jev 给目标，导航规划身体，战斗管视角和攻击。不需要另写一套战斗移动器。
- **下一步的短板在反应和效率，而不在正确性。**
  - 开局 0.4–1.4 秒静止、只允许步行、减速进入攻击距离，这三点决定了机器人在真实战斗里是否像一个“能打的 2P”。
  - 建议在加入战术层之前，先把以下两项做成正式指标，每批都统计：
    - “目标确定到首次移动的时间”；
    - “追击中平均接近速度”。

## 八、建议（按优先级）

1. **导航原因覆盖：** 没有路线的帧也写原因事件；评估只统计窗口内记录，并要求每个导航帧恰好一条；同步修正 D033 和验收第 9 节的表述。
2. **开局静止：** 查清诱发转向试验 28 帧不出路线的原因，加入“目标确定到首次移动”指标。
3. **任务上限：** 追逐到期时进入 `NEEDS_TASK_DECISION`，而不是抛出契约异常；补测试。
4. **视角冲突规则：** 写明并实现“导航动作要求的视角优先于追逐视角”。
5. **证据：** 补一个目标远离或横穿的持续追击场景；公开或注明 C1-B 回归批次。

## 九、复现脚本

目录：[`2026-09-26-b12-pursuit-repro/`](2026-09-26-b12-pursuit-repro/)。都在 `3a1fdf0` 检出目录的仓库根目录运行。

| 脚本 | 运行方式 | 结果 |
|---|---|---|
| `planning_frame_has_no_decision_event.py` | `PYTHONPATH=. python -B <脚本路径>` | 规划中的三帧：各有 1 条中性移动意图，`task_events=0` |
| `active_trial_reason_coverage.py` | `python -B <脚本路径> evidence/motion_navigation/representative-v1/archives/b12b-pass-20260926T074601049372Z-1e50bcb7.tar.gz` | 输出第二节表格中的数字 |
| `pursuit_task_limit_raises_contract_violation.py` | `PYTHONPATH=. python -B <脚本路径>` | `3a1fdf0` 抛出 `ContractViolation`；`211af42` 越过上限继续追 |

[base]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/tree/3a1fdf0845ba99e8d796ac7a9f056c39b6913478
[buffer-remove]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/3a1fdf0845ba99e8d796ac7a9f056c39b6913478/mc2p/backends/runtime_overlays/mc121_observation/ClientDamageEventBuffer.java#L54-L66
[pursuit]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/3a1fdf0845ba99e8d796ac7a9f056c39b6913478/mc2p/skills/moving_melee_driver.py#L389-L434
[pursuit-raise]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/3a1fdf0845ba99e8d796ac7a9f056c39b6913478/mc2p/skills/moving_melee_driver.py#L396-L400
[task-decision]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/3a1fdf0845ba99e8d796ac7a9f056c39b6913478/mc2p/skills/moving_melee_driver.py#L1070-L1072
[route-event]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/3a1fdf0845ba99e8d796ac7a9f056c39b6913478/mc2p/motion_nav/navigation_session.py#L1471-L1507
[early-return]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/3a1fdf0845ba99e8d796ac7a9f056c39b6913478/mc2p/motion_nav/navigation_session.py#L818-L832
[decision-events]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/3a1fdf0845ba99e8d796ac7a9f056c39b6913478/mc2p/motion_nav/navigation_session.py#L1369-L1383
[needs-info]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/3a1fdf0845ba99e8d796ac7a9f056c39b6913478/mc2p/motion_nav/navigation_session.py#L1117-L1120
[eval-reasons]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/3a1fdf0845ba99e8d796ac7a9f056c39b6913478/scripts/b12b_partial_combat_runtime.py#L271-L313
[d033-l17]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/3a1fdf0845ba99e8d796ac7a9f056c39b6913478/docs/motion_navigation/decisions/0033-sustain-combat-look-during-pursuit.md#L17
[d033-l30]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/3a1fdf0845ba99e8d796ac7a9f056c39b6913478/docs/motion_navigation/decisions/0033-sustain-combat-look-during-pursuit.md#L30
[acc-reasons]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/3a1fdf0845ba99e8d796ac7a9f056c39b6913478/docs/motion_navigation/acceptance/B12B-partial-observation-combat-motion.md#L161-L169
[rank]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/3a1fdf0845ba99e8d796ac7a9f056c39b6913478/mc2p/runtime/arbiter_v1.py#L28-L29
[corner]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/3a1fdf0845ba99e8d796ac7a9f056c39b6913478/mc2p/motion_nav/fixed_route.py#L568-L581
