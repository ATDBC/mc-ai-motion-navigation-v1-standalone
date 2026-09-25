# C1-R：公共控制与导航收敛实施方案

日期：2026-09-24
状态：已完成；R0 至 R5 均已通过

## 目标

修正 C1 审查发现的事实错误，并把战斗接近、运动导航和逐帧控制收敛到一条正式主线。完成后，战斗可以通过新导航会话使用已验证的地形动作；移动、转头和攻击可以在同一帧按兼容规则组合；外力和运行时失败不再依赖冻结验收中的偶然前提。

本阶段不新增完整战术系统。它先让后续能力有清楚、稳定的公共入口。

## 方案编写时的基线

- 2026-09-24 在当前 Windows 工作区运行 `tests/motion_nav`，345 项通过；
- 同日运行 C1 与外部运动直接相关的 72 项测试，全部通过；
- 外部审查在 Linux Python 3.11 环境运行公开快照时，运动／导航 338 项中有 1 项因墙钟 P95 超过 8 ms 失败；这说明冻结场景的功能回归基本稳定，但普通单元测试中的机器时间断言不具备跨机器可重复性；
- 公开仓库仍有四个测试模块缺失、`psutil` 未声明和 Java 路径依赖本机布局的问题。当前公开快照不能据此宣称“没有剩余阻断问题”。

这些结果只作为修复前基线。后续测试数量变化时，必须说明新增、删除或重分类的原因，不能只比较通过数。

## 实施顺序

### R0：修正事实、具体缺陷和基线

**完成情况（2026-09-24）**

- 正式自身观察已增加客户端读取的实际眼高，并保留旧观察记录缺少该字段时的兼容读取；
- 固定攻击和重新找回目标已共用同一个三维瞄准函数，目标中心由相对位置和碰撞箱高度计算；
- 站立、潜行、目标高低变化和重新找回目标的方向测试已经覆盖；
- 目标受伤动画代表原版伤害无敌时间仍在继续。C1 会等动画归零后再提交下一击；上一击可由生命下降、明确死亡或新的受伤变化确认；
- 恒真的世界会话分支已经删除。世界会话、目标身份、距离、准星和冷却门禁没有放宽；
- 120 项 C1、观察契约和 Java 来源检查通过，345 项 `tests/motion_nav` 回归通过；
- 独立 Fabric 客户端部署项目完成真实 Java 编译、打包和来源校验。本轮没有新增游戏内战斗实测，原 C1 正式统计保持不变。

**修改范围**

- `mc2p/skills/melee_strike_driver.py`
- `mc2p/skills/moving_melee_driver.py`
- `mc2p/skills/fixed_melee.py`
- `mc2p/contracts/observation_v2.py`
- `mc2p/backends/runtime_overlays/mc121_observation/ClientObservationCollector.java`
- C1 架构、阶段和验收文档
- `tests/test_melee_strike_driver.py`
- `tests/test_moving_melee_driver.py`

**工作**

1. 把目标中心、玩家眼高、yaw 和 pitch 计算收进一个纯函数。正式自身观察增加当前姿态对应的眼高，客户端直接读取玩家实际眼高；测试不能继续写死 1.62。
2. 修复重新找回目标时向上看的俯仰角错误，增加同高、目标更高、目标更低、站立和潜行用例。
3. 删除 `moving_melee_driver.py` 中恒真的条件和只影响可读性的重复分支。
4. 把“是否允许再次攻击”和“上一击是否已经确认”分开。受伤动画归零控制下一击时机；生命下降、死亡和攻击后的新变化用于确认上一击。
5. 修改 C1 文档：C1 已验证共享观察、仲裁、输入和回执，没有验证战斗已接入 `motion_nav` 导航主线。
6. 修改交战记忆文档：当前隐藏位置授权来自“先直接看见并确认机器人命中后的报复规则”，不是客户端直接观察仇恨目标。
7. 保留现有正式运行和失败证据，不重写 C1-A 至 C1-C 的统计结果。

**进入 R1 的条件**

- 新瞄准测试先失败后通过；
- 队友造成受伤动画、目标着火和上一击仍在确认时，攻击许可与命中归因得到各自明确结果；
- C1-A、C1-B、C1-C 的现有单元和重放回归通过；
- 文档不再声称 C1 已经验证新的运动导航主线。

