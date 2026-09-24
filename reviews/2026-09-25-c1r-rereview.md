# C1-R 更新复审

日期：2026-09-25
审查对象：`origin/main` 提交 [`1c6a0de`][base]（由主项目提交 `6b7fc98` 导出的快照）
性质：外部审查意见，不属于 `docs/motion_navigation/` 四类正式文档，不改变任何阶段结论或验收门槛
上一轮审查：[2026-09-24 整体审查](2026-09-24-architecture-review.md)

本文代码链接都指向 `1c6a0de`。本分支仍基于旧快照，请不要按本分支的相对路径查看代码。

## 结论

1. **这轮修改认真回应了上次审查，方向正确，多数问题有实质推进。**
   - 战斗接近已经改走 `NavigationSession`；
   - Runtime 成为唯一逐帧推进者；
   - 移动、转头和攻击可以同帧；
   - 外力检测引入了运动残差；
   - 世界知识按区段存储并设了上限；
   - 日志线程失败不再断开客户端；
   - 瞄准、眼高、C1 文档都已修正。
   - 公开快照在 Linux 上可以按 README 复现声明的检查。
2. **发现四个新的高优先级问题。** 它们都只在冻结的平地验收范围之外暴露，所以现有 Fabric 结果没有覆盖到：
   1. 战斗使用的 Runtime 导航桥不提供状态锚点和输入账本，**战斗中的 JumpGap 路线会一直等待，永远不执行**（已复现）。R2 所说“战斗可以使用已验证的地形动作”只对步行、台阶和 JumpUp 成立；R4 的带速跨隙只在 B10 脚本路径中可用。
   2. `NavigationSession` 在目标修订或取消时**直接丢弃执行器**，空中动作因此失去落地责任（违反 I10）。在平地上，每次目标修订至少多出一帧中性输入（已复现）。
   3. 外力检测从“只看伤害”变为“伤害与残差同时成立”。但在吸收心、再生、进食状态下，以及 10 种材质之外的地面上，残差都算不出来。这些情况下**击退完全不会被检测**（已复现），比上一版更不安全。R3 声称修复的“吸收心漏检”在真实游戏中仍会漏检。
   4. R0 允许在目标受伤动画期间攻击，但命中确认要求“受伤计时增加”。按 1.21 原版逻辑，无敌帧内的命中不会重置受伤动画，因此这类命中无法确认，确认超时会让**整个战斗任务失败**。清晨着火的僵尸就会稳定触发这种情况。
3. **D019 有几项只完成了一半：**
   - 失败处置枚举没有接入任何调用方；
   - 第 5 次需要身体恢复的受击仍直接判战斗失败；
   - 交战记忆在归约层能容忍缺口，驱动层却仍在缺口帧终止任务；
   - 用字符串决定生命周期的写法基本仍在；
   - 战斗技能本身仍然“停下再打”，没有用上同帧能力。

## 审查范围与证据边界

- 阅读了 D019、C1-R 阶段／架构／验收文档，以及以下代码：
  - Runtime、仲裁、客户端执行器；
  - `navigation_session.py`、`navigation_session_driver.py`；
  - 运动残差、外力检测与恢复；
  - 交战记忆、移动近战驱动、单次攻击驱动、世界知识。
- 在 Linux 容器（Python 3.11.15、OpenJDK 21）中按 README 运行了全部声明检查，结果见第四节。
- 三个新问题附有可运行的复现脚本，放在 [`2026-09-25-c1r-repro/`](2026-09-25-c1r-repro/)，需要在 `1c6a0de` 的检出目录中用 `PYTHONPATH=.` 运行。
- 没有运行 Fabric 实机。关于原版无敌帧和击退离地的判断来自 1.21 原版逻辑，需要实机确认。

## 一、上次问题的处理状态

