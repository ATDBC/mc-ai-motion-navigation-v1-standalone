# 连续高度移动审查：方向判断与阻塞问题

日期：2026-09-27
对象：`origin/main` 提交 [`59c45bb`][base]（feat: publish continuous height movement）
性质：外部审查意见，回应“审查本轮修正和高度衔接，并检查当前推进方向是否正确”。不属于 `docs/motion_navigation/` 四类正式文档。

## 结论

1. **方向是对的。**
   - 本轮基本按上一轮建议的四步落地：
     - 方向性入口窗口 `SegmentEntryWindow`；
     - 小高差并入 Walk，由 1.21 运动计算器验证整段；
     - 按整数 tick 计价；
     - 上跳和下落改为带速进入，走 B10 的“求解—验证—执行”链。
   - 伤害额度成为有类型的正式入参，额度变化使旧候选失效。
   - 制动候选的剪枝是可采纳下界的分支定界，并有穷举等价测试。
   - 文档如实写明完整矩阵未关闭。
   - 代表性 Fabric 12/12 和七项回归是真实进展。上坡小高差 27–28 tick，同一计算器“按住前进”参照为 22 tick，约 1.25 倍，停顿问题在这个场景里已经解决。
2. **但连续小高差离“能在真实游戏里用”还差一步：证明太脆，而执行只是回放这份证明。** 三个问题都会让导航会话直接进入 `FAILED`：
   - **起点不在格子正中心，就被拒绝（第二节）。**
     - 身体偏离起始格中心 5 厘米，含小高差的路线就被接纳拒绝，原因是 `ground_traversal_proof_missing`。
     - 根因：规划器按格子中心生成证明，接纳时却在路线前面补上身体当前位置；两条路线的点对不上，证明被丢掉。
     - Fabric 探针用传送把身体放在格子正中心，所以没有暴露。实际游戏中，上一段路线结束后身体几乎不会恰好在中心。
   - **混合路线没有证明（第三节）。** 小高差和一格下落出现在同一条路线里时，规划器返回需要证明、却没有证明的 Walk 边，接纳拒绝。
   - **在线执行是回放，不是闭环（第四节）。**
     - 控制器按“最近轨迹点”回放证明里的输入，偏离太远就判失败。架构文档写的是“每帧用完整计算器比较候选输入”“不是可以脱离观察盲目播放的固定输入序列”。
     - 闭环模拟，下两个半砖：
       - 证明终点离目标 0.334 格，完成半径 0.35，余量只有 0.016 格；
       - 每条命令晚 1 tick 就失败；
       - 每条命令有 20% 概率晚 1 tick 时，100 次只完成 41 次；
       - 朝向偏离路线 10° 就失败。
     - 上坡在延迟下是稳的（100/100），但朝向偏 20°、30°、60° 失败；入口速度和证明不一致时会卡死在原地，一直是 `RUNNING`。
3. **两项权限／安全语义没有到位（第五、六节）：**
   - **伤害额度只按单个动作检查，不累计。** 文档把它定义为“本任务允许的最大预计伤害”，但额度 2 点的请求会规划出两次 5 格下落，预计共 4 点。
   - **落点近距离观察门槛仍未实现，下落范围却已扩到 16 格。** D004、B09 验收和 B12 验收都把它写成“扩大下降能力之前”的前提。
4. **用时：** 连续整格下降 Fabric 实测 50–51 tick，同一计算器“按住前进”参照为 32 tick，约 1.6 倍，验收门槛是 1.3 倍。直接下落比参照多 6–7 tick，门槛是 3 tick。两边测量口径可能不同，需要在证据里给出分段用时再下结论。
5. **上一轮整改大部分已核实：**
   - 16 格范围泄漏降为 0；
   - “超出范围”单列并按等待上限失败；
   - 遮挡失败原因单列；
   - B09 探针恢复；
   - B07 用窄条件容忍一帧着地抖动。
   - 花盆只改对了一半：仍有 20 种盆栽因名字后缀被当成可透视。