### R1：Runtime 独占逐帧推进

**完成情况（2026-09-24）**

- Runtime 已增加公共控制帧入口。多个技能先提交意图和观察需求，再由 Runtime 合并并只推进一次后端；
- 点目标、近战、移动近战和外部运动恢复驱动已停止直接调用 `Runtime.step()`；包边界测试会阻止这些调用重新出现；
- `attack_entity` 已能与移动和转头共存。攻击冷却门槛由动作明确声明，C1 继续要求满冷却；
- Fabric 客户端会在玩家实际输入采样前重新检查目标身份、准星、距离、冷却、界面和世界状态。攻击与移动进入同一个客户端采样边界后才返回回执。只与攻击有关的门禁失败时返回 `operation_rejected` 并保留已经接纳的移动；整帧条件失败时仍拒绝完整动作；
- 实机场景覆盖前进、后退、左移和右移，每个方向 10 次。40／40 都命中指定目标，并取得同请求、同客户端 tick 的移动和攻击证据；错误目标、重复攻击和输入所有权违规均为 0；
- 123 项 R1 与 C1 直接回归通过，138 项扩大后的 Runtime、观察请求、Fabric 和部署检查通过；独立 Fabric 客户端重新编译、打包和来源校验通过；
- 正式运行 ID 为 `20260924T124051062211Z-4792742d`。诊断流无交付缺口，运行期间源码和启动输入未变化。

本轮只证明公共控制帧和固定目标组合输入。战斗接近仍未迁移到新运动导航主线，这项工作属于 R2。

**修改范围**

- `mc2p/runtime/player_runtime_v1.py`
- `mc2p/runtime/arbiter_v1.py`
- `mc2p/contracts/action_v1.py`
- `mc2p/contracts/intent_source.py`
- `mc2p/skills/point_goal_driver.py`
- `mc2p/skills/melee_strike_driver.py`
- `mc2p/skills/moving_melee_driver.py`
- `mc2p/skills/external_motion_recovery_driver.py`
- `mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorExecutor.java`
- 对应 Runtime、仲裁、Java 门禁和技能测试

**工作**

1. 增加 Runtime 控制帧协调入口。它取得一次正式观察，调用当前技能的观察和提议步骤，合并观察请求，然后只调用一次后端。
2. 逐个移除子驱动器对 `Runtime.step()` 的调用。迁移期间保留适配器，但包边界测试禁止新代码调用旧入口。
3. 让仲裁器按操作类型检查兼容关系。`attack_entity` 可以与移动和视角共同存在；界面、容器、挖掘保持现有限制。
4. Java 客户端接受“移动＋视角＋攻击”动作快照，并在同一个运动 tick 记录控制应用和攻击分发。目标身份、准星、距离和界面仍由客户端核验。攻击冷却门槛改成候选显式声明的能力条件；通用协议不再永久要求满冷却，C1 仍声明满冷却近战。
5. 父战斗任务不再因为攻击帧存在移动意图而封存 Runtime。
6. 观察在每帧由 Runtime 交给交战记忆、外力检测和导航适配器。某个技能不能独占一帧观察。

**进入 R2 的条件**

- 每个客户端运动 tick 最多一次后端 step；
- 相同输入集合无论技能注册顺序如何，仲裁结果一致；
- Fabric 专项证明机器人移动或横移时能够合法命中固定目标；
- 空中动作责任测试证明组合操作不会替换落地责任方；
- 现有 C1 单独瞄准、单独攻击和取消场景不退化。

### R2：建立 `NavigationSession` 并迁移 C1

**完成情况（2026-09-24）**

