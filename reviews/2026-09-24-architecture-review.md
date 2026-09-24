# 运动／导航与 C1 战斗切片整体审查

日期：2026-09-24
审查对象：本仓库提交 `9863056`（文中行号均指该提交）
性质：外部审查意见，不属于 `docs/motion_navigation/` 四类正式文档，不改变任何阶段结论或验收门槛

## 结论

1. **大方向正确。** LLM 不进 20 Hz 控制回路；先做规则底座；观察遵守合法边界（120° 视野加遮挡过滤，未知不当空气）；只有一个输入出口；“动作已发出”和“动作已成功”分开记录；失败证据不删除。这些是“玩家级伙伴”最难后补的地基，已经打好。1.21 运动计算器（`physics_1_21.py`）也写得扎实。
2. **最大问题：C1 战斗没有运行在 B01–B10 运动主线上。** 它使用 `skills/` 中另一套旧的平地导航 `PointGoalPolicy("D")`。战斗因此用不到跳跃、台阶、跨隙、运动计算器、状态锚点和后台规划。C1 文档写的是“检验 B10 公共链”，代码并非如此。
3. **控制结构与“最优策略战斗”冲突。** “同一时刻只有一个技能控制身体，停下、做完、再走”在协议、仲裁和驱动三层都写死了：
   - 移动或转头不能和攻击在同一帧；
   - 没有“按住使用”，吃东西、举盾、拉弓都无法实现；
   - 受击后必须刹到静止；
   - 同一任务第 5 次受击直接判失败。
4. **验收模式的失败处理写进了运行时。** 证据不确定或日志线程失败都会封存 Runtime 并断开客户端。验收时这样做合理；对常驻伙伴，这等于到处埋着关机开关。
5. **扩展方式偏慢。** 十个阶段后，运动能力范围是：完整方块、四个正方向、起跳前水平速度不超过 0.1 格／秒（基本需要停住）。当前做法是每种动作一个控制器，再维护接续矩阵，照这个节奏很难走到“各种地形都丝滑”。最有价值的资产——运动计算器——还没有被用作通用局部规划器。

## 审查范围与证据边界

- 阅读了四类文档，以及 `mc2p/runtime`、`mc2p/contracts`、战斗驱动、外部运动检测与恢复、导航驱动、`mc2p/motion_nav` 核心模块。
- 阅读了 Java 客户端执行器（`ClientBehaviorExecutor.java`）和观察采集器（`ClientObservationCollector.java`）。
- 在 Linux 容器中以 Python 3.11 运行了单元测试，结果见第四节。
- **没有运行任何 Fabric 实机验收。** 本文关于实机行为的判断来自代码阅读和已有验收记录，属于推断，需以实机证据为准。

## 一、分层与设想对照

项目设想分三层：顶层 LLM 负责性格、目标和记忆；中层 Jev 按局势切换策略；底层是移动、战斗、挖矿、建筑等技能。

