# D116-1 prepared proof 语义依赖修复计划

日期：2026-10-17。决定见 [D116](../decisions/0116-attribute-prepared-handoff-before-changing-order.md)。

当前状态：**D116-1 已实施并止损。语义依赖修复通过，连续两跳仍被两项后续生命周期问题阻塞。**

## 1. 要解决什么问题

`ActionRouteExecutor.stage_prepared_action()` 现在先比较完整 `CellFact`，再比较 semantic dependency version。正常观察会更新 `CellFact.stamp`，所以第一步已经拒绝，后面的语义版本没有机会生效。

D116-1 删除这条重复且冲突的判断。在线交接只信任当前 WorldModel owner 对 `dependency_changes(saved_versions)` 的类型化结论。

## 2. 修改范围

预计修改：

- `mc2p/motion_nav/action_route_executor.py`
- 必要时只增加一个靠近 WorldModel 的公开语义检查辅助函数；不得另建依赖账本
- 新建 `tests/motion_nav/test_d116_semantic_dependency_handoff.py`
- 更新 `tests/motion_nav/test_d116_handoff_timeline.py`
- 更新 D116 验收、缺陷台账和紧凑证据

首轮不得修改：

- `NavigationSession` 的状态、转移或 propose 顺序；
- `ExecutionSupervisor` 的批准协议；
- prepared proof 的 execution window、entry tolerance、generation 或账本规则；
- WorldModel semantic version 的定义；
- 受控下降的视觉取证门槛；
- worker、receiver、registry 或 Runtime 生命周期。

## 3. RED

先写以下失败检查，确认当前代码确实不满足新规则：

### R1：纯 stamp 刷新

同一 world session、同一 knowledge、block geometry、fluid 和 collision，使用更新的观察 stamp 重看依赖格。`dependency_changes()` 必须返回 `UNCHANGED`，prepared query 应为 `READY_MATCH`。当前实现会返回 `DEPENDENCY_CHANGED`，形成 RED。

### R2：source owner 缺失

让相同 `PhysicsWorldView` 不带 owner。即使其静态 facts 与 proof 一样，也必须拒绝，不能退回完整事实等值比较。

### R3：历史不完整

使用真实 WorldModel 让依赖版本早于 `_changes_complete_after`，并且没有足够历史证明该格未变。`dependency_changes()` 返回 `HISTORY_INCOMPLETE`，prepared query 必须拒绝。

### R4：真实语义变化

分别构造并拒绝：

1. 已知格变 `UNKNOWN`；
2. `AIR -> BLOCK`；
3. `BLOCK -> AIR`；
4. material 改变；
5. fluid 标志改变；
6. collision kind 改变；
7. boxes 改变。

每项都要先断言 WorldModel 的 semantic version 已变化，再断言 prepared query 拒绝。这样能区分世界模型漏记和执行器漏查。

### R5：可视空气归属

分成两条检查：

- 空气几何不变，只更新 `visual_air_evidence` 的 stamp、距离或 `lower_region_visible`，physical dependency check 仍为 `UNCHANGED`；
- 对需要视觉取证的 `ControlledDropSegment`，在真实动作边界把证据改成缺失、过期、距离过远或下部不可见，`ActionSpec.precondition` 必须返回 `NEEDS_ACQUISITION` 或 `NEEDS_INFORMATION`，不能激活。

第一条证明证据元数据不属于物理依赖。第二条证明没有因此放宽高危动作门槛。

## 4. 最小实现

在完成入口、窗口和真实身体检查后：

1. 读取 `frame.world._owner`；
2. owner 缺失时返回 `DEPENDENCY_CHANGED`；
3. 调用 `owner.dependency_changes(hot.dependency_versions)`；
4. 只有 `DependencyStatus.UNCHANGED` 可以返回 `READY_MATCH`；
5. `CHANGED` 和 `HISTORY_INCOMPLETE` 都返回 `DEPENDENCY_CHANGED`；
6. 删除 `frame.world.cell(position) != fact` 的完整 `CellFact` 比较。

不使用当前版本元组的直接等值比较。`dependency_changes()` 还会检查 snapshot epoch、历史截断和真实变更，这是直接比较看不到的。

## 5. 变异检查

至少保留五类变异：

1. **重新加入完整 `CellFact` 比较**：R1 必须失败；
2. **owner 缺失时放行**：R2 必须失败；
3. **把 `HISTORY_INCOMPLETE` 当成未变化**：R3 必须失败；
4. **跳过 `dependency_changes()`**：R4 至少四类真实变化必须失败；
5. **跳过受控下降的 action precondition**：R5 的视觉证据负例必须失败。