- B10 脚本内的规划、接纳、动作准备和执行编排已迁入正式 `NavigationSession`；固定地面、台阶和同高一格跨隙共用同一入口；
- C1 接近与追击已通过 `RuntimeNavigationDriver` 使用该会话。目标移动时更新同一目标的修订和区域，不再创建旧 A–E 导航状态；
- 会话只提交导航意图，由 R1 公共控制帧与视角和攻击一起仲裁。正式 C1 模块的包边界检查会拒绝重新导入旧导航；
- 战斗导航桥每帧从正式观察和 Runtime 输入账本取得状态锚点，再交给会话验证动作；跨隙不会再因空锚点和空账本停在等待验证；
- 目标修订或取消不会立即丢弃执行器。旧路线安全前段继续到新路线接管；若身体在空中，原执行器先完成取消和落地尾段；
- 同一观察不会重复写入世界知识；活动路线执行期间不会每帧轮询后台规划；几何查询在单帧内复用结果；固定路线候选采用保持精确结果的分支限界，开放地形不再完整扫掠所有劣势候选；
- 诊断记录改为有界异步写盘。控制线程只提交不可变记录；队列容量、丢弃数和写入失败都进入正式证据；
- 94 项 R2、B07、B09、B10 和移动战斗直接检查通过；
- C1-A 正式运行 `20260924T151034294227Z-f98bff78` 通过 40／40 正例和 7／7 反例；
- C1-B 正式运行 `20260924T150600298032Z-bb11a149` 通过 20／20 正例和 10／10 反例，整帧控制 P95 为 5.6107 ms、P99 为 7.5420 ms；
- C1-C 正式运行 `20260924T152120376309Z-de8e8578` 通过 20／20 正例和 10／10 反例，整帧控制 P95 为 4.4822 ms、P99 为 6.2036 ms，恢复输入实际生效 P95 为 48.9272 ms；
- 三轮正式记录和诊断记录均无丢弃，工作线程无失败，离线重放、来源校验和清理全部通过。

首轮 C1-C 复测 `20260924T151401396904Z-9b317f6e` 保留为验收脚本缺陷证据。该轮行为全部完成，但脚本把“本帧先执行路线输入、随后才从返回观察发现受击”误记成受击后的旧路线接管。修正后，只有在发现受击之后的后续帧继续选择旧来源才算违规；正式复测的旧路线接管为 0。

**新建**

- `mc2p/motion_nav/navigation_session.py`
- `tests/motion_nav/test_navigation_session.py`
- `tests/test_c1_navigation_session.py`

**修改**

- `mc2p/motion_nav/runtime_adapter.py`
- `mc2p/motion_nav/planner_worker.py`
- `mc2p/motion_nav/route_admission.py`
- `mc2p/motion_nav/motion_coordination.py`
- `mc2p/motion_nav/action_route_executor.py`
- `mc2p/skills/moving_melee_driver.py`
- `scripts/b10_gap_solver_runtime.py`
- `scripts/c1_moving_melee_runtime.py`
- `docs/motion_navigation/architecture/package-boundaries-v1.md`

**工作**

1. 把 B10 脚本中的规划、接纳、动作准备和执行编排迁入 `NavigationSession`。
2. 使用现有 `PlanningRequest`、`GoalState`、`ActiveRoute` 和 `StateAnchor`，不建立第二套目标几何和身体状态。
3. C1 将动态攻击站位转换为同一个目标 ID 的新修订和 `GoalState.region`，通过会话更新目标。
4. 会话只返回导航意图和有类型报告，由 R1 控制帧统一提交。
5. 正式 C1 路径停止创建 `PointGoalPolicy("D")` 和旧 `NavigationState`。
6. 增加包边界检查：`moving_melee_driver`、新的任务入口和 `motion_nav` 正式模块不能导入旧 A–E 导航。
7. 旧导航保持参照身份。完成调用清点前不删除，也不移动冻结证据依赖的文件。

**进入 R3 的条件**

- 固定目标、移动目标、目标修订、取消和恢复后重规划都通过 `NavigationSession`；
- 平地 C1-A/B/C 实机结果不低于原冻结门槛；
- 组件场景证明会话可以执行含台阶和同高一格跨隙的路线；
- 世界、目标或请求变化后，迟到结果不能接管身体；
- 正式 C1 运行中没有旧导航内存和 `WorldKnowledge` 两份地形事实。

### R3：世界分区、外力残差和常驻失败处理

**完成情况（2026-09-24）**

