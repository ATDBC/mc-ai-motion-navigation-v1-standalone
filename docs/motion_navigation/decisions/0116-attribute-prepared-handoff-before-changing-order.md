# D116：先归因准备动作交接，再决定是否调整同帧顺序

日期：2026-10-17。状态：**D116-1 PARTIAL／STOPPED。语义依赖修复成立，连续两跳仍未交付。**

## 1. 要解决什么问题

D115 证明连续两跳会在第二跳已经准备好的情况下停住。当时的紧凑记录把原因写成“Session 申请批准早于执行器发布边界”。这项判断只来自相邻 tick 的结果，没有记录一次控制调用内部的真实顺序。

直接调整 Session、监督者和执行器的调用顺序，可能让旧动作提前交出身体，也可能在一帧里产生两条移动输入。因此 D116 先做只读归因。归因完成前不改生产交接协议。

## 2. 只读时间线

代表场景继续使用正式规划器生成的：

`WalkSegment -> JumpGapSegment -> JumpGapSegment`

测试只用进程内补丁恢复 D115 已撤回的 `prepare_upcoming_action()` 调用。此补丁只用于让第二跳证明进入正式 receiver、registry、Session、监督者、执行器和 Runtime 链，不写入生产代码。

每个归因 tick 记录：

- 控制器状态，以及本 tick 是否调用 `_advance()`；
- 执行器发布动作边界前后的值；
- prepared entry 的状态、执行窗口和真实身体是否命中入口；
- Session 收到的路线决定；
- 监督者的批准或拒绝及原因；
- 输入账本是否追到当前 movement tick，是否已经观察到落地；
- 依赖事实、依赖内容和依赖版本是否一致；
- Runtime 最终选中的移动输入及其生效 tick。

## 3. 三条归因分支

归因只允许落到以下三条分支之一：

1. **动作完成或边界发布晚了。** 身体已经满足入口，但旧控制器仍未完成，或者执行器在 Session 尝试批准之后才发布边界。
2. **真实事实还没齐。** 输入账本、着地观察或当前身体状态没有追到批准所需的 tick。此时继续等待，不能绕过事实。
3. **prepared proof 查询拒绝。** 边界和真实事实已经齐全，但入口、时序、依赖或 generation 检查拒绝了证明。此时修对应查询，不能重排整个生命周期。

## 4. D116-0 结果

结果落在第 3 条，而且修正了 D115 的根因判断。

tick 20 的实际顺序是：

1. `VerifiedMotionExecutor` 已报告 `complete`；
2. `ActionRouteExecutor._advance()` 把边界从 `None` 写成 `2`；
3. 协调器把 `prepared_action_boundary` 返回给 Session；
4. Session 调用 `stage_prepared_action()`；
5. 监督者随后检查并拒绝。

因此，现行链并不存在“Session 在边界发布前批准”的顺序错误。

拒绝来自 `DEPENDENCY_CHANGED`。第二跳的真实入口、执行窗口、账本和着地事实都已满足。依赖版本也完全相同。不同的是 `CellFact` 的观察时间戳：相同草方块从 sequence 9 刷新到 sequence 19，`stage_prepared_action()` 用完整 `CellFact` 做等值比较，把这次纯证据刷新当成了地形变化。

tick 21 的 `ENTRY_MISMATCH` 和 tick 22 的 `OVERDUE` 都是第一次误拒之后继续滑行的结果。

## 5. D116-1 的依赖权威

D116-1 以 `WorldModel.dependency_changes()` 为唯一的在线依赖结论。prepared proof 保存求解时的 semantic dependency version；实际交接时，把这组版本交给当前世界 owner 检查：

- `UNCHANGED`：依赖仍有效，可以继续检查入口、窗口和账本；
- `CHANGED`：拒绝；
- `HISTORY_INCOMPLETE`：无法证明没变，保守拒绝；
- 当前 `PhysicsWorldView` 没有 source owner：无法检查历史，保守拒绝。

缺 owner 和历史不完整首版都复用 `PreparedQueryStatus.DEPENDENCY_CHANGED`，不为这次局部修复扩充公共状态。诊断可以另记具体来源。

不能继续比较完整 `CellFact`。它同时带有：

- 方块或空气事实；
- 观察 sequence、world tick 和接收时间；
- 可视空气的取证时间与观察条件。

前一项属于物理依赖，后两项会在世界几何没有变化时刷新。D107 已让 WorldModel 只在 knowledge 或方块几何真正变化时递增 semantic cell version。执行器再比较完整 `CellFact`，等于绕过这一规则，并让每次正常重看地形都作废证明。

