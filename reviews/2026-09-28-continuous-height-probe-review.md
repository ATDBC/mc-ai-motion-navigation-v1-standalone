# 连续高度第二轮整改复审：六个缺口已关闭，探边的起点和收尾仍不安全

日期：2026-09-28
对象：`origin/main` 提交 [`8898cf9`][base]（fix: publish continuous height hardening）
性质：外部审查意见，复审 [第十七轮审查](2026-09-27-continuous-height-hardening-review.md) 的整改。不属于 `docs/motion_navigation/` 四类正式文档。

## 结论

1. **上一轮指出的六个缺口都已关闭，逐项复现通过（第一节）。**
   - `LandingEdgeProbe` 是正确的结构：探边有了类型化状态、所有者、期限，并绑定目标版本和落点格。
   - 输入失联成为终态；空中动作之后一次 A* 就能得到 Step 后备路线。
   - 落点门槛与文档一致；额度在接纳时复核，身体离开支撑即记账。
   - 连续小高差与连续整格下降的闭环结果没有退化。
2. **方向判断：正确。** 剩下的问题集中在一处：探边会直接操纵在边缘上的身体，但它的起点条件和结束方式还没有像空中动作那样“负责到安全状态”。
3. **新问题，按严重度：**
   - **P0 探边会把身体带到未规划的边缘上，结束时松开潜行导致坠落（第二节）。**
     - 探边不检查身体离落点有多远。只要整条路线只缺一个落点证据，就立即开始，并沿直线潜行走向落点旁的观察位，不走规划路线。
     - 途中如果遇到坑边，潜行会把身体挂在边上。但持续前进的输入仍保存在速度里，每 tick 约 0.12 格。
     - 探边超时，或同一任务改目标（追击中很常见）时，会话直接提交不带潜行的输入，保存的速度把身体带下坑边。
     - 在 1.21 计算器闭环中，两种情况都从 9 格深的坑边掉了下去。这次下落没有规划、没有额度、没有落点证据，正是落点门槛要防止的事。
   - **P1 落点证据在整条路线接纳时一次检查所有直接下落（第三节）。**
     - 路线中有两个以上的高落差时（阶梯状山坡），远处落点永远满足不了“4.5 格内”，整条路线无法接纳。
     - 探边会以远处那个落点为目标，结果和 P0 叠加：在第一道坎边超时失败，然后掉下第一道坎。
   - **P1（残留）首个地面段的证明失败仍逐条禁用重搜（第四节）。**
     - 入口状态不能用时（没有状态锚点、正在疾跑、被击退后速度超过 Walk 上限），每一列的证明都会以同样理由失败。
     - 宽 21 格、8 行半格台阶的场地，要跑 17 次 A*，然后报告 `no_route_within_complete_scope`。其实 Step 路线存在，这是错误的“无路”分类。
   - **P2 重规划上限可以绕过（第五节）。** 计数只对连续相同的键累加。两个原因交替出现时，16 帧发 8 次请求，仍在循环。
   - **P2 直接下落动作的“参照”用错了（第六节）。**
     - 验收文档把旧 Fabric 批次（16/18/21/24 tick）当作参照。
     - 按第 1 节定义的持续前进参照（同一计算器），1/2/3/5 格为 10/11/13/16 tick；动作执行 13/14/16/20 tick，分别多 3/3/3/4 tick，5 格超出门槛 1 tick。
   - **P2 小项：**
     - 旧的 `information_probe_movement` 只剩测试在用；
     - 三处证据判断仍以 `_reason` 分支；
     - 观察位固定取右侧角，右侧被挡就直接失败。

## 一、上一轮问题核实

输出见 [`outputs-8898cf9.txt`](2026-09-28-continuous-height-probe-review-repro/outputs-8898cf9.txt)。