- `WorldKnowledge` 已按 16×16×16 区段保存事实，并把已知格总数限制为 262,144。机器人周围 32 格和活动路线依赖受到保护；无法在上限内接纳新事实时返回有类型结果，不再静默扩容或删除在用事实；
- Runtime 现在长期拥有真实输入账本。账本记录实际生效、被后续命令覆盖和存在歧义的 tick；运动残差从状态锚点、这份账本和 B09-R 计算器得到，不再用“观察到伤害”代替“身体发生了预测外运动”；
- 伤害事实已同时检查生命和吸收心。伤害事实与覆盖伤害 tick 的完整运动残差同时成立时形成 `DAMAGE_KNOCKBACK`；计算器因材质、姿态或状态明确返回不支持时形成 `DAMAGE_WITH_UNVERIFIED_MOTION` 并保守恢复，但不宣称已证明击退；没有伤害来源的预测外运动单独报告为 `UNATTRIBUTED_EXTERNAL_MOTION`；
- 残差所需的少量未知格会进入下一帧观察请求，并优先于规划所需的大批量查询。残差最多跨 8 个运动 tick 重放；超过窗口后明确重新锚定，不能无限追赶旧观察；
- 已知安全支撑上的可恢复偏移继续由同一个 `NavigationSession` 从新连续状态接管。身体离地、支撑不明或原入口已经越界时，仍由恢复控制器完成落地或有界制动；
- 异步记录的投影和写盘已移出控制线程。线程失败会让正式证据降级，但不会断开客户端；Runtime 的失败处置已改成有类型枚举，世界倒退、输入所有权冲突和可重试缺口不再依赖字符串判断；
- 交战观察出现短暂缺口时，仍保留目标身份和此前的新鲜事实。依赖连续观察的攻击资格会等待新观察，目标不会仅因一次缺包永久消失；
- 确定性容量检查连续执行 36,000 次更新，等于 20 Hz 下 30 分钟的更新数量。该检查证明已知格、输入账本、后台请求和日志队列有上限，但不等同于真实客户端连续运行 30 分钟；
- C1-B 正式回归 `20260924T165055740627Z-779c20b2` 通过 20／20 正例和 10／10 反例。2,022 个完整正常运动残差没有产生外力事件；位置误差最大 0.015408 格，速度误差最大 0.0000051 格／tick；整帧控制 P95 为 5.3653 ms、P99 为 7.1321 ms；
- C1-C 正式回归 `20260924T164611255023Z-db8461d6` 通过 20／20 正例和 10／10 反例。24 次外力恢复全部完成；整帧控制 P95 为 5.1863 ms、P99 为 7.5030 ms，恢复输入实际生效 P95 为 48.872 ms，单次恢复最多 12 tick；
- C1-C 调试期间的失败运行继续保留。它们依次暴露了输入采样缺少对应命令、租约尾部账本未标为已覆盖、残差查询被规划查询挤压，以及重复检查提前清除待查询格。这些问题都先由失败检查固定，再完成修正；
- 离线重放会重建伤害归因、事件去重、责任交接和恢复状态机。它读取实机日志中已经计算出的运动残差，不会独立重演服务器物理，因此不能替代 B09-R 和 Fabric 实机证据。

计算器返回“缺少世界格”或输入账本不完整时，系统仍等待证据或重新锚定，不使用保守受击兜底掩盖缺信息。圆石、沙子、深板岩、进食和部分状态效果目前仍未获得完整运动模型；本轮修正的是受击后不再失去身体恢复，并未把这些条件声明为精确可预测。

**新建**

- `mc2p/motion_nav/motion_residual.py`
- `mc2p/runtime/failure_disposition.py`
- `tests/motion_nav/test_motion_residual.py`
- `tests/test_runtime_failure_disposition.py`

**修改**

- `mc2p/motion_nav/world_model.py`
- `mc2p/motion_nav/external_motion.py`
- `mc2p/motion_nav/external_motion_recovery.py`
- `mc2p/motion_nav/physics_1_21.py`
- `mc2p/motion_nav/online_motion.py`
- `mc2p/runtime/player_runtime_v1.py`
- `mc2p/runtime/async_trace.py`
- `mc2p/skills/engagement_memory.py`
- `mc2p/skills/moving_melee_driver.py`