真实变化仍必须拒绝。首版至少覆盖：

- `UNKNOWN -> AIR/BLOCK`，或已知格退回 `UNKNOWN`；
- `AIR <-> BLOCK`；
- 方块 material 改变；
- fluid 标志改变；
- collision kind 或 boxes 改变。

## 6. 可视空气证据的归属

`visual_air_evidence` 是动作开始前的取证条件，不是物理轨迹的碰撞依赖。

运动计算器需要的是该格当前为空、方块碰撞形状和流体等物理事实。观察距离、下部是否看见、证据年龄回答的是“机器人现在是否有资格开始高危下降”。这项资格由 `ActionSpec.precondition` 在真实动作边界检查。当前受控下降已经通过 `direct_drop_visual_evidence_sufficient()`、落点支撑新鲜度和 acquisition grant 单独处理。

因此：

- 只刷新、变旧或替换 `visual_air_evidence`，不应让一个几何上仍有效的 motion proof 变成 `DEPENDENCY_CHANGED`；
- 需要这类证据的受控下降，如果边界处证据缺失、过期或下部不可见，仍不得激活，即使物理 proof 的 semantic versions 没变；
- D116-1 不把可视空气证据加入 semantic geometry version，也不放宽受控下降前置条件。

## 7. 下一步边界

D116-1 只替换 prepared handoff 的依赖判断，不调整 Session、监督者和执行器顺序。实现后重跑同一条时间线：tick 20 应从 `DEPENDENCY_CHANGED` 变成 `READY_MATCH`，同时所有真实几何变化和取证负例仍被拒绝。

只有这项共享规则修好后，新的时间线仍显示边界晚一帧，才讨论同帧第二阶段。当前没有依据修改生命周期顺序。

D116-0 没有运行 Fabric，也没有交付连续两跳能力。D115 的原始停止文件保持原字节；其中的旧根因由本决定更正。D116-1 的具体任务、RED、变异检查和止损条件见[实施计划](../stages/d116-semantic-dependency-repair-plan.md)。

## 8. D116-1 实际结果

语义依赖修复按第 5、6 节实施。纯观察时间戳刷新现在得到 `READY_MATCH`；owner 缺失、历史不完整以及 knowledge、方块、流体和碰撞几何变化仍会拒绝。受控下降的视觉空气证据继续由动作前置条件检查。

同一条正式代表链取得了以下结果：

- tick 20：prepared entry 为 `READY_MATCH`，监督者批准；
- tick 20：Runtime 只选中一条移动输入；
- tick 21：第二跳首条 `forward + jump + sprint` 输入实际应用；
- 全程 owner 没有空档，伤害为 0。

这关闭了 D115-HANDOFF-01 中“纯 stamp 刷新导致误拒”的部分，但连续两跳没有完成。时间线暴露一个后续运动阻塞，并把一个此前已存在的异步不变量缺口纳入门槛：

1. prepared 激活把执行器从 action 1 推进到 action 2，但 `ActiveRouteTracker` 仍绑定 action 1。tick 21 路线复核返回 `STOP / route_identity_changed`，并以 `dependency_changed` 取消刚激活的控制器。
2. prepared 结果已经登记为 `APPLIED`，异步生命周期却没有对应的 `apply` 事件。监视器在第二跳交接前报告 I19：`result applied without current identity, facts or time`。证据卫生重建表明，I19 在旧门槛 RED 中也会出现，因此它不是 D116-1 引入的回归，但仍是正式门禁缺口。

这两项不属于语义依赖修复的授权范围。D116 按止损规则停在这里，不调整生产交接顺序，不扩大入口，也不运行 Fabric。下一步应分别修正“动作索引与路线追踪身份的原子交接”和“prepared admission 的生命周期事件”。

## 9. 证据卫生更正

D116-0 最初生成的 `timeline.json` 没有提交。D116-1 复跑时，同一路径被现行结果覆盖。它们不能继续共用“D116-0 时间线”这个名字，也不能把后来文件当成早期 RED。

现行证据明确分开：

- `d116-0-red-timeline.json`：诊断脚本在进程内显式恢复旧完整 `CellFact` 门槛，重建 `DEPENDENCY_CHANGED` 和第二跳未起步的 RED；
- `d116-1-semantic-result-timeline.json`：使用当前生产语义依赖判断，记录 tick 20 获批、tick 21 应用输入、后续路线身份阻塞和既有 I19；
- `index.json`：记录两份文件的角色、哈希和覆盖事实。

重建 RED 能验证旧门槛的机制，但不是最初未提交时间线的原字节。D116-0 的工作结论保留，这项证据缺口也如实保留。