变异可以用进程内 patch 完成，不把错误版本提交到生产树。

## 6. 代表正式链

复跑 D116-0 的正式链：

`PlayerRuntimeV1 -> RuntimeNavigationDriver -> NavigationSession -> ExecutionSupervisor -> ActionRouteExecutor -> MotionRouteCoordinator -> CalculatorBackend`

要求：

- 路线仍由正式规划器生成 `Walk -> JumpGap -> JumpGap`；
- 第二跳证明仍经过正式 worker、receiver 和 registry；
- tick 20 的 query 为 `READY_MATCH`；
- 监督者在 tick 20 批准；
- Runtime 选中的第二跳首条输入在 tick 21 实际生效；
- 同一帧只有一条最终移动输入，身体 owner 没有空档；
- 零伤害、零 UNKNOWN 放行、零旧 generation 应用。

这一步如果在依赖检查通过后暴露新的阻塞，先写新时间线归因。不得直接扩大入口或改生命周期顺序。

## 7. 相邻回归

按顺序运行：

1. WorldModel semantic version、snapshot 和 materialization 检查；
2. D102 prepared proof／handoff／receiver；
3. D113 交付抖动；
4. D114 正式驱动夹具；
5. `test_action_continuity_formal`；
6. controlled-drop precondition、landing evidence 和 edge probe；
7. D116 正式链；
8. Windows 完整正序、逆序与公开快照。

组件全绿前不运行 Fabric。代表链和延迟矩阵完成后，才决定是否使用一次 Fabric 验证连续两跳。

## 8. 性能和工作量

在线只调用一次有界的 `dependency_changes()`，输入是 proof 已冻结的依赖位置。不得重新物化 snapshot、扫描完整世界、规范化 JSON、计算摘要或重放轨迹。

记录依赖格数量和这次检查耗时。它与交接 tick 一起继续受 8／15／30 ms 控制门槛约束。

## 9. 停止条件

出现任一情况立即停止并报告：

- 需要新增 Session 状态、第二套依赖账本或新的 worker；
- 必须修改 WorldModel semantic version 定义才能让 R1 通过；
- 任一真实 knowledge、block、fluid 或 collision 变化没有递增 semantic version；
- owner 缺失或 `HISTORY_INCOMPLETE` 只能靠猜测放行；
- 必须放宽 ControlledDrop 的视觉取证门槛；
- 需要在控制线程重建 snapshot、哈希或重放 proof；
- 两轮限定修正后，代表链仍不能在 tick 20 得到可信批准；
- 出现清单外旧成功退步、安全违规或无界等待。

D116-1 达到上述门槛后，才能恢复连续两跳矩阵。不得把“纯 stamp 刷新可通过”扩大解释为“观察证据变化都可以忽略”。

## 10. 实际结果

实施严格停留在本计划的修改范围内。`stage_prepared_action()` 不再比较完整 `CellFact`，而是只接受当前 WorldModel owner 返回的 `UNCHANGED`。owner 缺失、`HISTORY_INCOMPLETE` 和真实语义变化均保守拒绝。

RED 与边界检查通过：

- 纯 stamp 刷新通过；
- owner 缺失和历史不完整拒绝；
- 七类物理语义变化拒绝；
- visual-air 元数据刷新不改变物理依赖结论；
- ControlledDrop 的缺失、过期、过远和下部不可见证据仍被前置条件拒绝。

第 5 节列出的独立 mutation 批次没有运行。现有测试直接覆盖了对应错误结果，但在代表链出现新的清单外阻塞后，本阶段按止损规则结束，不能把直接覆盖写成 mutation 门槛已签署。

代表正式链也达到本计划对修复本身的三个关键要求：tick 20 `READY_MATCH`、监督者批准，tick 21 第二跳首条输入实际应用。Runtime 在交接帧只有一个移动输入，owner 无空档，伤害为 0。

随后出现一个清单外运动阻塞，并确认一个此前已存在的异步门禁缺口，因此按第 9 节停止：

- 路线追踪身份仍停在上一动作，复核以 `route_identity_changed` 取消新控制器；
- prepared admission 缺少异步生命周期 `apply` 事件，监视器报告 I19。重建 RED 中也有同一事件，所以它不是 D116-1 回归。

没有继续运行扩大矩阵、完整 Windows、公开快照或 Fabric。性能也没有重新签署；只读诊断包含大量插桩，不能用它判断 8／15／30 ms 门槛。