**方向判断：** 架构方向正确，不需要改路线。问题出在第二步只做了一半：“计算器验证整段”实现成了一次离线推演，证明绑定精确的起点和路线点，执行时再原样回放。建议下一步先把证明变成走廊、把执行改成闭环，并把起点和混合路线的问题修掉，再跑完整矩阵、扩大形状和速度带。按当前实现直接跑验收文档第 4 节的矩阵，下坡预计会大面积失败。

## 一、上一轮问题核实

| 上一轮问题 | 本轮状态 | 依据 |
|---|---|---|
| 空气露出区域越过 16 格 | **已修。** 空气也按同一 `in_range` 裁剪；60 个随机世界中，完整方块格上的视觉空气由 57 次降为 0 | 重跑 [`air_block_consistency.cpp`](2026-09-27-partial-air-repro/air_block_consistency.cpp) |
| 远处缺失格一直转头 | **已修。** 新增 `out_of_range` 状态；`out_of_range`、`occluded`、`unavailable` 不再转头，按 40 帧上限分别以 `information_out_of_range`、`information_occluded_requires_observation_position`、`information_unavailable_timeout` 失败。`outside_view` 仍会转头，这是设计本意 | 重跑 [`far_cell_information_wait.py`](2026-09-27-partial-air-repro/far_cell_information_wait.py)，读 `navigation_session.py:1653-1699` |
| 极小露出面积 | **已修。** `AIR_VISIBLE_AREA=1e-5` | `geometry.hpp:18` |
| B09 探针缺信息 | **已修。** 预观察改用统一的缺失格观察，并记录状态 | 读 `air_motion_runtime.py` |
| B07 连续路线 `ordinary_ground_state_lost` | **已处理。** 根因是一帧着地标志抖动；修法只在已知完整支撑 ≥80%、站立、同高、无上行速度时容忍一帧。架构文档也写明它“只作为观测抗抖，不能代替物理验证”，边界合适 | 读 `fixed_route.py`、`step_transition_runtime.py` |
| 冰块、花盆规则 | **一半。** `packed_ice`、`blue_ice` 已删；`potted_` 前缀已删。但 [`SurfaceVisibilityRules.java:58-64`][pots] 的后缀 `_sapling`、`_tulip`、`_mushroom`、`_roots`、`_fungus`、`_bush` 仍会命中 20 种盆栽：7 种树苗、4 种郁金香、2 种蘑菇、2 种菌索、2 种菌、枯萎的灌木和 2 种杜鹃花丛。测试只查了 `potted_dandelion` | 读代码 |

检查：`tests/motion_nav` 523 项全部通过；相关模块另 182 项中 1 项报错，原因是本机缺 loom 缓存，属于环境问题。

## 二、起点偏离格子中心，小高差路线就被拒绝（P0）

**现象：** [`off_center_start.py`](2026-09-27-continuous-height-repro/off_center_start.py) 使用与仓库测试相同的连续上两个半砖直道，调用真实规划器 `plan_known_surface_snapshot` 和 `RouteAdmitter`，只改变身体起点：

| 起点相对格子中心 | 规划 | 证明 | 接纳 |
|---|---|---|---|
| (0, 0) | complete | 1 | accepted |
| (0.05, 0) | complete | 1 | **rejected `ground_traversal_proof_missing`** |
| (0.12, −0.09) | complete | 1 | **rejected `ground_traversal_proof_missing`** |

**原因：**

- 规划器用路径上各格子中心的坐标构造证明路线（[`known_map_planner.py:2301-2303`][planner-proof]），起始物理状态却是身体的真实状态。
- 接纳时，只要身体不在起始格中心，就在 Walk 路线前面补上身体当前位置（[`route_admission.py:503-509`][admit-prepend]）。
- 随后按“路线点完全相等”查找证明（[`route_admission.py:517-520`][admit-proof-match]），找不到，于是拒绝（[`route_admission.py:661-669`][admit-reject]）。
- 会话把这个拒绝直接映射为 `FAILED`（[`navigation_session.py:1453-1456`][session-fail]）。