| 上次问题 | 本轮对应 | 状态 | 说明 |
|---|---|---|---|
| 战斗没有复用运动主线 | R2 | 大部分解决 | C1 通过 `RuntimeNavigationDriver` 使用 `NavigationSession`，不再导入旧导航。但 JumpGap 在战斗路径中不可达（第二节第 1 条）；世界知识归单个会话所有（第三节第 4 条） |
| 移动与攻击互斥 | R1 | 协议层解决，技能层未使用 | 仲裁（[`arbiter_v1.py:139`][arb-attack]）和客户端都允许攻击与移动同帧，攻击在同一次输入采样前派发。但战斗技能仍然停下再打（第三节第 2 条） |
| 停下、做完、再走 | R3、R4 | 部分解决 | 带速跨隙完成了试点；受击恢复仍要刹到静止；目标修订引入了新的中性帧（第二节第 2 条） |
| 受击检测只看伤害 | R3 | 设计正确，覆盖范围倒退 | 伤害事实与运动残差分开是对的；残差不可用时不产生事件，变成了漏检（第二节第 3 条） |
| 验收失败语义写进运行时 | R3 | 部分解决 | 日志线程失败不再断开 ✓；[`failure_disposition.py`][fd] 没有任何调用方；`step()` 任何异常仍会 `_seal()`（[`player_runtime_v1.py:375-376`][seal]） |
| 用字符串决定流程 | R3 | 基本未解决 | 见第三节第 3 条 |
| 瞄准俯仰角、眼高写死 | R0 | 已解决 | 新增共用的 `combat_aim_angles`，观察中增加实际眼高 |
| 吸收心漏检 | R3 | 事实层解决，端到端未解决 | 伤害事实已计入吸收心；但吸收心必然伴随吸收效果，残差因此不可用（第二节第 3 条） |
| 必须等受伤动画结束才攻击 | R0 | 改了，但引入新问题 | 见第二节第 4 条 |
| 日志序列化在控制线程 | R3 | 已解决 | 控制线程只做浅拷贝，投影移到工作线程 |
| 世界知识无上限 | R3 | 已解决 | 16³ 区段、262,144 格上限、保护半径和路线依赖，结果带类型 |
| 墙钟断言、缺失模块、psutil、Java 路径 | R5 | 已解决 | 3 个补上的模块仍无法在公开环境运行（第四节） |
| C1 文档与代码不符 | R0 | 已解决 | C1 架构已写明 C1-A 至 C1-C 使用旧导航，交战许可来自报复规则 |
| `floor(y)-1` 推支撑 | — | 未改 | [`moving_target.py:54-55`][support] |

## 二、高优先级新问题

### 1. 战斗路径中的 JumpGap 永远不会执行

- `RuntimeNavigationDriver.tick()` 调用 `session.propose(frame, None, deadline)`，没有传状态锚点，也没有传输入账本（[`navigation_session_driver.py:134`][bridge]）。
- `NavigationSession.propose()` 只有在锚点和账本都存在时才会走协调器（[`navigation_session.py:699`][propose]）。否则 JumpGap 路线始终处于 `awaiting_verified_motion`。
- 复现：[`jumpgap_via_runtime_bridge.py`](2026-09-25-c1r-repro/jumpgap_via_runtime_bridge.py) 使用会话测试自己的跨隙世界：
  - 按测试方式传入锚点和账本，第 28 次轮询提交了跳跃命令；
  - 按 Runtime 桥的方式传 `(None, None)`，3 秒内 293 次轮询都没有提交输入。
- 正式路径上目前也没有锚点构造器。锚点仍由脚本自己构造（[`b10_gap_solver_runtime.py:478`][anchor-script]），并且脚本使用本地新建的账本（[`:528`][ledger-script]），而不是 `runtime.input_ledger`。

**影响：** R2 “战斗可以通过新导航会话使用已验证的地形动作”和 R4 “带速跨隙”在战斗中都不成立。C1 的冻结场景是平地，所以验收没有暴露这个问题。

**建议：** `MotionResidualTracker` 已经在每次观察后构造 `StateAnchor`（[`motion_residual.py:332`][tracker-anchor]）。把它和 `runtime.input_ledger` 通过桥传给 `propose()`，删除脚本里的第二份账本。再增加一个“经由 `RuntimeNavigationDriver` 执行 JumpGap”的集成测试。

### 2. 目标修订或取消会丢弃执行器