**工作**

1. 将世界事实按 16×16×16 区段保存，候选记录实际依赖区段。远处无关更新不再让所有视图失效。
2. 实现 32 格受保护范围、活动路线依赖保护和 262,144 已知格上限。容量不足返回有类型结果。
3. 伤害检测同时比较生命和吸收心，并把伤害事实与外部运动事实拆开。
4. 从状态锚点和真实输入账本计算下一观察的运动残差。先用离线影子和固定 Fabric 样本冻结阈值，再允许它触发恢复。
5. 外力后从新的连续状态重新接入 `NavigationSession`。路线和能力入口仍成立时直接续行；只有空中责任、路线危险或入口超界时才落地或制动，不能把“恢复”固定成“停稳”。
6. 把第五次受击等任务级耗尽结果返回战斗策略。单次恢复的 tick 上限和重复同因防循环仍保留。
7. 增加错误处置枚举，替换决定权限和生命周期的 `reason` 字符串比较。
8. 异步日志在控制帧只接收最小不可变事件；投影和写盘移出控制线程。队列必须有上限、丢弃计数和证据降级规则，不能每帧同步投影整份观察。
9. 观察缺口让依赖连续证据的模块等待重新观察，不自动永久撤销仍有身份和新鲜事实的目标。

**进入 R4 的条件**

- 吸收心受击、着火／中毒伤害、摔伤、普通移动偏差和真实击退得到不同分类；
- 影子预测在冻结正常运动样本中不误报外力；
- 已知安全走廊内的可恢复击退能够从新锚点续行，不机械要求速度归零；超出能力入口时仍会安全落地或制动；
- 日志线程失败不会断开客户端，也不会被计为完整成功证据；
- 观察缺口后能够重新锚定，旧候选仍按身份和依赖失效；
- 30 分钟容量场景中世界事实、日志、输入账本和后台队列都保持有界。

### R4：带速度 `JumpGap` 试点

**完成情况（2026-09-25）**

- 新的跨隙求解策略接受朝向缺口、水平速度为 0.5–3.0 格／秒的连续入口。它只搜索 12 个有上限的真实命令模板，没有扩大 B09 的旧低速 Profile；
- Walk 接近跨隙时会保留仍在新策略入口内的速度，并把身体状态交给后台求解器。求解结果过期时只从新锚点重放同一组命令并重新验证，不在控制线程重新搜索；
- 每个允许的首条命令生效 tick 都有独立证明。晚一 tick 生效时，证明包含前一 tick 的空输入、变化后的入口状态、完整轨迹、落点和每个释放时刻；
- 求解等待期间，会话继续取得不带新输入的正式观察，使状态锚点随客户端运动 tick 前进。等待期限和首条命令的执行窗口分别管理；
- 恢复或结束时发出的中性输入不再冒充证明中的下一条命令。只有实际属于不可变证明的命令才登记逐 tick 回执；
- 83 项 R4 直接组件检查通过；加入导航会话等待语义后，64 项求解、执行、后台工作进程和会话集成检查通过；
- 正式 Fabric 运行 `20260924T190335141223Z-a31d66d0` 通过：旧跨隙 40／40、转弯出口 40／40、三个速度带四个方向共 60／60、反例 24／24、默认后台协调 10／10；
- 142 次求解的 P95 为 27.8819 ms、P99 为 27.9836 ms、最大值为 28.0456 ms，低于冻结的 30 ms 求解门槛；20 项运行时、输入证据、时钟归因和来源不变检查全部通过。

调试中的失败运行继续保留：

- `20260924T184028642222Z-82a2c0f0` 暴露了执行窗口只验证最早开始 tick 的问题。首条命令晚一 tick 生效后，空输入改变了入口速度，机器人虽然安全落地，却没有落入原证明的出口；
- `20260924T185411232963Z-40f45007` 暴露了后台等待期间没有刷新正式观察，以及恢复中性输入被错误登记为证明命令的问题；
- 更早的失败分别暴露旧输入账本、观察过期、旧意图容量、材质许可、观察范围和大型时间记录导出问题。它们没有从实验产物中删除。

**修改范围**