**影响：**

- 真实游戏中，身体停在上一段路线的完成半径内（0.25–0.35 格），几乎不会恰好在中心。
- 追击时身体更不会在中心，所以带小高差的地形上，每次重规划都会失败。
- Fabric 探针用 `(sx + .5, sy + 1, sz + .5)` 作为起点，并传送到位，正好避开了这个问题。

**修法：**

- 证明路线和接纳路线由同一个函数生成，包括“身体到起始格”的连接段。
- 或者，接纳按节点序列和依赖匹配证明，不按浮点坐标相等匹配。
- 增加回归测试：起点随机偏移 ±0.2 格、朝向随机。

## 三、混合路线产出没有证明的边（P0）

**现象：** [`mixed_route_fallback.py`](2026-09-27-continuous-height-repro/mixed_route_fallback.py)，7 格宽直道，真实规划器，带真实起始物理状态：

| 路线 | 规划结果 | 需证明的 Walk 边 | 证明数 | 接纳 |
|---|---|---|---|---|
| 连续上两个半砖（对照） | 5 条 Walk，沿中线 | 2 | 1 | 接纳 |
| 上两个半砖，再下一格 | 5 条 Walk、1 条 ControlledDrop、1 条 Walk；**绕到 x=−1 的旁道走小高差，再横移回来** | 2（在旁道上） | **0** | **拒绝 `ground_traversal_proof_missing`** |

**原因**（[`known_map_planner.py:2286-2343`][planner-proof]）：

1. 只有“整条路线全是 Walk”时才尝试证明（`can_verify_one_ground_run`）。路线里有下落时，直接跳过证明。
2. 跳过或证明失败后，只禁用这一次搜索用到的那几条需证明边，然后重新搜索一次，不再检查新路线。相邻车道上同样需要证明的边没有被禁用，于是新路线换到旁道，仍然需要证明，却没有证明。
3. 证明成功的分支里，`alternative` 也可能带未证明的边，同样没有检查。

**修法：**

- **短期：** 禁用后循环重搜，直到路线中不再有未证明的需证明边，或者无路可走。循环次数有上限。
- **正式：** 按“连续地面段”分别证明。第一段从身体的真实状态开始；后续每段从前一动作的出口窗口开始，证明覆盖整个窗口，而不是一个点。这正是 `SegmentEntryWindow` 要解决的问题。
- 增加回归测试：小高差与 JumpUp、JumpGap、ControlledDrop 前后相接。

## 四、在线执行：回放而不是闭环（P0）

### 方法

[`traversal_robustness.py`](2026-09-27-continuous-height-repro/traversal_robustness.py) 使用仓库测试自带的场景 `traversal_fixture`：平地、下半砖、整方块，连续两个半格。

- 执行链路都是仓库原件：`verify_ground_traversal` 生成证明，`FixedRouteController` 执行，`project_movement_command` 投影按键，`physics_1_21.step` 推进。
- 只改变控制器外部的条件：
  - 输入延迟：每条命令都晚 1 tick；或每条命令以 20% 概率晚 1 tick（100 个冻结种子）。命令迟到的那一 tick 保持上一组按键，与仓库自己的晚释放测试一致。
  - 朝向：身体全程朝向偏离路线方向 θ。战斗注视正是这样工作的，见下文“朝向归属”。
  - 入口速度：实际速度与生成证明时的速度不同。

### 结果