- `update_goal()` → `_replace_request()` → `_retire_route()`：先调用 `executor.cancel()`，随后立刻 `self._executor = None`（[`navigation_session.py:757-780`][retire]）。
- `ActionRouteExecutor.cancel()` 只把状态设为 `CANCELLING`，设计上要靠后续的 `decide()` 完成空中落地（[`action_route_executor.py:351`][exec-cancel]）。执行器被丢弃后，这个落地阶段永远不会执行。
- `session.cancel()` 也一样：会话状态变为 `CANCELLED` 后，`propose()` 直接返回中性输入（[`navigation_session.py:693-696`][neutral]），不再调用执行器。战斗中进入攻击距离时会释放导航，走的就是这条路径。

**影响：**

- **安全：** 移动目标每移动 0.75 格就会修订一次目标。只要战斗路线包含 JumpUp 或 ControlledDrop（这两个动作不经过协调器，战斗路径可以用），在空中收到修订或进入攻击距离，机器人就会失去落地控制。平地验收不会触发。
- **流畅：** 即使在平地，每次修订也要等新路线被接纳后才恢复移动。复现 [`goal_revision_neutral_frames.py`](2026-09-25-c1r-repro/goal_revision_neutral_frames.py) 使用真实的后台规划进程，在一条直走廊上测得每次修订后至少 1 帧中性输入。僵尸追击时每隔几 tick 就会修订一次，追击会反复顿挫。
- 这也与 C1-B 架构第 7 节“已接纳且仍安全的前段可以继续执行”不一致。

**建议：** 目标修订时保留当前执行器，直到新路线被接纳；空中或有落地责任时推迟替换。`cancel()` 在执行器仍是 `CANCELLING` 时继续调用它的 `decide()`。补上“空中修订”和“空中取消”的会话级测试。

### 3. 外力检测在常见战斗状态下失效

- 运动残差用 B09-R 的 `step()` 重放已应用的输入。以下情况 `step()` 会返回 `UNSUPPORTED`：
  - 除失明外的任何状态效果（[`physics_1_21.py:328`][effects]）；
  - 正在使用物品（[`:332`][using]）；
  - 支撑面不属于 10 种普通材质（[`:408`][material]）。
- 残差不可用时，`DamageKnockbackDetector` 返回 `motion_residual_unavailable`，并在 8 tick 后丢弃伤害事实，不产生事件（[`external_motion.py:340`][detector-pending]、[`:368`][detector-drop]、[`:377`][detector-unavailable]）。
- 复现：[`residual_unsupported_conditions.py`](2026-09-25-c1r-repro/residual_unsupported_conditions.py) 显示，以下情况都返回 `UNSUPPORTED`：
  - 吸收效果、再生效果；
  - 进食；
  - 圆石、沙子、深板岩地面。
- 原版中，吸收心只在吸收效果存在时出现，所以“吸收心受击”在真实游戏中必然走到残差不可用这条分支。组件测试（[`test_external_motion.py:59`][absorption-test]）构造了吸收心但没有吸收效果，并直接注入了人工残差，因此没有覆盖这条真实路径。

**影响：** 上一版在这些情况下至少会因为受伤而进入恢复；新版什么都不做，旧路线和旧动作责任都不会失效。吃金苹果、喝药、在洞穴石头上战斗都是常见场景。

**建议：** 残差不可用但伤害事实存在时，保守地产生一个“来源已知、运动未验证”的事件，并进入安全处理，而不是丢弃伤害事实。同时补一个带吸收效果、跑完整链路的测试。

### 4. 在无敌帧内攻击会导致战斗任务失败

- R0 删掉了“等待受伤动画结束”这一步。命中确认改为“攻击后观察到的受伤计时大于攻击前的值”（[`fixed_melee.py:232`][confirm]）。
- 按 1.21 原版 `LivingEntity.damage`：受击后的前 10 tick 内，新的攻击只有伤害更高时才补上差值，而且不会重置受伤动画。也就是说，只要观察到受伤动画大于 0，这次攻击要么被无敌帧吸收，要么造成了伤害但无法用受伤计时确认。
- 确认超时后，单次攻击失败，`MovingMeleeDriver` 把整个任务判为 `FAILED`（[`moving_melee_driver.py:533`][strike-fail]）。