| 上一轮问题 | 本轮结果 |
|---|---|
| P0 探边潜行保持不会解除 | **已关闭。** 同一任务改目标后，旧探边结束（`goal_revised`），新路线的 `strafe=1` 原样提交 |
| P1 `INPUT_LOST` 无限重规划 | **已关闭。** 直接失败，不发新请求 |
| P1 `NEEDS_REPLAN` 无上限 | **基本关闭。** 同一原因重试 2 次后返回 `replan_retry_exhausted`；交替原因仍可绕过，见第五节 |
| P1 空中动作后逐条禁用重搜 | **已关闭。** 宽 21 格、8 行半格：由 17 次 A* 并失败，变为 1 次 A* 并完成（8 个 Step）。首个地面段仍有残留，见第四节 |
| P1 落点门槛探边分支过宽 | **已关闭。** 9 格外只见上部的证据，潜行站在边缘也拒绝；超过 4.5 格的证据只接受探边到达边缘后、下部可见、绑定落点的 |
| P1 额度接纳时不复核 | **已关闭。** 余额为 0 时不再接纳 2 点下落，而是以 `damage_budget_changed_before_admission` 重新请求 |
| P1 `INPUT_LOST` 的下落不记账 | **已关闭。** 身体离地即按保守预计记账，之后的任何终态都不退回 |
| P2 直接下落用时 | 分段报告已按 D038 执行；参照口径见第六节 |

回归：
- 以下都与上一轮相同，没有退化：
  - 起点偏到格子角落；
  - 连续上、下半砖在延迟、随机迟到 100/100、朝向偏差 10°–90°、入口速度 0–3 格/秒下完成；
  - 合成的连续整格下降同样通过；
  - 控制耗时 P95 约 2 ms。
- 测试：`tests/motion_nav` 550 项全部通过。相关模块另 186 项中 1 项报错，原因是本机缺 loom 缓存，属于环境问题。

## 二、探边从远处开始、走直线、结束时松开潜行（P0）

**场景**（[`probe_far_landing.py`](2026-09-28-continuous-height-probe-review-repro/probe_far_landing.py)）：

- 完全已知的 L 形石头走道：先沿 +z 走 6 格，再沿 +x 走 6 格。走道尽头有一个 3 格直接下落。
- 走道内侧是深到 y=55 以下的坑，规划器会绕着走。
- 使用正式 Profile 集和仓库测试用的同步规划器，与 1.21 计算器闭环。计算器实现了原版潜行防坠的边缘裁剪。

| 帧 | 会话 | 身体 (x, y, z) | 输入 | x 方向速度 |
|---|---|---|---|---|
| 1 | `needs_information`，探边 `approaching` | (0.50, 64.00, 0.50) | 前进 + 右移 + 潜行 | 0 |
| 20 | 同上 | (1.30, 64.00, 3.13) | 同上 | +0.109 格/tick |
| 40 | 同上，身体被潜行挂在坑边 | (1.30, 64.00, 3.86) | 同上 | +0.119 格/tick |
| 41 | **`failed` / `landing_edge_probe_timed_out`** | (1.30, 64.00, 3.90) | **全部松开，包括潜行** | +0.119 格/tick |
| 43 | `failed` | (1.48, 63.92, 3.93) | 无 | **离地，开始下落** |
| 46 | `failed` | (1.58, 63.23, 3.94) | 无 | 继续下落，坑底在 y=55 以下 |

- 另一个变体：第 30 帧同一任务把目标改回起点（追击中目标移动就是这样）。
  - 探边以 `goal_revised` 结束，新路线第一帧是 `ground_mode_confirmation_pending`，提交的是空输入。
  - 身体同样掉进坑里。

**原因：**

1. **起点没有距离条件。** 只要接纳结果缺一个落点格，会话就创建 `LandingEdgeProbe`（[`navigation_session.py:1689`][probe-create]）。
   - `movement()` 直接朝“落点旁的观察位”移动（[`landing_edge_probe.py:184-244`][probe-move]）。
   - 上一轮的临时实现还有“水平距离 ≤ 1.75 格、朝向误差 ≤ 5°”的限制，这次删掉了。
2. **路径没有规划。** 移动只按直线方向量化成 8 个方向，并附带潜行；唯一的检查是“身体当前在已知支撑上”（[`landing_edge_probe.py:362-411`][probe-toward]）。
   - 途中的坑边靠原版潜行拦住。
   - 与地面齐平的熔岩、岩浆块、仙人掌、火等不会让身体掉下去的危险，完全没有检查。