| 设想 | 仓库现状 | 评价 |
|---|---|---|
| 顶层 LLM：性格、目标、记忆 | 有 [`BehaviorProfileV0`](../mc2p/contracts/behavior.py#L11)（risk_tolerance、protectiveness 等 7 项），各驱动都会传递，但**没有任何代码读取这些字段** | 性格到行为的通道目前为空 |
| 中层 Jev：按局势切换策略 | 战斗只有一个写死的 `MovingMeleeDriver`，没有可选策略和参数，上报只有状态和原因字符串 | Jev 暂时没有可切换的对象 |
| 底层技能 | 运动在 `motion_nav`；战斗在 `skills/`，使用另一套导航；挖和放只有协议操作（`MineBlockV1`、`InteractBlockV1`），没有技能 | 两套导航栈并存 |
| 输入：血量、饥饿、物品栏、地形、敌人 | Observation V3 覆盖生命、吸收、护甲、饥饿、饱和、状态效果、物品栏、使用物品状态，以及合法可见的方块和实体 | 最符合设想的部分 |

本文不假设 Jev 的内部机制，只从接口看。中层要能切换策略，底层需要提供：

- 可参数化、可同时运行、可打断的技能；
- 一份统一的局势状态（威胁、自身资源）；
- 有类型的事件上报（受击、目标丢失、资源不足）。

目前这三样都只存在于单个战斗驱动内部。

## 二、架构问题（按影响排序）

### 1. 战斗没有复用运动主线

**证据**

- [`scripts/c1_moving_melee_runtime.py:315`](../scripts/c1_moving_melee_runtime.py#L315) 使用 `PointGoalPolicy("D", …)`。
- `mc2p/skills/` 中除 `external_motion*` 外，没有任何 `from mc2p.motion_nav` 导入。
- D 组规划器复用 C 组的格子搜索（[`navigation_joint_policy.py:101`](../mc2p/skills/navigation_joint_policy.py#L101)）：单层，只走四个正方向。[`point_goal_policy.py:264`](../mc2p/skills/point_goal_policy.py#L264) 用 `floor = round(own.position.y) - 1` 推算脚下地面，正是[总架构](../docs/motion_navigation/architecture/mc_motion_navigation_architecture_v1.md) 3.1 节明确禁止的简化。

**根因**

`motion_nav` 没有总架构 M05 规定的会话接口（`start / update_goal / cancel / poll`）。B10 的编排写在脚本中：[`b10_gap_solver_runtime.py:428-490`](../scripts/b10_gap_solver_runtime.py#L428) 手工构造支撑面、跨隙边和活动路线，并把玩家传送到起点。任务层没有可以直接调用的导航库，C1 只能接到旧栈上。

**后果**

- 战斗只能在平地进行。原因是底层导航只支持平地，而不是验收恰好选择了平地。
- 同时存在两份世界记忆：`skills/navigation_memory` 60 秒后遗忘、半径 32 格（[`normal_navigation_types.py:55-57`](../mc2p/skills/normal_navigation_types.py#L55)）；`motion_nav` 的 [`WorldKnowledge`](../mc2p/motion_nav/world_model.py#L220) 永久保存。这违反了 AGENTS.md 中“每类长期状态只有一个拥有者”。

**建议**

把 `motion_nav` 封装成 `NavigationSession`，把脚本里的控制循环收进 `mc2p/`，再把战斗的接近和追击迁移过去。`skills/` 的 A–E 导航冻结为参照实现。

### 2. “移动和攻击不能同帧”写在三层

- **仲裁：** [`arbiter_v1.py:140-142`](../mc2p/runtime/arbiter_v1.py#L140)，攻击操作与非中性的移动或转头只能保留一个。
- **客户端：** [`ClientBehaviorExecutor.java:132-134`](../mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorExecutor.java#L132)，请求同时包含控制和操作时直接拒绝（`unsupported_control_operation_combination`）。
- **驱动结构：** 每个驱动的 `tick()` 自己调用 `runtime.step()`（例如 [`point_goal_driver.py:294`](../mc2p/skills/point_goal_driver.py#L294)、[`melee_strike_driver.py:219`](../mc2p/skills/melee_strike_driver.py#L219)），因此一帧只能由一个技能推进。战斗驱动发现攻击帧中带有移动时，会直接关停运行时（[`moving_melee_driver.py:788-795`](../mc2p/skills/moving_melee_driver.py#L788)）。
- **没有持续使用：** [`ClientUsePulse.java`](../mc2p/backends/runtime_overlays/mc121_actions/ClientUsePulse.java) 触发一次使用后立即松开，协议中也没有持续使用的操作。
- **策略写成了协议检查：** “攻击冷却满才允许攻击”本应是策略，却写在客户端协议守卫中（[`ClientBehaviorExecutor.java:256`](../mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorExecutor.java#L256)）。

**后果**

疾跑击退、跳劈暴击、冷却期间后撤、横移、边走边吃、举盾接近、拉弓都无法实现。这些正是“最优策略”战斗的基本动作。

**建议**

- 由 Runtime 独占 20 Hz 帧循环。技能每帧只提出意图（移动、视角、一次性操作、持续使用四组），仲裁按兼容表合并后只推进一次。
- 客户端允许移动、转头和攻击同帧（原版本来如此）。
- 新增带持续时间和取消的 `UseItemV1`。

### 3. “停下、做完、再走”贯穿整个栈

- **运动：** 跳上、台阶、跨隙、下落的入口水平速度上限都是 0.1 格／秒（[`jump-up-v1.json:13`](../config/motion-navigation/jump-up-v1.json#L13)、[`air-motions-b09-v1.json:20`](../config/motion-navigation/air-motions-b09-v1.json#L20)、[`step-b07-v1.json:13`](../config/motion-navigation/step-b07-v1.json#L13)；执行器在 [`action_route_executor.py:167`](../mc2p/motion_nav/action_route_executor.py#L167) 和 [`:189`](../mc2p/motion_nav/action_route_executor.py#L189) 据此要求先减速），而步行约 4.3 格／秒。每次高度变化都要先停下。
- **战斗：** 等待冷却、等待目标受击动画结束时，机器人原地不动（[`fixed_melee.py:222`](../mc2p/skills/fixed_melee.py#L222)）。
- **受击恢复：** 空中放开全部输入；落地后刹到水平速度不超过 0.03 格／tick 并保持 2 tick；随后还要在原地完成一个至少 300 ms 的点目标（[`external_motion_recovery_driver.py:254-285`](../mc2p/skills/external_motion_recovery_driver.py#L254)）。

**共同根因**

状态锚点和求解器只支持“低速站立”起步。总架构 1.3 节已经预留了 `entry_class`，但尚未实现。

**建议**

实现“带速度起步”，并用运动计算器做局部规划：把候选输入序列（移动、朝向、跳跃，8–20 tick）放在 B09-R 计算器上模拟，按碰撞、落点和代价挑选。跳跃、疾跑跳、台阶、击退后接管、战斗走位都用同一套机制。这比“每种动作一个控制器（FixedRoute／JumpUp／Step／AirMotion／VerifiedMotion）＋ N×N 接续矩阵”更容易扩展。

### 4. 受击检测与 D017 的定义不一致

[D017](../docs/motion_navigation/decisions/0017-unified-external-motion-recovery.md) 把外部运动定义为“已应用的输入无法单独解释的身体运动”。实现（[`external_motion.py:149-154`](../mc2p/motion_nav/external_motion.py#L149)）只检查自身受击动画从 0 变为正数、并且生命下降，没有使用输入账本或计算器预测残差。由此产生三个问题：

- 着火、中毒、摔伤都会被当成击退，触发刹停并计入任务级次数上限。
- 有吸收心（例如吃了金苹果）时生命值不下降，**击退会漏检**，机器人继续执行旧路线。
- 次数上限为 `maximum_events=4`（[`external_motion_recovery.py:25`](../mc2p/motion_nav/external_motion_recovery.py#L25)），整个战斗任务共用一个计数器（[`moving_melee_driver.py:95`](../mc2p/skills/moving_melee_driver.py#L95)）。D018 记录了单只僵尸在平地上碰不到机器人；但遇到多敌人、小僵尸或骷髅射箭时，被打 5 次很正常。

**建议**

用“实际运动减去按已应用输入预测的运动”这一残差检测外力，伤害事实只作为来源标签。受击次数作为局势信号交给中层，不设硬性失败。

### 5. 验收模式的失败处理写进了运行时

[`PlayerRuntimeV1._seal()`](../mc2p/runtime/player_runtime_v1.py#L397) 会关闭与客户端的连接。触发它的情况包括：

- `PointGoalDriver` 遇到任何异常，经 `_fail()` 调用 `fail_closed`；
- 驱动主动调用 `fail_closed`；
- 日志写入线程失败后，下一次 `write()` 抛错（[`async_trace.py:62`](../mc2p/runtime/async_trace.py#L62) 起）。

交战记忆也很脆弱：

- 观察序号只要跳过一帧，交战记忆就被永久撤销（[`engagement_memory.py:194`](../mc2p/skills/engagement_memory.py#L194)）。
- 控制端卡顿超过 500 ms，目标位置过期，整个任务直接失败（[`moving_melee_driver.py:685`](../mc2p/skills/moving_melee_driver.py#L685)）。

这些都依赖一个隐含约定：每个父驱动必须观察到每一帧。[`external_motion_recovery_driver.py:148`](../mc2p/skills/external_motion_recovery_driver.py#L148) 的注释说明这个坑已经踩过一次。

**建议**

- 区分两种失败策略：验收用严格模式；常驻用“中性输入 → 重新锚定 → 上报”，不断开连接。
- 观察流改为每帧由 Runtime 广播给交战记忆、受击检测和导航记忆，不再依赖各驱动手动调用 `_observe()`。

### 6. 用字符串决定流程

AGENTS.md 第 4 节要求“不要用 `reason` 字符串……暗中决定权限和生命周期”，但：

- [`point_goal_driver.py:264`](../mc2p/skills/point_goal_driver.py#L264) 通过比较异常消息文本决定是否停止；
- [`moving_melee_driver.py:472-491`](../mc2p/skills/moving_melee_driver.py#L472) 通过 `report.reason == "hit_confirmed" / "target_dead" / "target_revised_after_submit"` 分支；
- 子驱动状态是裸字符串，例如 `"needs_approach"`、`"observing_after_submit"`。

**建议**

改为枚举结果类型，异常改为专门的异常类。

## 三、具体代码问题

| 位置 | 问题 | 影响 |
|---|---|---|
| [`moving_melee_driver.py:305`](../mc2p/skills/moving_melee_driver.py#L305) | 重新找回目标时，俯仰角公式 `atan2(-(rel.y+.9), h)` 与 [`melee_strike_driver.py:250`](../mc2p/skills/melee_strike_driver.py#L250) 符号相反，且没有扣除眼高 | 同一高度、2.5 格外的目标会向上看约 20°（正确应向下约 14°）。目前靠 120° 视野兜底才通过；1.5 格以内或有高度差时可能转不回来，20 次后任务以 `reacquire_vision_exhausted` 失败 |
| [`external_motion.py:139`](../mc2p/motion_nav/external_motion.py#L139)、[`:154`](../mc2p/motion_nav/external_motion.py#L154) | 只比较 `health_points`，不计吸收心 | 有吸收心时击退漏检 |
| [`melee_strike_driver.py:250`](../mc2p/skills/melee_strike_driver.py#L250) | 眼高写死为 1.62 | 潜行时（眼高 1.27）瞄准偏差 |
| [`fixed_melee.py:114`](../mc2p/skills/fixed_melee.py#L114) | 攻击站位的 y 直接取自身 y | 只适用于平地 |
| [`moving_target.py:47`](../mc2p/skills/moving_target.py#L47) | 用 `floor(y)-1` 推算目标脚下支撑 | 半砖、楼梯上误判，导致反复更换站位目标 |
| [`fixed_melee.py:204`](../mc2p/skills/fixed_melee.py#L204)、[`:222`](../mc2p/skills/fixed_melee.py#L222) | 攻击前必须等目标受击动画归零 | 队友同时攻击或目标着火时，攻击会被持续推迟。“能否确认命中”反过来限制了“要不要打” |
| [`moving_melee_driver.py:670`](../mc2p/skills/moving_melee_driver.py#L670) | 上一行已处理 `!=` 并返回，这里的 `==` 条件恒为真 | 无害，影响可读性 |
| [`async_trace.py:62`](../mc2p/runtime/async_trace.py#L62) | 日志投影在控制线程同步执行，每帧 step 记录包含整份观察 | 帧时间预算隐患，后续加入多目标评估时会更明显 |
| [`world_model.py:220`](../mc2p/motion_nav/world_model.py#L220) | 无上限的全局字典，不分区块；任何一次更新都会让所有视图过期（[`:214`](../mc2p/motion_nav/world_model.py#L214)） | 长时间常驻运行的内存和失效粒度问题 |

## 四、测试与文档

在 Linux 容器（Python 3.11.15）中运行，结果如下：

- **运动／导航专项：** `python -m unittest discover -s tests/motion_nav -p 'test_*.py'` 运行 338 项，失败 1 项。失败的是 [`test_fixed_route_walk.py:401`](../tests/motion_nav/test_fixed_route_walk.py#L401) 的 P95 ≤ 8 ms 墙钟断言，该容器实测 11.4 ms。按墙钟计时的性能门槛放在单元测试中，换一台机器就不稳定，建议移到基准测试或验收中。
- **公开快照缺文件：** 仓库引用了 4 个不存在的测试模块：`test_navigation_motion`、`test_craftground_backend`、`test_craftground_runtime`、`test_visible_equipment_projection`。因此 `tests/test_tracked_entity_observation.py` 和 `tests/test_navigation_look.py` 在公开仓库中无法导入。
- **根目录其余失败：** 需要先安装 `psutil` 才能导入。安装后剩余的失败主要来自 Windows 硬编码路径（`.venv/Library/bin/javac.exe`）和未构建的 Java 运行时，属于预期。

需要修正的过时文档（AGENTS.md 要求文档与事实冲突时先修正文档）：

- [C1 架构](../docs/motion_navigation/architecture/C1-combat-vertical-slice-v1.md) §5.2 仍写“游戏确认仇恨”才建立交战记忆；实际实现的是 [D016](../docs/motion_navigation/decisions/0016-c1b-target-vitality-and-seeds.md) 的“确认命中后按报复规则授权”。
- C1 架构和[阶段记录](../docs/motion_navigation/stages/C1-fixed-visible-melee.md)都写“检验 B10 公共链”，与代码不符（见第二节第 1 条）。

## 五、建议的推进顺序

1. **收敛到一条主线。** 把 `motion_nav` 封装成 `NavigationSession`（`start / update_goal / cancel / poll`），把脚本中的控制循环收进 `mc2p/`，再迁移战斗的接近和追击。`skills/` 的 A–E 导航冻结为参照。
2. **重构帧循环，扩展动作契约。** Runtime 独占帧循环，技能只提出意图；允许移动、转头、攻击同帧；新增 `UseItemV1`，支持吃东西、举盾和拉弓。这一步决定以后能否实现“最优策略”。
3. **带速度起步，用计算器驱动局部规划。** 以此取代“每种动作一个控制器”。击退恢复改为从当前运动状态直接重新规划；外力检测改用预测残差。
4. **战斗技能库参数化。** 包括保持攻击距离、冷却期间后撤、疾跑击退、跳劈、撤退或风筝、安全时进食、举盾，以及多敌人威胁评估和统一的局势状态。Jev 在这些技能之间切换；`BehaviorProfile` 映射为具体参数，例如风险容忍度决定撤退血线。受击次数只作为局势信号，不设硬性失败。
5. **常驻模式使用独立的失败策略。** 验收保留严格模式；常驻采用“中性输入 → 重新锚定 → 上报”，不断开连接。观察流由 Runtime 每帧广播，取代各驱动手动 `_observe()`。
6. **考虑把声音字幕加入合法信息。** 真人玩家很依赖声音察觉身后的怪物。只有 120° 视野时，机器人要么频繁转头，要么在感知上天然弱于人。
