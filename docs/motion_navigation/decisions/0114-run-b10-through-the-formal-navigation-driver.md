# D114：B10 协调器夹具必须走正式导航驱动

日期：2026-10-17。状态：**已完成并签署。** 唯一一次 Fabric 命令的原始失败保持不变；同一批原始 trace 经只读量尺修正后满足本决定的行为与安全门槛。

## 1. 要解决的问题

D113 的 10-tick 正式场景以 Runtime `deadline_exceeded` 结束。三方复核确认，直接原因不在运动控制器：B10 夹具在等待 worker 时绕过了 `RuntimeNavigationDriver`，没有撤销上一帧的移动意图。该意图的租约逐帧接近过期，最终把本帧动作期限压到约 1.4 ms。

夹具还可能保留地形准备阶段的手工移动意图。never-return 场景持续约一秒，这条约 750 ms 的旧租约也会成为错误期限来源。

D113 的失败记录保持不变。本轮新建 D114，只修正式调用路径、可执行组件验收和公开记录完整性。

## 2. B10 复用正式驱动路径

B10 单场协调器循环统一执行：

1. `RuntimeNavigationDriver.prepare_proposals()`；
2. `Runtime.control_frame()`；
3. 正常返回后立即 `RuntimeNavigationDriver.adopt_result()`；
4. Runtime 抛错时调用 `discard_prepared()`，随后保留原异常。

等待 worker 的帧也必须完整经过这条路径。驱动会在本帧没有移动声明时撤销旧会话意图。夹具不得再直接调用 `session.propose()`，不得手工登记 verified submission，也不得另算一份执行窗口。

Fabric 与组件后端必须调用同一个单场函数。只在测试里模仿驱动行为，或保留两份控制循环，都不能关闭这次缺陷。

## 3. 接管和释放规则

创建本场导航驱动前，夹具必须撤销地形准备使用的 `manual_source` 旧意图。来源仍保持注册，供后续夹具步骤继续使用。

会话来源由正式驱动登记和释放。任务终态、停止或异常时，必须核对：

- 驱动没有未配对的 prepared frame；
- Runtime 不再选择旧会话意图或旧手工意图；
- 显式释放只有在来源确实退出 Runtime 后才能记录为成功；
- Runtime close 的全局清理不能冒充会话已安全释放。

## 4. 延迟和产品行为保持不变

本轮不修改中性准备上限、重试次数、动作窗口、运动求解、伤害门槛或 Session 状态。

交付估计在路线内只增不减会造成后续动作多等约 200 ms，这是独立的产品优化。它不参与 D114。后续若处理，必须另立决定并用多动作路线重新冻结规则；本决定不预设具体更新条件。

## 5. 可执行组件门槛

组件测试使用正式 Runtime、NavigationSession、RuntimeNavigationDriver 和 B10 延迟 wrapper，实际执行提取后的单场函数。至少覆盖 baseline、5 tick、10 tick 和 never-return，并注入旧手工租约。

10 tick 可以完成，也可以得到现行规则允许的 typed 安全终态；不得因旧租约出现 Runtime `deadline_exceeded`。never-return 必须有界结束，不起跳、不离边、零伤害，并显式释放来源。

测试必须能通过变异抓到：删掉等待帧撤销、漏掉手工源撤销、绕过 `adopt_result()`、错误延迟端点、伪造释放和漏记等待帧。

## 6. 记录与公开边界

- 恢复 D113 发布时误删的三份 TP 5A-R 失败记录，保持历史字节不变；
- 把三份记录加入公开导出的必需文件清单；
- 更新打包版 AGENTS／README 到 D113，不把 D113 写成通过；
- D102 两阶段准备测试改为等待明确的当前 identity 作业，不用一次 receiver 周期猜测完成；
- D113 原证据、结论和提交不回写。

## 7. 验收和止损

组件正式调用链通过后，才运行 Windows 源树与公开快照完整检查。所有检查全绿后，D114 只运行一次完整 Fabric 批次。协调器矩阵包含 56 项：baseline 20 项、2／5／10 tick 各 8 项、never-return 4 项；其中既有 late1 的 8 项按后端能力单列，不冒充实际运行。完整 B10 命令还必须运行或复核既有 validation、turn validation、默认协调链、R4 和 typed negative，不能用 56 项协调器矩阵替代。

以下情况立即停止：

- 修复需要改变生产运动语义或放宽安全条件；
- 组件真实循环仍出现旧来源复选、Runtime 期限失败、伤害或无界等待；
- 需要改写 D113 证据才能通过；
- 第二轮限定修正后仍有清单外回归。

D110-SOLVE-01 不属于夹具修复范围，在 D114 结束后仍保持打开。连续两跳、零等待交接和新动作仍未授权。

## 8. 实施结论

B10 的协调器场景已经统一走正式驱动。每帧都执行 prepare、Runtime control 和 adopt；异常走 discard。旧手工来源在接管前清空，旧 Session 来源不会重新入选。组件和 Fabric 共用同一个单场函数。

唯一一次 Fabric 命令完成了全部登记场景。命令原始结果为 failed，因为量尺把传送复位后坐标不变的一帧 `on_ground=false` 当成“离开起点”。原始记录中的 10-tick 安全数因此是 5／8。修正后的量尺要求身体真的发生位移；对未改动的原始 trial 和 trace 只读重算后，三项误报全部消失，10-tick 为 8／8 安全有界。没有重跑 Fabric。

最终结果为 baseline 20／20、2／5-tick 16／16、10-tick 8／8 安全有界、never-return 4／4 安全有界。所有协调器场景零伤害，旧 manual／Session 意图从未获胜，来源全部显式释放。D113 仍为 FAILED／STOPPED，D110-SOLVE-01 仍打开。
