# D086：用有界多 tick 序列求解普通地面窄终点

日期：2026-10-08。状态：已实施到正式隔离 worker 门槛并失败，F2-GP 不得接入。

## 要解决的问题

F2-SS 已让正式普通 `STANDING + WALK` 每帧只生成一份物理报告。它恢复了两个历史退步，也让 remaining9 达到 `6/9`、remaining42 达到 `42/42`。

v8 仍有三个冻结的旧成功退步。正式 1.21 物理显示，机器人最终停在窄 completion 外 `0.0061—0.0229` 格。九键的一 tick输入粒度太粗：继续按键会越过目标，松键又停在目标外。

这不是安全规则过严。当前候选的碰撞、支撑和停止尾迹都安全。缺少的是一段能同时安排推进、制动和微调的多 tick 输入。逐帧只比较下一次一／两 tick输入，看不到“先向一侧制动，再沿另一轴修正，最后松键”的组合。

## 决定

为普通地面窄 completion 增加一个有界末段求解器。它只处理 `STANDING + WALK`，使用现有 1.21 运动计算器搜索短输入序列。

求解器分两层：

1. 先按固定顺序搜索一到三个阶段的通用运动基元；
2. 第一层没有结果时，使用宽度 `512` 的有界 beam search 兜底。

这里的“阶段”是一小段目的明确的连续输入。基元只有：

- 沿 X 轴接近目标；
- 沿 Z 轴接近目标；
- 沿 X 轴制动；
- 沿 Z 轴制动；
- 同时处理两个轴的合法组合；
- 松开方向键的中性输入。

阶段数量按 `1 → 2 → 3` 搜索。每个阶段的持续时间按 `1..remaining_ticks` 递增枚举，总长度不超过请求里的 `maximum_ticks`。`maximum_ticks` 必须位于 `1..64`，并在运行 oracle 前冻结。基元、组合和持续 tick 的遍历顺序固定。求解器根据身体与 completion 的相对关系生成按键，不读取场景 ID、种子、东西南北名称或人工方向表。

## 为什么保留两层搜索

阶段搜索覆盖常见的“接近—制动—微调”。它的候选少，容易解释，也适合先进入正式链。

beam search 只处理阶段模板没有覆盖的入口速度与双轴耦合。它使用同一组合法运动输入和同一个物理计算器，不引入第二套物理规则。宽度固定为 `512`；达到节点、深度或墙钟预算时，返回类型化的预算耗尽，不公开尚未证明完整的候选。

beam search 只有在预计执行点至少还有 `11 tick` 时才可提交。这个提前量给最多 `500 ms` 的后台计算和下一 tick提交各留出明确位置。提前量不足时不能在控制线程补算，也不能把预算耗尽写成无路。

## 正式结果

求解器只发布不可变的 `GroundTerminalSequence`。结果至少绑定：

- `StateAnchor`；
- 完整 `AsyncWorkIdentity`；
- goal ID 与 goal revision；
- route ID、route revision 和动作位置；
- `CandidateExecutionWindow`；
- 从正式锚点到执行窗口的有界准备前缀，以及 normal／late1 对应的预计入口状态；
- 固定的逐 tick 命令；
- 按时启动和晚一 tick启动的两条预测轨迹；
- 两条轨迹上每个已执行前缀对应的完整中性停止尾迹；
- 最终 completion、姿态、模式、支撑和速度结论；
- 两条启动分支与全部安全尾迹读取的世界依赖；
- 预计执行 tick 成本、求解层级和稳定身份。

按时与晚一 tick两个分支都满足硬安全和最终 completion，结果才可执行。任一前缀都必须能通过已保存的中性尾迹安全收尾。只证明最终落点、只证明按时启动或只证明完整执行都不够。

## 执行方式

末段序列仍属于可恢复的普通地面控制。它不取得空中动作的不可逆责任。

执行器逐 tick 核对正式观察、实际应用账本、当前启动分支、世界依赖和预测容差。核对通过后才提交下一条命令。身体、输入或世界超出结果适用范围时：

1. 停止继续播放旧序列；
2. 只用旧前缀尾迹的长度限定恢复期限；实际输入必须从当前观察重新验证中性或地面恢复动作；
3. 从新观察重新锚定；
4. 需要时提交新的有界求解。

不能跳到旧序列后面的某一条命令继续执行，也不能为了追上原计划扩大输入窗口。

## 后台工作与身体责任

复用现有 motion worker、结果 inbox、工作身份和生效窗口。不新建第二个 worker 或第二套异步生命周期。

求解期间，现有 `FixedRoute` 继续拥有身体并运行已验证的普通地面闭环。Session 不增加 `WAITING_FOR_TERMINAL_SOLVE` 一类状态。结果尚未返回时，控制器只能继续执行当前已证明安全的输入，或按原规则安全停下。

如果求解目标位于未来 tick，请求必须带上由当前 FixedRoute 已证明的有界准备前缀。worker 从正式 `StateAnchor` 推演这段前缀，再开始末段搜索。不能只拿当前锚点配一个未来窗口，假设身体会自行到达入口。准备前缀实际生效与预测不符时，旧结果失效。