- `mc2p/motion_nav/motion_solver.py`
- `mc2p/motion_nav/motion_candidate.py`
- `mc2p/motion_nav/motion_coordination.py`
- `mc2p/motion_nav/action_route_executor.py`
- `config/motion-navigation/air-motions-b09-v1.json`
- B09-R、B10-B、B10-C 的求解和执行测试

**工作**

1. 保留 `JumpGap` 动作边及其入口、落地和恢复语义。
2. 把入口连续速度放入求解请求。候选只从有限的保持、释放、跳跃和方向模板产生。
3. 用 B09-R 验证完整轨迹、每个中断时刻的安全落地和后续动作入口。
4. 覆盖 0.5–1.0、1.0–2.0、2.0–3.0 格／秒三个入口速度带和四个正方向。
5. 旧低速 Profile 继续作为参照。新证据没有通过时，正式能力不得扩大入口范围。

**进入 R5 的条件**

- 每个冻结入口带都有求解成功、明确拒绝和中断安全证据；
- 求解 P99 和端到端首个输入应用仍在 B10 已冻结预算内；
- Walk → JumpGap 不因接口交接机械停稳；
- 不新增逐动作两两接续分支；
- 原低速 B09/B10 场景保持通过。

上述条件已经满足。R5 只处理完整回归、可重复公开快照和 GitHub 同步，不再扩大运动能力范围。

### R5：完整回归、公开快照和 GitHub 同步

**完成情况（2026-09-25）**

- 建立了由固定清单驱动的源码导出器。它只允许清理工作区 `.tmp` 的明确子目录，并生成来源提交、清单哈希、逐文件 SHA-256 和工作树状态；
- 公开仓库的 `.gitattributes` 禁止 Git 改写文件字节。Windows 本地发布树与 GitHub 干净克隆的根目录树 SHA 完全一致；
- C1 固定近战只依赖带来源哈希的 11 项精简控制能力，不再依赖未公开的 315 KB 原始实验产物；实时导航演示只在实际启动时查找本地 Gradle，导入和离线测试不再依赖缓存；
- 主工作区和独立导出树的 `tests/motion_nav` 均为 406／406，通过 README 声明的 C1 检查 42／42、公共控制与战斗检查 140／140、纯 Java 门禁 1／1；
- 从 GitHub 默认分支重新干净克隆后，再次得到 406／406、42／42、140／140 和 1／1。哈希检查报告 582 个受检文件，运行检查后仓库保持干净；
- 普通单元测试已停止用当前机器的墙钟 P95／P99 判定功能正确性。原毫秒门槛和 R4 正式 Fabric 样本继续保留，不因这次调整而放宽；
- 公开仓库只包含实现源码、配置、测试、精简文档和门禁。世界、完整轨迹、日志、缓存、构建产物及本地参考文档没有上传。

公开仓库根目录的 `EXPORT-METADATA.json` 记录准确的主项目来源提交；`SHA256SUMS.txt` 记录可克隆文件的字节哈希。远端默认分支只有在干净克隆通过上述命令后才视为同步完成。

**新建**

- `config/motion-navigation/standalone-export-v1.json`
- `scripts/export_motion_navigation_standalone.py`
- `tests/motion_nav/test_standalone_export.py`
- `tests/test_standalone_java_gates.py`
- `packaging/motion-navigation-standalone/` 下的公开仓库入口文件

**修改**

- `tests/motion_nav/test_public_repository_completeness.py`
- `tests/motion_nav/test_fixed_route_walk.py`
- `docs/motion_navigation/acceptance/B03-fixed-route-walk.md`
- C1 固定近战配置来源
- 独立仓库 `README.md`、`AGENTS.md` 和 `SHA256SUMS.txt`

**工作**

