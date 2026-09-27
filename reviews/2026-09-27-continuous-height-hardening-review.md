# 连续高度整改复审：上一轮问题已关闭，新代码带来六处缺口

日期：2026-09-27
对象：`origin/main` 提交 [`5fa2f33`][base]（fix: harden surface visibility and continuous height movement）
性质：外部审查意见，复审上一轮 [连续高度移动审查](2026-09-27-continuous-height-review.md) 的整改。不属于 `docs/motion_navigation/` 四类正式文档。

## 结论

1. **上一轮的三个 P0、两个 P1 和花盆问题都已关闭，修法对路。**
   - 用上一轮的复现脚本逐项重跑，结果与文档相符。
   - 闭环逐帧选键也经受住了本轮加做的检查：
     - 起点偏到格子角落（±0.45, ±0.45）；
     - 合成的连续整格下降，在延迟、朝向偏差和起点偏移下都能完成；
     - 控制耗时 P95 约 2 ms，每帧中位 7 次计算器步进，远低于 8 ms 门槛。
   - 四级下降由 51 tick 降到 33 tick，参照为 32 tick。
   - D037 把“证明给走廊、在线闭环、按节点绑定、任务累计额度”写成了正式决定，边界清楚。
2. **方向判断：正确，可以继续。**
   - 剩下的问题都不在总体结构上。
   - 它们集中在本轮新加的“生命周期和记账”代码里：探边、重规划、额度和后续地面段。
3. **新代码带来六处缺口，按严重度：**
   - **P0 探边后的潜行保持不会解除（第二节）。** 探边让会话进入“只提交潜行”的保持状态，只有提交了已验证动作命令才解除。同一任务改目标后，新路线是普通 Walk，路线要横移，会话却一直只提交潜行，身体不动。追击中目标改变是常态。
   - **P1 重规划没有上限（第三节）。**
     - `INPUT_LOST` 和 `NEEDS_REPLAN` 都改成了“从当前位置重规划”，没有次数上限。同样的失联或停滞在身体和世界都不变时，每两帧发一次新请求，永远不进入终态。
     - 验收文档要求输入失联后“返回对应终态”；架构文档要求同一原因、同一身体状态、同一世界修订下的重新验证次数有上限。
   - **P1 空中动作之后的小高差会让规划失败（第四节）。**
     - 后续地面段的证明边，每轮只禁用当前路线上的那几条，然后整次重搜，最多 16 轮。
     - 21 格宽的场地，下落后接 8 行半格台阶，16 轮用完，规划返回 `unsupported`，尽管 Step 后备路线存在。
     - 即使没有用完，也要 6–14 次整次 A*，耗时是无下落时的 5–13 倍。
   - **P1 落点门槛比文档宽（第五节）。**
     - 四份文档都写“只接受 4.5 格内、下部可见的证据”。代码另有一个探边分支：只要身体潜行站在边缘，任何 5 个观察序号内的视觉空气证据都算数，不看“下部可见”，也不看观察距离。
     - 5 格及以上的下落，边缘到落点格的距离恒大于 4.5，只能走这个分支。所以 5 格实测“确认落点下部后下落”的说法，代码并没有检查。
   - **P1 额度记账有两个缺口（第六节）。**
     - 请求发出时快照剩余额度，接纳时不复核。2 点额度的任务，在一次 2 点下落完成后，仍接纳了另一次 2 点下落，共 4 点。
     - 以 `INPUT_LOST` 结束的下落不记账，而 `INPUT_LOST` 现在又会继续重规划。
   - **P2 直接下落用时退化（第七节）。** 2、3、5 格下落由 18、21、24 tick 变为 34、36、39 tick；参照为 12、15、17 tick，门槛是最多多 3 tick。这是落点门槛的代价，文档没有说明。
4. **结构上的共同原因：** 探边这个动作是用 `self._reason is AdmissionReason.LANDING_VISUAL_EVIDENCE_MISSING` 加一个布尔保持位驱动的，它没有自己的所有者和生命周期。AGENTS.md 明确要求不要用 reason 字符串和散落布尔开关决定权限和生命周期。第二节的问题就是直接后果。