3. **结束时没有安全收尾。**
   - 超时、目标修订、取消、路线替换都会立刻结束探边，下一帧由会话或新路线提交输入，通常不带潜行。
   - 原版的潜行裁剪只限制位移，不清零速度。所以身体被持续推向边缘时，保存的速度约 0.12 格/tick，一松开潜行就滑下去。

**修法：**

- **起点：** 探边只能在身体已经站在该次下落的起始支撑面上时开始。在此之前，按路线正常走过去（见第三节的修法）。
- **路径：** 探边只在起始支撑面内移动，观察位和入口位都要落在已知的无危险支撑上，并由计算器或支撑查询验证。
- **收尾：** 给 `LandingEdgeProbe` 加一个安全退出阶段。任何原因结束时，先保持潜行并松开方向键（或反向轻推），直到水平速度 ≤ 0.1 格/秒、身体盒与支撑的重叠不小于一个余量，再交出控制权。这与空中动作“负责到安全落地”是同一原则。
- **测试：** 补充以下会话级闭环测试：
  - 远处落点；
  - 探边中改目标；
  - 探边中取消；
  - 探边超时；
  - 以上每种情况都检查身体没有离开支撑。

## 三、整条路线接纳时检查所有直接下落（P1）

**场景**（[`two_drop_terrace.py`](2026-09-28-continuous-height-probe-review-repro/two_drop_terrace.py)）：

- 三层平台，两道 3 格坎。
- 给两个落点格都放上最有利的证据：起点观察、下部可见、距离按真实几何计算。
  - 第一道坎的落点格：距离 4.40，满足；
  - 第二道坎的落点格：距离 10.77，不满足。

| 帧 | 结果 |
|---|---|
| 1 | `landing_visual_evidence_missing`，缺 `(0, 58, 9)`，即第二道坎；探边以它为目标 |
| 20–40 | 身体被潜行挂在**第一道**坎边 (0.50, 64.00, 3.29) |
| 41 | 探边超时，会话失败，松开潜行 |
| 42–47 | 身体从第一道坎滑下，没有经过受控下降动作 |

**原因：** `_direct_drop_missing_visual_evidence` 在整条路线接纳时，遍历路线里所有超过一格的 `ControlledDrop`（[`route_admission.py:153-180`][gate-all]）。证据要求“4.5 格内、5 个观察序号内”，只能在靠近那道坎时取得，而整条路线又必须先被接纳，身体才会走过去。

**修法：**

- 落点证据改为在该次 `ControlledDrop` 动作开始前检查（动作级接纳，或者 `MotionRouteCoordinator` 为这个动作提交求解之前），不在整条路线接纳时检查。
- 路线前缀照常执行。身体走到坎前时，由动作级门槛决定：直接下落、探边，还是请求信息。
- 远处的落点只需要在规划时是已知空气，与现在的规划条件相同。

## 四、首个地面段的证明失败仍逐条禁用（P1，残留）

**场景**（[`later_run_research.py`](2026-09-28-continuous-height-probe-review-repro/later_run_research.py) 第二张表）：没有下落，只是入口状态不能用于证明。

| 场地 | 半格台阶行数 | 入口：无锚点 / 疾跑 / 超速 |
|---|---|---|
| 宽 7 | 4、8 | 8 次 A*，完成，全部 Step |
| 宽 21 | 4 | 10 次 A*，完成 |
| 宽 21 | 8 | **17 次 A*，`no_route_within_complete_scope`** |

**原因：**

- `verify_ground_traversal` 对疾跑、潜行、非站立、超速的入口，以及没有入口状态的情况，直接返回失败，与路线无关（[`ground_traversal.py:217-223`][verify-entry]，[`known_map_planner.py:2463-2480`][planner-entry]）。
- 规划器仍按“这条路线的证明边不行”处理，每轮只禁用一列，最多 16 轮，然后清空路线。

**什么时候发生：**
- 会话在没有状态锚点时规划：`execution_anchor` 返回 `None`，例如刚开始或残差无法解释时。
- 被击退后落地重规划，速度仍高于 Walk 上限。

