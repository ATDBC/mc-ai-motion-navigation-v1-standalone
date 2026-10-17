# D116 准备动作交接归因计划

日期：2026-10-17。对应决定见 [D116](../decisions/0116-attribute-prepared-handoff-before-changing-order.md)。

当前状态：**D116-0 已完成；D116-1 已实施并因新的独立生命周期阻塞而停止。**

## 1. 范围

本阶段先回答一个问题：第二跳在 tick 20 为什么没有被批准？

D116-0 只能增加测试、诊断脚本和证据。不得修改生产交接顺序，不得扩大入口、执行窗口、UNKNOWN 或伤害规则，也不得运行 Fabric。

## 2. D116-0 实施顺序

### A. 修好测试入口

由独立切片处理 `InlineMotionWorker` 的 prepared job 身份，以及冻结值校验器的 `frozenset[str]`。D116-0 不修改这两处，只使用修好后的正式测试入口。

### B. 建立正式链 RED

通过 `tests.sim.runner` 建立：

`PlayerRuntimeV1 -> RuntimeNavigationDriver -> NavigationSession -> ExecutionSupervisor -> ActionRouteExecutor -> MotionRouteCoordinator -> CalculatorBackend`

正式规划器必须返回 `WalkSegment -> JumpGapSegment -> JumpGapSegment`。诊断补丁只恢复已撤回的后继准备调用，其他步骤继续走正式路径。

### C. 记录逐 tick 时间线

记录决定文档第 2 节列出的事实。tick 内事件必须保留发生顺序。Runtime 的最终输入要同时记录决定 tick 和实际生效 tick。

### D. 归因并止损

按三条归因分支分类。结果能够唯一归因后立即停止，不在同一切片修生产行为。

## 3. D116-0 门槛

- 代表路线必须来自正式规划器；
- 运行必须经过正式 Runtime 和导航驱动；
- 第一跳真实执行，第二跳证明进入正式 receiver 和 registry；
- 时间线必须同时包含控制器、边界、prepared query、Session、监督者、账本、着地和 Runtime 输入；
- 归因必须由同一 tick 的事实支持，不能根据相邻 tick 猜测；
- 生产行为修改为 0，Fabric 运行次数为 0；
- 伤害和不变量违规为 0。

## 4. 实际结果

D116-0 已通过。四项正式检查覆盖正式链、tick 内顺序、依赖拒绝和后续结果。

归因结果为：tick 20 先发布边界，再进入 Session 的 prepared approval；控制器、账本、落地、身体入口和执行窗口均已就绪。完整 `CellFact` 因观察时间戳刷新而不同，但其方块／空气内容与正式依赖版本未改变。第一次拒绝是 `DEPENDENCY_CHANGED`。

证据卫生修正后分成两份：

- `evidence/motion_navigation/d116-handoff-timeline-v1/d116-0-red-timeline.json`：在现行代码上显式重放旧完整 `CellFact` 门槛得到的 RED；
- `evidence/motion_navigation/d116-handoff-timeline-v1/d116-1-semantic-result-timeline.json`：现行 D116-1 语义依赖结果。

D116-0 最初的未提交 `timeline.json` 曾在 D116-1 复跑时被覆盖，因此无法声称 RED 文件保留了原始字节。新的 RED 是带明确 provenance 的旧门槛重建，只用于复核机制和前后差异。

## 5. D116-1 预定顺序

1. 先写“时间戳刷新允许通过、真实语义变化必须拒绝”的 RED 和变异检查；
2. 把 prepared proof 依赖检查统一到世界模型公开的语义身份；
3. 重跑 D116-0 时间线，要求 tick 20 取得批准；
4. 再检查第二跳是否在 tick 21 实际生效；
5. 代表场景通过后，才进入四方向、平台长度、交付延迟和取消矩阵；
6. 组件矩阵通过后，才允许一次 Fabric。

如果修复依赖身份后出现新的独立阻塞，必须重新归因。不得沿用 D115 已被推翻的顺序结论。

## 6. D116-1 收尾

D116-1 已证明语义依赖误拒可以在原层修正。代表链在 tick 20 获批，tick 21 实际应用第二跳输入，且只有一个移动 winner、owner 无空档、伤害为 0。

链路随后被 `route_identity_changed` 阻塞；同一时间线还记录了异步生命周期 I19。重建 RED 证明 I19 在 D116-1 前已经存在，不是语义修复引入，但它仍会阻止正式门禁关闭。按本计划的止损要求，本轮没有顺手修改路线追踪或异步生命周期，也没有进入扩大矩阵和 Fabric。