## 一、上一轮问题核实

所有数字都来自在 `5fa2f33` 上重跑的脚本，输出见 [`outputs-5fa2f33.txt`](2026-09-27-continuous-height-hardening-repro/outputs-5fa2f33.txt)。

| 上一轮问题 | 本轮结果 |
|---|---|
| 起点偏 5 厘米被拒绝 | **已修。** 偏 0、0.05、0.12 格都能接纳。本轮加测 0.3 格和格子角落（±0.45, ±0.45），接纳后闭环 30–33 tick 完成 |
| 混合路线没有证明 | **已修。** 小高差后接一格下降：保留前段证明，不再绕到旁道，接纳通过 |
| 回放执行 | **已修。** 上、下两个半砖：<br>• 每条命令晚 1 tick：完成；<br>• 20% 命令晚 1 tick：上、下各 100/100；<br>• 朝向偏 10°–90°：全部完成；<br>• 入口速度 0–3 格/秒与证明不一致：全部完成 |
| 停滞卡死 | **已修。** 20 帧无进展返回 `ground_traversal_stalled`（但重规划没有上限，见第三节） |
| 伤害额度不累计 | **规划内已修。** 搜索使用非支配的（节点，剩余资源）标签，额度 2 点时拒绝两次各 2 点的下落，4 点时通过。会话层记账还有缺口，见第六节 |
| 落点观察门槛缺失 | **已接入**，但比文档宽，见第五节 |
| 20 种盆栽可透视 | **已修。** `potted_` 前缀不再参与植物后缀匹配 |
| 连续整格下降用时 1.6 倍 | **已修。** 同方向两级以上一格下降合成一段地面证明，Fabric 33 tick，参照 32 tick |

本轮另外检查了两项，都没有问题：

- **合成的连续整格下降**（[`stair_descent_robustness.py`](2026-09-27-continuous-height-hardening-repro/stair_descent_robustness.py)）：在以下条件下都完成，用时 39–41 tick：
  - 正常；
  - 每条命令晚 1 tick；
  - 20% 命令晚 1 tick（100/100）；
  - 朝向偏 30°、60°；
  - 起点偏移 (0.3, −0.3)。
- **控制耗时**（[`traversal_control_cost.py`](2026-09-27-continuous-height-hardening-repro/traversal_control_cost.py)）：P95 1.8–2.2 ms，最大 3.4 ms；每帧中位 7 次、最多 10 次计算器步进。

检查：
- `tests/motion_nav` 540 项全部通过。
- 相关模块另 185 项中 1 项报错，原因是本机缺 loom 缓存，属于环境问题。

## 二、探边后的潜行保持不会解除（P0）

**现象**（[`edge_hold_persists.py`](2026-09-27-continuous-height-hardening-repro/edge_hold_persists.py)，正式 Profile 集和仓库测试用的同步规划器）：

1. 任务目标在平台下方，只能直接下落 4 格，额度 1 点。传感器报告落点格被平台遮挡，会话返回 `landing_visual_evidence_missing`，开始潜行探边：
   - 提交 `forward=1, sneak=True`；
   - `_information_edge_hold=True`。
2. 同一任务把目标改到平台另一端，新路线是普通 Walk。

| 帧 | 会话状态 | 路线要的输入 | 实际提交 |
|---|---|---|---|
| 1–3 | `needs_information` | — | 前进 + 潜行（探边） |
| 4–9 | `executing` / `tracking_fixed_route` | `strafe=1` | **只有潜行，不移动** |

**原因：**

- 保持位只在“提交了已验证动作命令”时解除（[`navigation_session.py:1211-1216`][hold-clear]）。
- 改目标、重规划、新路线首段不是受控下降、会话取消，都不会清除它。
- 保持期间，路线决定的输入会被整体替换为 `MovementV1(sneak=True)`。

**后果：**