| 条件 | 上两个半砖 | 下两个半砖 |
|---|---|---|
| 证明终点离目标 | 0.064 格（余量 0.286） | **0.334 格（余量 0.016）** |
| 正常 | 16 tick 完成 | 21 tick 完成 |
| 每条命令晚 1 tick | 18 tick 完成 | **失败**：`ground_traversal_terminal_mismatch` |
| 20% 命令晚 1 tick | 100/100 | **41/100**；其余为终点不符或卡住超时 |
| 朝向偏 10° | 完成 | **失败**：终点不符 |
| 朝向偏 20°、30°、60° | **失败**：`left_verified_envelope` | **失败** |
| 朝向偏 45°、90° | 完成 | 45° 失败，90° 完成 |
| 证明按 0 格/秒，实际 1 格/秒 | **卡住**：停在原地，输入为空，状态一直是 `RUNNING` | **失败**：终点不符 |
| 证明按 2 格/秒，实际 0 格/秒 | 完成 | **卡住** |

验收文档第 4 节要求每类路线在“20% 命令晚 1 tick”下至少跑 100 次（[`continuous-height-ground-movement.md:59-66`][acc-gates]）；第 3 节要求整格动作在“每条命令晚 1 tick”下也要完成。下坡小高差按当前实现过不了这两项。

### 原因

1. **证明的终点本身就贴着边界。**
   - 证明用的推演策略是：朝下一个路线点前进；进入终点 0.35 格内并着地后松键，靠惯性滑行到速度 ≤ 0.10（[`ground_traversal.py:246-282`][trav-verify]）。
   - 下坡到达终点时速度更高，滑过终点 0.33 格才停下。
   - 验证不检查最终位置是否仍在完成范围内，也不留余量；执行器的完成半径是 0.35（[`fixed_route.py:487-530`][trav-terminal]）。于是正常条件只多出 0.016 格，晚 1 tick 就超出。
2. **执行是回放，没有闭环。**
   - 控制器在 `[当前序号−1, 当前序号+4)` 范围内找离身体最近的轨迹点，然后回放该点记录的输入（[`fixed_route.py:440-464`][trav-track]）。
   - 偏差超过 0.45 格或速度误差超过 2 格/秒，就判 `UNSUPPORTED`；这与 B10 的“有限修正，超出范围才重新求解”不同。
   - 序号推进到末尾时，只要不满足完成条件，就立即判终点不符，不给制动或修正机会。
   - 架构文档写的是：接近已验证的高度变化时，由控制器用完整计算器比较本帧候选输入，每个选中的输入都必须让短期预测留在路线走廊内；`GroundTraversalPlan`“不是一条可以脱离观察盲目播放的固定输入序列”（[`continuous-height-ground-movement-v1.md:117-121`][arch-online]）。当前实现正是这种回放。
3. **没有停滞检测。** 如果最近的轨迹点正好对应一个空输入，身体停下后最近点不会再变，控制器会一直回放空输入并报告 `RUNNING`。
4. **朝向。**
   - 证明按路线方向推演，执行时再把世界方向换算成当前朝向下的 8 方向按键，阈值 0.35（[`fixed_route.py:405-426`][trav-move]）。
   - 偏 45° 或 90° 时恰好对上一个按键组合；偏 20°、30°、60° 时实际方向差 15°–20°，身体逐渐偏出走廊。
   - 同时，[`navigation_session.py:416-431`][route-look] 规定 Walk 类路段不要求路线朝向，战斗注视可以一直保持朝向目标。所以在坡道上追击时，这正是会出现的情况；C1-B 和 B12-B 的回归都在平地上，没有覆盖。
5. **晚 1 tick 生效窗口没有证明。** B10 架构要求执行窗口中的每一个允许开始 tick 都有完整证明（[`B10-motion-solving-continuous-execution-v1.md:113`][b10-late]）。地面段证明只从一个起点推演一次；仓库测试也只把第一次松键推迟一次，且只测上坡。
6. **失败没有降级路径。** 执行器的 `UNSUPPORTED` 在会话中直接映射为 `FAILED`（[`navigation_session.py:1499-1510`][session-unsupported]），不会从当前位置重规划。

### 修法

按架构文档原本的设计完成第二步，不需要新框架：