**修法：**

- 证明失败的原因与路线无关时，把起始状态的 `first_ground_run` 直接设为 `False`，一次搜索得到 Step 后备路线。
- 16 轮用完不能报告成“无路”，应当退回同样的整体后备搜索，或者返回单独的有类型原因。

## 五、重规划上限可以被交替原因绕过（P2）

[`input_lost_replans.py`](2026-09-28-continuous-height-probe-review-repro/input_lost_replans.py)：

| 执行器每帧报告 | 16 帧内新请求 | 最终 |
|---|---|---|
| `INPUT_LOST` | 0 | `failed` |
| `NEEDS_REPLAN` / `ground_traversal_stalled` | 2 | `failed` / `replan_retry_exhausted` |
| `NEEDS_REPLAN`，`stalled` 与 `left_verified_envelope` 交替 | **8** | 仍在循环 |

**原因：** 计数只在“这一次的键等于上一次的键”时累加（[`navigation_session.py:1247-1268`][replan-key]），换一个原因就从 1 重新开始。每次目标修订也会清零，而追击时目标一直在修订。

**修法：**
- 按（起始支撑面，世界几何修订）保存每个原因的计数字典，同时设一个与原因无关的总上限。
- 目标修订时不清零，改为只在身体换到另一个支撑面、或几何修订变化时清零。

## 六、直接下落动作的参照口径（P2）

- 验收文档第 1 节把参照定义为“同一运动计算器、同一起点和终点”的持续前进（[`continuous-height-ground-movement.md:20`][acc-ref]）。
- 第 12.2 节却用旧 Fabric 批次的 16/18/21/24 tick 作为参照（[同文件第 250 行][acc-122]），整改计划第 7.1 节也一样。

[`drop_action_reference.py`](2026-09-28-continuous-height-probe-review-repro/drop_action_reference.py) 按第 1 节的定义计算：从探边入口位（伸出边缘 0.15 格）静止开始，持续前进，统计到第一次落地的 tick 数。

| 下落 | 持续前进参照 | Fabric 动作执行（12.2） | 差值 | 门槛 |
|---|---|---|---|---|
| 1 格（从中心起步） | 10 | 13 | +3 | ≤ +3 |
| 2 格 | 11 | 14 | +3 | ≤ +3 |
| 3 格 | 13 | 16 | +3 | ≤ +3 |
| 5 格 | 16 | 20 | **+4** | ≤ +3 |

- Fabric 的“确认落地”可能比计算器的第一次着地多一帧观察延迟，所以这里不下“未通过”的结论。
- 但文档“满足门槛”的说法依据的是错误的参照。按正确参照，结果恰好在门槛边上，5 格超出 1 tick。
- 建议用第 1 节的参照重算，并写明计时窗口的起点和终点。

## 七、小项（P2）

- `information_probe_movement` 仍在 `landing_edge_probe.py` 中，会话也导入了它，但正式代码已经不再调用，只有三个测试在测它。这些测试测的不是正式行为，建议删除函数和测试，改为测 `LandingEdgeProbe`。
- `observe()` 里仍有三处以 `self._reason is AdmissionReason.LANDING_VISUAL_EVIDENCE_MISSING` 决定证据如何处理（第 982、992、1006 行）。既然探边已有所有者，可以改为以 `self._edge_probe` 的状态为准。
- 观察位固定取右侧角（[`landing_edge_probe.py:316-336`][vantage]），右侧被挡时只能超时失败。先检查右侧角是否可站，不可站时换左侧，成本很低。

## 八、方向判断

**做对的：**

- 探边从“原因字符串 + 布尔位”改成了有类型、有期限、绑定目标和落点的对象，这正是上一轮建议的结构。
- 取证、横移、松开潜行、恢复视角、交出证据，分成明确的阶段。
- 空气更新只有在探边完成后才能授权下落；实体方块更新立即使候选失效。这个顺序是对的。
- 额度按承诺记账，接纳按当前余额复核。
- 计时分成取证和动作执行两段报告，没有把取证成本藏起来。