1. 把依赖机器墙钟的 P95/P99 断言移出普通单元测试。单元测试保留确定性的计算量、状态和上限检查；固定环境基准和正式验收继续检查毫秒预算。
2. 建立可重复导出脚本。导出清单明确包含正式源码、共享契约、配置、测试夹具、Java 门禁、四类文档和精简正式报告。
3. 在临时目录检查 Python 内部导入闭合。审查指出缺失的 `test_navigation_motion`、`test_craftground_backend`、`test_craftground_runtime`、`test_visible_equipment_projection` 必须随真实依赖导出，或删除对它们的错误引用；不得建立不能运行的空壳测试。
4. 公开仓库声明测试依赖，包含当前确实需要的 `psutil`。Java 检查通过 `JAVA_HOME` 或 PATH 查找 JDK 21，不再写死 Windows `.venv/Library/bin/javac.exe`，README 写明构建步骤。
5. 在只有导出内容和声明依赖的环境中运行：编译检查、`tests/motion_nav`、C1 相关测试、Java 输入门禁和哈希校验。
6. README 删除“没有剩余阻断问题”等已经被新审查推翻的绝对表述，列出当前已通过范围、未完成范围和准确命令。
7. 从 `ATDBC/mc-ai-motion-navigation-v1-standalone` 默认分支干净克隆后重复同一检查。远端树与本地导出树哈希一致后，才记录同步完成；外部审查分支不作为发布来源，也不覆盖其内容。
8. 不上传世界存档、完整原始轨迹、日志、缓存、构建目录和本地无关参考文档。

### 复审整改（2026-09-25）

三方复审在平地冻结范围之外发现四个高优先级问题。本轮按依赖顺序完成修正：

1. 战斗导航桥接入会话已有的状态锚点和 Runtime 输入账本；
2. 目标修订和取消期间保留当前执行器，直到安全前段完成或身体责任明确交接；
3. 受击时若物理模型明确不支持当前材质、姿态或状态，以 `DAMAGE_WITH_UNVERIFIED_MOTION` 进入保守恢复；
4. 近战避开目标受伤无敌时间，命中不确定时有界重试并返回任务策略。

随后补齐了失败处置、第五次外部运动事件、短暂观察缺口和移动攻击组合。普通地面 Walk 允许战斗视角与导航视角在 5° 内共存；精确朝向的台阶和空中动作没有放宽。攻击操作的门禁拒绝不再清空同帧已经接纳的导航移动。

正式 Fabric 复测如下：

| 场景 | 结果 | 关键数据 |
|---|---:|---|
| C1-A `20260925T010639812160Z-46d4f9d7` | 40／40 正例、7／7 反例 | 攻击专属拒绝返回 `operation_rejected` |
| C1-B `20260925T015540321761Z-d008bbc3` | 20／20 正例、10／10 反例 | 部署总状态通过；控制 P95 6.7240 ms，P99 8.2514 ms；重放 2,888 个决策一致 |
| C1-C `20260925T013056709842Z-97bc0db8` | 20／20 正例、10／10 反例 | 控制 P95 6.5571 ms，P99 7.6251 ms；恢复响应 P99 49.2964 ms |

C1-B 的运行 `20260925T012052536298Z-a6847277` 在游戏试次全部通过后，原部署包装器因合法空位置事实崩溃。修复重放器后，同一份不可变 trace 的 2,855 个决策全部一致。运行 `20260925T014651611158Z-8fbbd0bb` 随后暴露了另一个边界：单次攻击遇到一帧目标视觉缺口时，外层战斗错误地把整个任务判为失败，结果为 19／20 正例。修正后，攻击尝试结束，但有效交战记忆会把控制交回重新观察流程；身份失效和目标明确不可用仍直接失败。两份失败证据都保留，最终运行不靠删样本或重新分类通过。

本轮未新增圆石、沙子、深板岩和状态效果的 Fabric 正式场景。它们的安全兜底由确定性组件检查覆盖；完整运动预测和对应实机范围留待材质与状态能力阶段。

## 提交顺序

每个子阶段单独提交，不能把五个阶段压成一个大提交：

1. `fix: correct C1 aiming and scope records`
2. `refactor: centralize one control frame`
3. `refactor: route combat through navigation session`
4. `fix: separate damage motion and runtime failure handling`
5. `feat: solve bounded moving jump gap entries`
6. `test: make standalone snapshot self-verifying`

每次提交只包含该子阶段文件。现有未跟踪的 `docs/reference/MC 伙伴运动层：战斗切片.md` 不属于本方案提交，除非用户另行要求。

## 停止条件