1. **证明提供走廊，而不是答案。**
   - `GroundTraversalPlan` 保留支撑面序列、走廊、依赖格和预计 tick 数。
   - 推演策略改为按制动距离提前减速，停在终点附近，不靠惯性滑过终点。
   - 验证要求终点落在完成范围内，并留出至少覆盖“晚 1 tick”分支的余量；再补做晚 1 tick 分支的推演，满足 B10 的窗口要求。
2. **执行改为逐帧闭环。**
   - 每帧在当前朝向下枚举 9 种移动按键组合（沿用证明中的疾跑和跳跃），用计算器前推几 tick。
   - 选出留在走廊内、沿路线推进、终点可制动的候选。证明里的输入只作为同分时的偏好。
   - 普通路线已经有“九种按键 + 计算器 + 分支定界”的机制，本轮的剪枝也能直接复用。
   - 最后一个高度变化之后的平地部分交还普通 Walk 的制动逻辑。
3. **加停滞检测：** 连续 N tick 没有沿路线推进，且输入为空，判定为停滞。
4. **偏出走廊时降级：** 从当前状态重规划或重新证明，而不是让整个会话 `FAILED`。
5. **明确朝向归属：** 有高度变化的 Walk 段要么声明需要路线朝向，要么由闭环控制器在任意朝向下选键。建议后者，因为追击时需要一直看着目标。
6. **不要靠放宽 0.45 格、2 格/秒或 0.35 格来通过。** 验收文档也明确禁止用扩大窗口的办法过关。

## 五、伤害额度按动作检查，没有按任务累计（P1）

**现象：** [`damage_budget_accumulation.py`](2026-09-27-continuous-height-repro/damage_budget_accumulation.py) 构造一条直道：平台顶面 74，下落 5 格到 69，走 1 格，再下落 5 格到 64。每次 5 格下落的保守预计伤害为 `ceil(5 − 3) = 2` 点。

| 额度 | 规划结果 | 下落 | 各次预计伤害之和 |
|---|---|---|---|
| 0 | unsupported | — | — |
| **2** | **complete** | 74→69、69→64 | **4** |
| 4 | complete | 同上 | 4 |

**原因：**

- 规划器逐边比较“本次下落的预计伤害 ≤ 额度”（[`known_map_planner.py:1670-1682`][budget-planner]）。
- 接纳也是逐个候选调用 `TaskDamageBudget.allows()`（[`motion_risk.py:36-54`][budget-code]），只和当前生命比较。
- 代码中没有“已消耗”或“剩余额度”。

**与文档的差异：** 架构文档写的是“本任务允许的最大预计伤害点数”（[`continuous-height-ground-movement-v1.md:172`][budget-doc]），类型名也是 `TaskDamageBudget`。当前行为实际是“单次动作伤害上限”。

**修法**（二选一，并写入决定记录）：

- **按任务累计：**
  - 规划时，路线中所有动作的预计伤害之和不超过额度；
  - 会话记录本任务已经实际损失的生命，每次接纳按剩余额度检查；
  - 目标更新时是否重置额度，由上层显式决定。
- **按动作上限：** 改名，例如改为 `PerMotionDamageLimit`，文档同步改为“单次动作”，另外增加任务级累计。

以后由性格或局势调整额度时，上层需要的是任务级语义。

## 六、落点近距离观察门槛仍未实现（P1）

- 以下几处都把“受控下降在离边前要求近距离、足够新的落点观察”列为扩大下降能力的前提：
  - D004（[`0004-lazy-positive-air-confirmation.md:61`][d004]）；
  - B09 验收（[`B09-parameterized-air-transitions.md:77`][b09-acc]）；
  - B12 验收（[`B12-surface-depth-remediation.md:259`][b12-acc] 和 280 行）；
  - D035 第 121 行。
  原因是：从高处只看得到落点格的上部，低矮的熔岩或篝火可能藏在格子底部。