**影响：** 队友同时攻击，或僵尸在清晨着火（每 20 tick 受一次火伤，大约一半时间处于受伤动画中）时，新逻辑会主动在无敌帧里出手，然后任务失败。旧逻辑虽然慢，但不会失败。

**建议：** 允许攻击的同时，把“无敌帧剩余时间”纳入攻击时机，优先等到无敌帧结束；确认时增加目标生命下降作为辅助证据；“未确认”作为可重试结果，不应终止整个任务。

## 三、D019 未完成部分与其他问题

1. **失败处置只写了分类器。** [`failure_disposition.py`][fd] 在仓库中没有调用方。`step()` 遇到任何异常仍会封存 Runtime 并关闭后端（[`player_runtime_v1.py:375-376`][seal]）。D019 第 9 条要求的“按后果分类”在运行时还没有生效。
2. **战斗技能没有用上同帧能力。**
   - 单次攻击驱动从不提交移动。战斗在进入攻击距离后仍然先释放导航，再原地瞄准和攻击。
   - 导航意图带 `movement_requires_look`（[`navigation_session.py:1055`][nav-look]）。战斗的瞄准视角一旦胜出，导航的移动就会被仲裁压掉（[`arbiter_v1.py:148`][arb-look]）。也就是说，“导航负责走位、战斗负责瞄准”目前无法组合。
   - 同帧攻击在派发时被拒绝的话，`reject()` 会通过 `rejectAdmittedRequest` 清掉这一帧的移动（[`ClientBehaviorExecutor.java:302`][client-reject]、[`:209`][client-clear]）。
   - 建议：导航输出世界坐标下的期望移动方向，再由最终合成器按当时的视角换算成按键；让攻击失败不影响同帧移动。
3. **字符串控制流仍然普遍。**
   - [`moving_melee_driver.py:505-533`][strings] 仍然靠 `report.reason == "hit_confirmed"`、`"target_dead"`、`"target_revised_after_submit"` 分支；
   - 各子驱动的状态仍是裸字符串；
   - 新的 [`navigation_session.py:962`][admit-strings] 又增加了一处 `admitted.reason in {...}`。
4. **世界知识归单个导航会话所有。**
   - `NavigationSession` 默认创建自己的 `NavigationObservationAdapter`（[`navigation_session.py:261`][adapter]），适配器再持有 `WorldKnowledge`（[`runtime_adapter.py:170`][world-owner]）。
   - C1 脚本每个试次新建一个会话（[`c1_moving_melee_runtime.py:319`][per-trial]）。
   - 常驻伙伴在跟随、战斗、挖矿之间切换时，地图记忆会随会话一起丢失；两个会话并存时又会出现两份世界知识。外力检测也依赖导航会话存在（`navigation_session.motion_residual`）。
   - 建议：把世界知识、观察适配、残差和锚点提升为 Runtime 级服务，导航会话只读取。
5. **第 5 次需要身体恢复的受击仍直接判失败。** 恢复控制器的 `maximum_events=4` 没有改，恢复被取消后战斗直接进入 `FAILED`（[`moving_melee_driver.py:694`][recovery-fail]）。D019 第 8 条要求把这个结果交给任务策略决定。
   - 新增的“直接续行”只在检测时身体着地才生效。原版击退通常会把着地目标抬离地面，所以大部分受击仍会走“刹到静止”的恢复路径。建议验收记录按“续行”和“身体恢复”分别统计 24 次恢复。
6. **交战缺口的修复停在归约层。** 归约层在观察缺口后保留目标，并等待下一帧重新锚定（[`engagement_memory.py:245`][engagement]）。但移动近战驱动在 `_fact is None` 时仍直接判失败（[`moving_melee_driver.py:757`][fact-none]），缺口帧恰好就是 `None`，所以任务仍然会终止。

## 四、公开快照复现结果

| 检查 | 结果 |
|---|---|
| `export_motion_navigation_standalone.py verify --root .` | `STANDALONE_EXPORT_OK files=568` |
| `check-java` | 找到 JDK 21 |
| `tests/motion_nav` | 401／401，约 76 秒 |
| README 声明的 C1 检查 | 41／41 |
| Java 控制门禁 | 1／1 |
| 运行后工作树 | 保持干净 |