**需要补的：** 探边会在边缘直接操纵身体，应当与空中动作遵守同一条原则：从哪里开始、走哪条路、在任何原因下怎样安全结束，都由它自己负责。第二、三节的问题都出在这里，第三节还说明证据门槛放错了层级：它属于单次下落动作，不属于整条路线。

**建议的下一步：**

1. **P0：**
   - 探边只在下落起始支撑面上开始，并只在该支撑面内移动；
   - 增加安全退出阶段；
   - 补充远处落点、改目标、取消、超时四个闭环测试。
2. **P1：**
   - 落点证据改为动作级门槛；
   - 与路线无关的证明失败一次退到 Step 后备；
   - 循环用完不报“无路”。
3. **P2：** 重规划计数改为字典并加总上限；用正确参照重算下落用时；清理旧函数和 `_reason` 分支；观察位允许左右两侧。
4. 然后再跑完整矩阵。其中 Fabric 场景应增加两类：**起点不在落点旁的直接下落**，以及**两道以上坎的阶梯下坡**。现有 12 个代表场景都是“起点就在落点旁、只有一次下落”，恰好避开了第二、三节的问题。

## 九、复现

目录：[`2026-09-28-continuous-height-probe-review-repro/`](2026-09-28-continuous-height-probe-review-repro/)。在 `8898cf9` 检出目录的仓库根目录运行 `PYTHONPATH=. python -B <脚本路径>`，`height_transition_cost.py` 已放在同一目录。全部输出，包括前两轮脚本的重跑结果，见 `outputs-8898cf9.txt`。

| 脚本 | 结果 |
|---|---|
| `probe_far_landing.py` | 探边从 9 格外直线走向落点，挂在坑边；超时或改目标后松开潜行，掉进坑里 |
| `two_drop_terrace.py` | 两道坎的路线因第二道坎缺证据而无法接纳；探边挂在第一道坎边，超时后掉下 |
| `later_run_research.py` | 表一：空中动作后 1 次 A*（已修）；表二：入口状态不可用时，宽 21、8 行仍要 17 次 A* 并报告无路 |
| `input_lost_replans.py` | 失联直接失败；同一原因重试 2 次后失败；交替原因 16 帧 8 次请求 |
| `drop_action_reference.py` | 按验收第 1 节定义的直接下落持续前进参照 |
| `edge_hold_persists.py` | 探边中改目标，新路线的横移原样提交（已修） |

[base]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/tree/8898cf958cf31ad231fcb3953df240ce83bb6292
[probe-create]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/8898cf958cf31ad231fcb3953df240ce83bb6292/mc2p/motion_nav/navigation_session.py#L1685-L1702
[probe-move]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/8898cf958cf31ad231fcb3953df240ce83bb6292/mc2p/motion_nav/landing_edge_probe.py#L184-L244
[probe-toward]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/8898cf958cf31ad231fcb3953df240ce83bb6292/mc2p/motion_nav/landing_edge_probe.py#L362-L411
[gate-all]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/8898cf958cf31ad231fcb3953df240ce83bb6292/mc2p/motion_nav/route_admission.py#L153-L180
[verify-entry]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/8898cf958cf31ad231fcb3953df240ce83bb6292/mc2p/motion_nav/ground_traversal.py#L217-L223
[planner-entry]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/8898cf958cf31ad231fcb3953df240ce83bb6292/mc2p/motion_nav/known_map_planner.py#L2449-L2480
[replan-key]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/8898cf958cf31ad231fcb3953df240ce83bb6292/mc2p/motion_nav/navigation_session.py#L1247-L1268
[acc-ref]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/8898cf958cf31ad231fcb3953df240ce83bb6292/docs/motion_navigation/acceptance/continuous-height-ground-movement.md?plain=1#L20
[acc-122]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/8898cf958cf31ad231fcb3953df240ce83bb6292/docs/motion_navigation/acceptance/continuous-height-ground-movement.md?plain=1#L250
[vantage]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/8898cf958cf31ad231fcb3953df240ce83bb6292/mc2p/motion_nav/landing_edge_probe.py#L316-L336