旧 work identity、旧 goal revision、旧 route revision、过期 `StateAnchor`、变化后的依赖或已经错过的执行窗口都会使结果失效。迟到结果直接退休，不重新贴到当前任务上。

## 与规划的关系

第一步先独立证明求解器和正式执行链。三项 F2-SS 退步、remaining9 和 remaining42 通过后，再把结果接入 F2-GP 的 `SurfaceTerminalApproachEdge`。

接入后：

- 末段边只使用有具体 `PhysicsState` 依据的序列；宽泛搜索状态不能伪造精确入口；
- `cost_ticks` 使用已证明序列的实际 tick 数；
- A* 的 `g` 值包含末段成本；
- 预算耗尽表示“本次没有算完”，不能删除这条边后宣称没有路线；
- 通用 A* 循环不按场景增加分支；
- 大图节点不运行在线十八项候选或控制线程搜索。

## 验证顺序

实施必须按下面顺序推进：

1. 冻结与三个已知退步分开的 holdout；
2. 证明删除任一保留模板的错误副本会被 holdout 或成本门槛发现；
3. 证明 phase search P95 `<50 ms`、beam fallback P95 `<500 ms`，控制线程搜索调用数为 `0`；
4. 运行三个 F2-SS 退步 oracle；
5. 运行 remaining9 和 remaining42；
6. 接入 F2-GP terminal edge 与 A* 成本；
7. 运行大集合、D061；
8. 最后运行 Fabric。

前一层失败就停止。不能用后面的大集合掩盖前面的失败。

## 实施后的更正与停止点

最初的三项 oracle、remaining9 和 remaining42 运行使用了测试用 `InlineMotionWorker`。它在 `poll_available()` 中直接执行搜索。模拟运动 tick在搜索期间不会推进，因此这些运行虽然得到 `3/3`、`9/9` 和 `42/42`，却没有证明后台结果能在真实期限内交付，也没有测到完整控制区间。这三组原始记录继续保留，但不得再作为 F2-TS 门槛证据。

复审后完成了四项安全加固：

- 偏离、依赖变化或取消后，不再无条件发送旧前缀的中性尾迹；控制器改为在当前 frame、anchor 和世界上验证中性或现有地面恢复动作；
- 无法证明当前恢复输入时，不发送新输入，并返回 typed `INPUT_LOST`；
- 安装和执行会核对 anchor phase、input projection、ruleset、schema，以及 sequence anchor 与 solve basis；
- 恢复期限取已保存前缀尾迹长度与现行最大恢复 tick的较小值。持续外力不能永久占有身体。

干净 Windows 提交 `a8cb3bfb` 使用真实 `PlannerWorker` 和 `MotionSolverWorker` 重跑第一个 oracle。控制进程 PID 为 `10304`，搜索进程 PID 为 `45828`，确认搜索没有在控制线程运行。该场景在既有 20 tick／1 秒工作期限内没有收到 beam 结果，最终以 `no_safe_ground_candidate` 有界失败。

后续性能审计确认，归档中的 `23.2694／42.7075／42.7075 ms` 混合了生产控制、模拟后端和完整夹具，不能直接与 production `8／15／30 ms` 门槛比较。该数字继续作为原始混合量尺保留，不再作为生产控制失败证据。正式阻塞仍是 beam 约需 `16 秒`、工作期限内没有交付，以及过期工作不能在搜索内部及时停止。修正见 [D087](0087-correct-terminal-search-timing-and-bound-delivery.md)。

项目已在第一个 oracle 停止。其余两个 oracle、remaining9、remaining42、F2-GP、大集合、D061 和 Fabric 均未运行。后续如继续，必须先重新设计实际 oracle 上的搜索成本与交付期限；不能恢复 Inline 结果或单纯放宽工作期限来宣称通过。

## 明确不做

- 不扩大 completion；
- 不降低支撑、碰撞、UNKNOWN、危险接触或停止尾迹门槛；
- 不调普通候选评分来制造通过；
- 不按样例 ID、种子、方位或坐标写修正规则；
- 不在控制线程执行 phase 或 beam search；
- 不加入学习模型、通用 MPC、完整 state lattice 或路线平滑；
- 不修改 Sprint、Sneak／探边、Crawl、Swim、Climb、JumpGap、JumpUp、ControlledDrop 和连续高度严格入口；
- 不回写 F2-SS、F2-GP 或其他历史阶段的失败结论。

## 与既有决定的关系

本决定保留 [D085](0085-use-one-source-for-ordinary-ground-candidates.md) 的单一普通地面物理事实源。末段求解调用同一个 1.21 计算器，不恢复简化 rollout。

本决定修订 [D080](0080-share-ground-tracking-policy-for-terminal-approach.md) 的一个边界：普通地面大部分时间仍逐帧闭环；窄 completion 可以使用后台证明的短序列。因为每个前缀都有安全中性尾迹，这段序列仍可中断，不升级为空中严格动作。

[D079](0079-model-terminal-approach-as-a-real-search-edge.md) 中“末段成本必须进入 A*”继续成立；入口不能由宽泛状态随意代表的限制也继续成立。

实施与关闭条件见 [F2-TS 阶段](../stages/F2TS-bounded-ground-terminal-sequences.md)和 [F2-TS 验收](../acceptance/F2TS-bounded-ground-terminal-sequences.md)。