任何子阶段出现以下情况时，不继续叠加下一阶段：

- 旧动作失去唯一身体责任；
- 同一运动 tick 出现两个后端 step 或两个输入写入者；
- 正式任务重新建立第二份世界知识；
- 为通过测试而放宽未知、身份、目标修订或输入回执要求；
- 实机失败无法归因到观察、候选、仲裁、输入应用或运动结果中的一层；
- 公开快照只能依靠原仓库未声明文件运行。

## 检查命令

下面命令在对应子阶段完成后运行。新增测试文件在该子阶段提交中创建，不能提前放置空测试壳。

### R0

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_fixed_melee tests.test_fixed_melee_driver tests.test_melee_strike_driver tests.test_moving_melee_driver tests.test_engagement_memory tests.test_c1_fixed_melee_runtime tests.test_c1_moving_melee_runtime tests.test_c1_external_motion_runtime -v
```

预期：全部通过；新测试能在修复前稳定暴露俯仰角和姿态眼高错误。

### R1

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_action_arbiter_v1 tests.test_player_runtime_v1 tests.test_client_behavior_executor tests.test_client_behavior_input tests.test_moving_melee_driver tests.test_external_motion_recovery_driver -v
```

预期：全部通过；新增断言证明一帧只有一次后端推进，移动、转头和攻击形成同一个合法动作快照。

### R2

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_navigation_session tests.test_c1_navigation_session tests.motion_nav.test_planner_worker tests.motion_nav.test_b07_step_route tests.motion_nav.test_b09_air_transitions tests.motion_nav.test_b10_motion_candidate tests.motion_nav.test_b10_motion_worker tests.test_moving_melee_driver -v
```

预期：全部通过；结构检查确认正式 C1 入口不再依赖 `PointGoalPolicy`、旧 `NavigationMemory` 或 A–E 导航。

### R3

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_motion_residual tests.motion_nav.test_external_motion tests.motion_nav.test_external_motion_recovery tests.test_runtime_failure_disposition tests.test_engagement_memory tests.test_async_trace tests.motion_nav.test_runtime_adapter -v
```

预期：全部通过；伤害、吸收心、预测残差、日志故障和观察缺口分别得到冻结结果。

### R4

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_b09r_physics_rollout tests.motion_nav.test_b10_gap_solver tests.motion_nav.test_b10_motion_candidate tests.motion_nav.test_b10_motion_worker tests.motion_nav.test_b09_air_transitions -v
```

预期：全部通过；三个非零入口速度带都有求解、拒绝和中断安全样本。

### R5 本地完整检查

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest discover -s tests/motion_nav -p 'test_*.py' -v
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_c1_melee_evidence tests.test_c1_moving_melee_evidence tests.test_c1_external_motion_evidence tests.test_c1_fixed_melee_runtime tests.test_c1_moving_melee_runtime tests.test_c1_external_motion_runtime -v
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_standalone_export tests.motion_nav.test_public_repository_completeness -v
git diff --check
```

预期：全部通过，`git diff --check` 没有输出。

### R5 独立仓库检查

导出脚本在固定检查目录创建临时树后运行：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/export_motion_navigation_standalone.py export --root .tmp\standalone-export-check --clean
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/export_motion_navigation_standalone.py verify --root .tmp\standalone-export-check
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest discover -s .tmp\standalone-export-check\tests\motion_nav -p 'test_*.py' -v
```

`.tmp\standalone-export-check` 必须由脚本解析为当前工作区内的固定临时目录；`--clean` 只能清理这个精确目录，不能接受工作区根目录或其父目录。干净克隆远端仓库到另一个固定临时目录后，对克隆目录重复 `verify` 和测试命令。

## 相关文档

- [D019：先收敛公共控制和导航主线](../decisions/0019-converge-runtime-navigation-before-new-capabilities.md)
- [运行时与导航收敛架构](../architecture/runtime-navigation-convergence-v1.md)
- [C1-R 验收计划](../acceptance/C1R-runtime-navigation-convergence.md)
- [现有总架构](../architecture/mc_motion_navigation_architecture_v1.md)
- [B10 架构](../architecture/B10-motion-solving-continuous-execution-v1.md)