- 普通 Walk 会因为没有进展而被判停滞，然后按第三节无限重规划；保持位仍在，身体永远不动。
- 以下情况都会触发：
  - 追击中目标位置改变；
  - 探边后发现落点有危险，改走别的路；
  - 探边后停下的位置不满足下降入口，新路线先接一段 Walk。

**修法：** 把探边做成有类型、有所有者的信息获取动作，例如与 `ActionRouteExecutor` 并列的 `EdgeProbe`：

- 状态至少包括“前进中、已到边、证据已取得、放弃”。
- 以下任一情况出现时，由这个所有者结束探边并交出控制权：
  - 证据成立；
  - 请求或目标修订变化；
  - 新路线首段不是对应的受控下降；
  - 会话取消；
  - 超时。
- 不再用 `_reason` 判断是否进入探边。

## 三、重规划没有上限（P1）

**现象**（[`input_lost_replans.py`](2026-09-27-continuous-height-hardening-repro/input_lost_replans.py)）：让执行器在身体和世界都不变的情况下，每帧报告同一状态。

| 执行器报告 | 12 帧内新发的规划请求 | 12 帧后的状态 |
|---|---|---|
| `INPUT_LOST`（`input_application_unconfirmed`） | 6 | `snapshotting`，仍在循环 |
| `NEEDS_REPLAN`（`ground_traversal_stalled`） | 6 | `snapshotting`，仍在循环 |

**原因：**

- [`navigation_session.py:1200-1207`][replan] 对这两种状态都调用 `_restart_request_from_current`。
- `_apply_decision_state` 把 `INPUT_LOST` 从 `FAILED` 改成了 `PLANNING`（[`navigation_session.py:1680`][input-lost-map]）。
- 全程没有计数或上限。

**与文档的冲突：**

- 验收文档要求“取消和输入失联先安全落地，再返回对应终态”（[`continuous-height-ground-movement.md:101`][acc-input-lost]）。
- 架构文档要求“同一原因、同一身体状态和同一世界修订下的重新验证次数有上限”（[`continuous-height-ground-movement-v1.md:193`][arch-cap]）。
- D037 只授权“离开证明适用范围或连续没有进展时”重规划，没有提到输入失联。

**影响：**

- 输入通道真的断了时，上层永远收不到失联终态。
- 身体被卡住时，停滞—重规划会无限循环。

**修法：**

- `INPUT_LOST` 恢复为终态：先安全落地，再报告。
- `NEEDS_REPLAN` 按（原因，起始节点，世界修订）计数，达到上限后返回有类型的失败，例如 `replan_limit_reached`。
- 两项都补会话级测试。

## 四、空中动作之后的小高差会让规划失败（P1）

**现象**（[`later_run_research.py`](2026-09-27-continuous-height-hardening-repro/later_run_research.py)）：

- 完全已知的场地，宽 W 格：先走 1 格，下落 1 格，再走 1 格，然后 R 行横贯全宽的半格台阶。
- 对照组相同，但没有下落。

| 场地 | 半格台阶行数 | 无下落 | 先下落一格 |
|---|---|---|---|
| 宽 7 | 2 / 4 / 6 / 8 | 2 次 A*，23–45 ms | 6 / 8 / 8 / 8 次 A*，136–186 ms，全部改用 Step |
| 宽 21 | 2 / 4 / 6 | 2 次 A*，23–35 ms | 6 / 10 / 14 次 A*，125–429 ms，全部改用 Step |
| 宽 21 | 8 | 2 次 A*，42 ms | **17 次 A*，545 ms，返回 `unsupported`** |

**原因**（[`known_map_planner.py:2436-2536`][later-runs]）：

- 空中动作之后的地面段没有入口状态，不能证明，这一点符合 D037。
- 但处理方式是“只禁用当前路线上的证明边，然后整次重搜”。宽场地里每一行台阶都有很多条等价的证明边，每轮只去掉一条路线上的，A* 下一轮换一列再用。
- 16 轮用完后，`for ... else` 把路线清空，返回 `unsupported`。最后一轮重搜的结果也没有再检查。
- 每次 `run_search` 都拿到完整的扩展数和时间预算，所以总耗时可以达到 16 倍预算。