- 本轮已把下落从一格扩到 16 格，并允许带伤害下落，但接纳链路中没有这一检查（全仓搜索无对应实现）。
- **修法：**
  - 在 `ControlledDrop` 的接纳中，对 1 格以上的下落，或统一对所有下落，检查落点格及其下方支撑是否有足够新的近距离观察；
  - 不满足时返回缺信息，或请求探边观察；
  - 验收文档第 6 节的“未知落点”反例应覆盖“只看到上部”的情况。

## 七、用时与参照（P2）

[`probe_hold_forward_baseline.py`](2026-09-27-continuous-height-repro/probe_hold_forward_baseline.py) 用同一计算器和与探针相同的一格宽悬空支撑，只按住前进键，在终点按同一速度要求制动：

| 场景 | Fabric 实测（验收文档第 8 节） | 按住前进参照 | 比值或差值 | 门槛 |
|---|---|---|---|---|
| 连续小高差（上四个半格） | 27–28 | 22 | 约 1.25 倍 | 1.3 倍 |
| 连续整格下降四级 | 50–51 | 32 | **约 1.6 倍** | 1.3 倍 |
| 直接下落 1、2、3、5 格 | 16、18、21、24 | 10、12、15、17 | **多 6–7 tick** | 多 3 tick |

**说明：**

- 两边口径可能不同：直接下落的门槛从“首次前进输入到确认落地”计时，Fabric 数字可能包含落地后的制动；参照是模拟，Fabric 有真实延迟。
- 所以这里不下“未达标”的结论，但整格下降的 1.6 倍已经明显超出。
- 建议在证据中给出分段用时：首个输入、离边、落地确认、下一段开始、制动完成。这样才能看出时间耗在求解等待、入口调整还是落地确认上。

## 八、证据覆盖

代表性 Fabric 批次证明主链能在真实游戏里工作，但它恰好避开了第二至第四节的问题：

- **起点：** 全部传送到格子正中心（第二节）。
- **下坡小高差：** 没有覆盖。“连续小高差”只有上坡；“整格下降”走的是 `ControlledDrop`，不经过地面段证明。闭环模拟中最脆弱的正是下坡小高差（第四节）。
- **混合路线：** 没有覆盖（第三节）。
- **坡道上战斗：** 没有覆盖。C1-B 和 B12-B 回归都在平地上（第四节“朝向”）。
- **延迟：** 只有自然延迟，没有注入延迟。

建议补一个便宜的组件闭环矩阵，覆盖起点偏移、下坡、混合、朝向偏移和注入延迟，先在模拟里通过，再上 Fabric。

## 九、方向判断

**做对的：**

- 继续以运动计算器作为唯一的物理依据，没有另起一套轨迹模型。
- 入口窗口有方向、有速度范围，交接不再只是“停在半径内”。
- 代价改为整数 tick，规划不再为了避开半砖而绕路。
- 带速上跳和下落复用 B10 的求解、验证和回执链，没有复制一套执行器。
- 伤害额度是有类型的入参，与请求修订绑定。
- 保留静止入口作为后备。
- 文档如实写明完整矩阵未关闭。

**偏离的：**

- 第二步的“验证整段”变成了“一次离线推演 + 回放”：
  - 证明绑定精确的起点坐标和路线点，执行不闭环，失败不降级。
  - 三者叠加，在理想条件下表现很好，日常扰动下就会失败，而且是让会话 `FAILED`。
- 任务伤害额度的语义与文档不符。
- 落点观察这个安全前提被跳过了。

**下一步建议：** 不要先扩大能力或跑完整 Fabric 矩阵。先做以下几件，再按验收文档跑矩阵：