公开快照的边界：

- 导出清单发布了全部 `mc2p/**/*.py`，但测试只发布白名单。控制帧、仲裁兼容、失败处置、日志、交战记忆、单次攻击、移动近战和外力恢复驱动的测试都不在公开仓库中。R1、R3 的核心结论因此无法只靠公开仓库复核。建议至少导出这些模块的纯 Python 测试。
- 补上的 `test_craftground_backend`、`test_craftground_runtime`、`test_visible_equipment_projection` 可以导入，但运行时仍依赖 `.venv/Lib/site-packages` 和 `.gradle` 缓存路径，在公开环境中报错。README 没有声明它们，所以不算错误声明；但它们不是“可运行的真实测试”。

## 五、建议的修复顺序

1. **修正战斗桥：** 把会话内已有的锚点和 `runtime.input_ledger` 传给 `propose()`，删除脚本中的第二份账本，补上“战斗经由桥执行 JumpGap”的集成测试。
2. **修正会话的责任交接：** 目标修订保留旧执行器直到新路线接纳；空中不替换；取消期间继续驱动 `CANCELLING` 状态的执行器。补上空中修订和空中取消的测试。
3. **外力检测不可用时保守处理：** 残差不可用但有伤害事实时，产生“未验证运动”事件并进入安全处理；补上带真实效果的端到端测试。
4. **攻击时机和命中确认：** 纳入无敌帧，增加生命下降作为辅助证据，“未确认”改为可重试。
5. **补完 D019：** 接入失败处置、交战缺口在驱动层可恢复、受击上限交给策略、用枚举替换字符串控制流。
6. **然后再做战斗走位：** 导航输出世界方向，由合成器按瞄准视角换算按键，让“边走边打”真正落到战斗技能上。

[base]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/tree/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b
[bridge]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/skills/navigation_session_driver.py#L134
[propose]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/motion_nav/navigation_session.py#L699
[anchor-script]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/scripts/b10_gap_solver_runtime.py#L478
[ledger-script]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/scripts/b10_gap_solver_runtime.py#L528
[tracker-anchor]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/motion_nav/motion_residual.py#L332
[retire]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/motion_nav/navigation_session.py#L757-L780
[exec-cancel]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/motion_nav/action_route_executor.py#L351
[neutral]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/motion_nav/navigation_session.py#L693-L696
[effects]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/motion_nav/physics_1_21.py#L328
[using]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/motion_nav/physics_1_21.py#L332
[material]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/motion_nav/physics_1_21.py#L408
[detector-pending]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/motion_nav/external_motion.py#L340
[detector-drop]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/motion_nav/external_motion.py#L368
[detector-unavailable]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/motion_nav/external_motion.py#L377
[absorption-test]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/tests/motion_nav/test_external_motion.py#L59
[confirm]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/skills/fixed_melee.py#L232
[strike-fail]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/skills/moving_melee_driver.py#L533
[fd]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/runtime/failure_disposition.py
[seal]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/runtime/player_runtime_v1.py#L375-L376
[arb-attack]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/runtime/arbiter_v1.py#L139
[arb-look]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/runtime/arbiter_v1.py#L148
[nav-look]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/motion_nav/navigation_session.py#L1055
[client-reject]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorExecutor.java#L302
[client-clear]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorExecutor.java#L209
[strings]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/skills/moving_melee_driver.py#L505-L533
[admit-strings]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/motion_nav/navigation_session.py#L962
[adapter]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/motion_nav/navigation_session.py#L261
[world-owner]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/motion_nav/runtime_adapter.py#L170
[per-trial]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/scripts/c1_moving_melee_runtime.py#L319
[recovery-fail]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/skills/moving_melee_driver.py#L694
[engagement]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/skills/engagement_memory.py#L245
[fact-none]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/skills/moving_melee_driver.py#L757
[support]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/1c6a0defaa5bb7b9483ae90a7ca8dc0f0a0dd67b/mc2p/skills/moving_target.py#L54-L55