**影响：**

- 在自然地形中，以下情况都会出现这种组合：
  - 下坡、上跳或跨隙之后，接土径（高差 1/16）、积雪层或半砖；
  - 村庄道路和雪原。
- 结果是整条路线规划失败，会话 `FAILED`；或者规划时间超过 500 ms 后台预算。

**修法：** 一次搜索就排除这些边，不要逐条禁用。

- 在搜索状态里加一位“仍在首个地面段”。经过空中动作、上跳或 Step 之后，这一位清零，此后只展开非证明边（Step 后备）。
- 这样一次 A* 就能得到合法路线，不再需要循环。
- 等以后有“前一动作出口窗口 → 后续地面段”的证明链，再放开这一位。

## 五、落点门槛的探边分支（P1）

**代码**（[`route_admission.py:117-148`][gate]）：

```
lower_volume_seen = evidence.lower_region_visible and evidence.observer_distance_blocks <= 4.5
fresh_edge_probe  = body.on_ground and body.is_sneaking and 与落点格中心水平距离 <= 0.35
return lower_volume_seen or fresh_edge_probe      # 两者都要求证据不超过 5 个观察序号
```

**现象**（[`landing_gate_edge_branch.py`](2026-09-27-continuous-height-hardening-repro/landing_gate_edge_branch.py)）：

- 证据是 9 格外看到的，而且只看到落点格上部（`lower_region_visible=False`）：
  - 站在中心：拒绝；
  - 站在边缘：拒绝；
  - **潜行站在边缘：接受。**
- 客户端报告的距离是眼睛到格子最近点的距离（[`SurfaceSensor.java:188-194`][distance]）。身体在边缘时：

| 下落高度 | 站立 | 潜行 |
|---|---|---|
| 2 格 | 2.62 | 2.27 |
| 3 格 | 3.62 | 3.27 |
| 4 格 | 4.62（超出） | 4.27 |
| 5 格 | **5.62（超出）** | **5.27（超出）** |
| 8 格 | 8.62（超出） | 8.27（超出） |

**问题：**

- 以下四处都写“首版只接受 4.5 格内、5 个世界 tick 内、下部可见的证据”：
  - 架构文档第 176 行（[`continuous-height-ground-movement-v1.md:176`][arch-gate]）；
  - 验收文档第 160 行；
  - D004 第 63 行；
  - D035 第 47 行。
  代码的第二个分支两项都不要求。
- 5 格及以上的下落不可能满足第一个分支，所以实际放行完全靠第二个分支。验收记录说 5 格下落“确认落点下部后”才下落，代码并不检查这一点。
- 实践中，潜行站在边缘向下看时，落点格下部通常确实可见，所以风险不高。但这是安全门槛：文档声明的条件应当就是代码检查的条件。
- 另外，“5 个世界 tick”在代码中是观察序号之差，不是世界 tick。

**修法：**

- 探边分支也要求 `lower_region_visible`，并要求证据的观察序号不早于身体到达边缘的那一帧。
- 距离条件改为按“眼睛到落点格下部”的几何计算，或者在文档中明确：多格下落以“边缘近距离 + 下部可见”代替距离上限。
- 文档和代码改成同一句话。

## 六、额度记账的两个缺口（P1）

### 1. 请求时的余额在接纳时不复核

**现象**（[`stale_request_budget.py`](2026-09-27-continuous-height-hardening-repro/stale_request_budget.py)，写法与上游测试 `test_replanning_keeps_damage_already_spent_by_the_same_task` 相同）：

| 步骤 | 值 |
|---|---|
| 任务额度，请求发出时的余额 | 2 点 |
| 请求的候选被接纳之前，同一任务的一次 2 点下落完成 | 剩余 0 点 |
| 下一帧接纳的候选 | 一个 5 格 `ControlledDropSegment`，保守伤害 2 点；执行器绑定的额度仍是 2 点 |

**原因：**