1. **P0**：统一证明路线与接纳路线的生成；按节点匹配证明；起点随机偏移测试。
2. **P0**：混合路线按地面段分别证明，禁用后循环重搜。
3. **P0**：证明改为走廊，终点留余量，补晚 1 tick 分支；执行改为逐帧闭环；加停滞检测；偏出走廊时重规划。
4. **P1**：确定伤害额度语义，任务级累计。
5. **P1**：受控下降的落点近距离观察门槛。
6. **P2**：分段用时证据；整格下降用时。
7. **P2**：盆栽后缀。

这些都在现有模块内部完成，不需要新框架。其中第 3 项的逐帧候选选择可以直接复用普通路线的九键评估和本轮的分支定界剪枝。

## 十、复现

目录：[`2026-09-27-continuous-height-repro/`](2026-09-27-continuous-height-repro/)。在 `59c45bb` 检出目录的仓库根目录运行 `PYTHONPATH=. python -B <脚本路径>`；除 `traversal_robustness.py` 外，其余脚本需要与 `height_transition_cost.py` 放在同一目录。

| 脚本 | 结果 |
|---|---|
| `off_center_start.py` | 起点偏 5 厘米，接纳即拒绝 `ground_traversal_proof_missing` |
| `mixed_route_fallback.py` | 混合路线绕到旁道，0 份证明，接纳拒绝；对照路线 1 份证明，接纳通过 |
| `traversal_robustness.py` | 第四节的表格（输出另存为 `traversal_robustness.out`） |
| `damage_budget_accumulation.py` | 额度 2 点，规划出预计共 4 点的两次下落 |
| `probe_hold_forward_baseline.py` | 第七节的参照 tick |
| `height_transition_cost.py` | 上一轮的辅助脚本；本副本把已知空气扩到 y=79，以容纳更高的场景。它的主程序仍走旧的图接口，不经过本轮的证明和后台协调链，所以本轮不使用它的主程序结果 |

[base]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/tree/59c45bbc95dd320b550ba3bdfc028d4db407ce6b
[pots]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface/SurfaceVisibilityRules.java#L58-L64
[planner-proof]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/mc2p/motion_nav/known_map_planner.py#L2286-L2343
[admit-prepend]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/mc2p/motion_nav/route_admission.py#L503-L509
[admit-proof-match]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/mc2p/motion_nav/route_admission.py#L517-L520
[admit-reject]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/mc2p/motion_nav/route_admission.py#L661-L669
[session-fail]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/mc2p/motion_nav/navigation_session.py#L1440-L1456
[session-unsupported]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/mc2p/motion_nav/navigation_session.py#L1499-L1510
[trav-verify]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/mc2p/motion_nav/ground_traversal.py#L239-L282
[trav-move]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/mc2p/motion_nav/fixed_route.py#L405-L426
[trav-track]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/mc2p/motion_nav/fixed_route.py#L440-L464
[trav-terminal]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/mc2p/motion_nav/fixed_route.py#L487-L530
[route-look]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/mc2p/motion_nav/navigation_session.py#L416-L431
[arch-online]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/docs/motion_navigation/architecture/continuous-height-ground-movement-v1.md?plain=1#L117-L121
[b10-late]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/docs/motion_navigation/architecture/B10-motion-solving-continuous-execution-v1.md?plain=1#L113
[acc-gates]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/docs/motion_navigation/acceptance/continuous-height-ground-movement.md?plain=1#L59-L66
[budget-planner]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/mc2p/motion_nav/known_map_planner.py#L1670-L1682
[budget-code]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/mc2p/motion_nav/motion_risk.py#L36-L54
[budget-doc]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/docs/motion_navigation/architecture/continuous-height-ground-movement-v1.md?plain=1#L172
[d004]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/docs/motion_navigation/decisions/0004-lazy-positive-air-confirmation.md?plain=1#L61
[b09-acc]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/docs/motion_navigation/acceptance/B09-parameterized-air-transitions.md?plain=1#L77
[b12-acc]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/59c45bbc95dd320b550ba3bdfc028d4db407ce6b/docs/motion_navigation/acceptance/B12-surface-depth-remediation.md?plain=1#L259