- `update_goal` 和重规划在发请求时写入 `_remaining_damage_budget()`（[`navigation_session.py:825`][update-budget]）。
- 接纳和执行器使用的是请求里的旧值（[`navigation_session.py:1650-1666`][exec-budget]）。
- 伤害在下落完成后才记账。

**真实场景：** 追击中目标更新，新请求在后台规划；与此同时，旧路线作为安全前缀继续执行，并完成一次受伤下落。新候选随后被接纳。

**修法：**

- 接纳时比较候选请求的额度与当前剩余额度。余额变少时，重新检查路线中受伤下落的总和，不满足就重新请求。
- 或者在下落离边、即承诺时预扣额度，不等到完成。

### 2. 以 `INPUT_LOST` 结束的下落不记账

- `ActionRouteExecutor` 只在 `_advance`，即动作成功完成时，累计下落伤害（[`action_route_executor.py:704-716`][advance]）。
- 离边后输入失联的下落：执行器负责落地后报告 `INPUT_LOST`，这次伤害不会记账。
- 会话随后按第三节重规划，余额没有扣减。
- 整改计划写的是“归因不确定时按保守预计值扣除”（[`continuous-height-ground-movement-hardening-plan.md:53`][plan-attr]）。
- **修法：** 离边即记账，或者在任何终态（成功、失败、失联、取消）都按保守预计扣除已离边的下落。

## 七、直接下落用时退化（P2）

| 直接下落 | 整改前 Fabric | 本轮 Fabric（验收文档第 10、11 节） | 持续前进参照 | 门槛 |
|---|---|---|---|---|
| 1 格 | 16 | 16–17 | 10 | 参照 + 3 |
| 2 格 | 18 | **34** | 12 | 参照 + 3 |
| 3 格 | 21 | **36** | 15 | 参照 + 3 |
| 5 格 | 24 | **39** | 17 | 参照 + 3 |

- 多出的 15 tick 左右来自落点证据：转头、等待、探边，然后从潜行姿态下降。验收文档列出了这些数字，但没有说明退化，也没有调整门槛。
- **建议：**
  - 规划得到含多格下落的路线后，在接近边缘时就请求落点格并低头观察，让证据在到达边缘时已经成立；只在确实被平台遮挡时才探边。
  - 5 个观察序号的有效期很短，路上取得的证据到边缘时已经过期。可以把有效期改为“离边之前 N tick 内”，或者“观察后没有相关世界变化”，并用实测确定 N。
  - 如果决定接受这项代价，应当写进决定记录，并调整“多 3 tick”门槛的适用范围。

## 八、方向判断

**做对的：**

- 证明只负责“可通行 + 走廊 + 依赖 + 入口出口”，在线控制逐帧用同一计算器选键。这正是架构文档原本的设计，这次实现与文档一致了。
- 证明按表面节点序列绑定，与运行时浮点路线点解耦。
- 路线级额度用非支配资源标签搜索，理论上正确。
- 连续整格下降合成地面证明，并保留逐级受控下降作为后备，边界写得清楚。
- 保留了失败证据批次，文档如实说明完整矩阵未关闭。

**需要纠正的：**

- 新增的生命周期代码（探边、重规划、额度记账）散落在 `NavigationSession` 里，靠 reason 和布尔位驱动，没有所有者、没有上限。本轮的 P0 和两个 P1 都出在这里。
- 后续地面段的“有界重搜”把一个类型规则（空中动作之后不用证明边）实现成了逐条试错。
- 落点门槛的文档和代码不一致。

**建议的下一步**（都在现有模块内，不需要新框架）：

1. **P0**：探边改为有所有者的动作，在目标或请求变化、路线不再是对应下降、取消和超时时结束。
2. **P1**：
   - `INPUT_LOST` 恢复终态；`NEEDS_REPLAN` 计数设上限；
   - 后续地面段改为搜索状态位，一次搜索完成；
   - 落点门槛两个分支都要求下部可见，文档与代码统一；
   - 额度在接纳时复核、离边即记账。
3. **P2**：
   - 提前取得落点证据，减少探边；说明并确认直接下落的用时代价；
   - 盆栽测试改为按注册表枚举（整改计划 H6 的写法），目前只测了 3 种。
4. 这些修完后，再跑验收文档第 3 至第 6 节的完整矩阵。组件闭环部分已经证明能通过，完整矩阵主要剩 Fabric 重复次数和速度带。

## 九、复现

目录：[`2026-09-27-continuous-height-hardening-repro/`](2026-09-27-continuous-height-hardening-repro/)。

- 在 `5fa2f33` 检出目录的仓库根目录运行 `PYTHONPATH=. python -B <脚本路径>`。
- 需要 `height_transition_cost.py` 的脚本已与它放在同一目录。
- 全部输出，包括上一轮四个脚本的重跑结果，见 `outputs-5fa2f33.txt`。

| 脚本 | 结果 |
|---|---|
| `edge_hold_persists.py` | 探边后改目标，路线要 `strafe=1`，会话一直只提交潜行 |
| `input_lost_replans.py` | `INPUT_LOST`、`NEEDS_REPLAN` 各 12 帧内发 6 次新请求，不进入终态 |
| `later_run_research.py` | 下落后接 8 行半格、宽 21：17 次 A*，返回 `unsupported` |
| `landing_gate_edge_branch.py` | 9 格外、只见上部的证据，潜行在边缘即被接受；5 格以上距离恒超 4.5 |
| `stale_request_budget.py` | 余额 0 时，接纳了按 2 点请求规划的 2 点下落 |
| `start_offset_closed_loop.py` | 起点偏到格子角落，接纳并闭环完成 |
| `stair_descent_robustness.py` | 合成连续整格下降：延迟、随机迟到 100/100、朝向偏差、起点偏移都能完成 |
| `traversal_control_cost.py` | 闭环控制 P95 约 2 ms，每帧中位 7 次计算器步进 |

[base]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/tree/5fa2f333e7bda8c45d71eedf51f36c97c44edec4
[hold-clear]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/5fa2f333e7bda8c45d71eedf51f36c97c44edec4/mc2p/motion_nav/navigation_session.py#L1211-L1216
[replan]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/5fa2f333e7bda8c45d71eedf51f36c97c44edec4/mc2p/motion_nav/navigation_session.py#L1200-L1207
[input-lost-map]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/5fa2f333e7bda8c45d71eedf51f36c97c44edec4/mc2p/motion_nav/navigation_session.py#L1674-L1682
[acc-input-lost]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/5fa2f333e7bda8c45d71eedf51f36c97c44edec4/docs/motion_navigation/acceptance/continuous-height-ground-movement.md?plain=1#L101
[arch-cap]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/5fa2f333e7bda8c45d71eedf51f36c97c44edec4/docs/motion_navigation/architecture/continuous-height-ground-movement-v1.md?plain=1#L193
[later-runs]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/5fa2f333e7bda8c45d71eedf51f36c97c44edec4/mc2p/motion_nav/known_map_planner.py#L2436-L2536
[gate]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/5fa2f333e7bda8c45d71eedf51f36c97c44edec4/mc2p/motion_nav/route_admission.py#L117-L148
[distance]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/5fa2f333e7bda8c45d71eedf51f36c97c44edec4/mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface/SurfaceSensor.java#L188-L194
[arch-gate]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/5fa2f333e7bda8c45d71eedf51f36c97c44edec4/docs/motion_navigation/architecture/continuous-height-ground-movement-v1.md?plain=1#L176
[update-budget]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/5fa2f333e7bda8c45d71eedf51f36c97c44edec4/mc2p/motion_nav/navigation_session.py#L815-L830
[exec-budget]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/5fa2f333e7bda8c45d71eedf51f36c97c44edec4/mc2p/motion_nav/navigation_session.py#L1650-L1666
[advance]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/5fa2f333e7bda8c45d71eedf51f36c97c44edec4/mc2p/motion_nav/action_route_executor.py#L704-L716
[plan-attr]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/5fa2f333e7bda8c45d71eedf51f36c97c44edec4/docs/motion_navigation/stages/continuous-height-ground-movement-hardening-plan.md?plain=1#L53
