# F2-GP：共享地面跟踪策略实施方案

日期：2026-10-08。状态：批次 1—2 已保留；批次 3 按止损规则停止，阶段未通过。后续先执行独立的 [F2-PC](F2PC-ground-precision-control.md)。

**目标：** 让规划 rollout 和正式 `FixedRoute` 执行共享唯一的普通地面闭环策略，用真实身体状态安全完成终点末段，并把预计末段成本纳入一次 A*。

**依据：** [D080](../decisions/0080-share-ground-tracking-policy-for-terminal-approach.md)、[F2-GP 验收](../acceptance/F2GP-shared-ground-tracking-policy.md)、[D079](../decisions/0079-model-terminal-approach-as-a-real-search-edge.md)和 [F2-ST](F2ST-terminal-approach-proof.md)。

## 1. 当前基线

F2-ST 首轮已经保留：

- 剩余 9 项机器可读 RED；
- typed `GroundTraversalExitRequirement`；
- typed `GroundTerminalApproachResult`；
- `GroundTerminalApproach`、`SurfaceTerminalApproachEdge` 和 `terminal_approach_id` 的通用数据契约；
- 通用 A* 循环不含终点动作特判的门禁。

F2-ST 没有通过。近停的 `0..0.10` 格／秒入口已否决；约 `0.50..1.19` 格／秒只证明单个代表状态可行，不能作为整个窗口的证明。搜索、接纳、执行和复核的未提交改动已经撤销。

F2-GP 不继续扩大窗口证明。它把普通地面末段改为可恢复的逐 tick 闭环控制。不可逆动作继续走原严格证明链。

## 2. 实施范围

预计修改：

- 普通地面候选生成、rollout 与评分所在模块；
- `mc2p/motion_nav/fixed_route.py` 或拆出的纯策略模块；
- `mc2p/motion_nav/known_map_planner.py`；
- `mc2p/motion_nav/route_admission.py`；
- `mc2p/motion_nav/action_route.py`；
- `mc2p/motion_nav/action_route_executor.py`；
- `mc2p/motion_nav/route_validation.py`；
- 对应 `tests/motion_nav/`、`tests/sim/`、`scripts/` 和四类正式文档。

明确不改：

- 通用 `_plain_search()`、`_resource_aware_search()` 循环；
- Session、PlanningCoordinator、PlannerWorker 生命周期；
- Runtime、MotorGateway、ExecutionSupervisor 和输入账本；
- JumpGap、JumpUp、ControlledDrop 等严格动作的证明；
- 路线平滑、潜行生产者、动态避障和未知探索。

实施前用调用图确定实际文件。若需要新增 Session 状态、等待、重试，或修改通用 A*，立即停止并修订决定。

## 3. 实施规则

每个批次按 TDD 进行：

1. 先写正式失败检查；
2. 确认失败原因与本批目标一致；
3. 做最小实现；
4. 运行本批定向检查和错误副本；
5. 独立复核后提交；
6. 再进入下一批。

任何新失败先在最小场景查根因。不得为了让完整矩阵变绿而连续扩大当前批次。

## 4. 批次 0：冻结行为、共享边界和错误副本

先补测试，不改生产行为。

1. 固定剩余 9 项、原 51 项、已恢复 42 项和 F2S-C-04。
2. 固定直线、X→Z、Z→X、退化去重和成本平手的小图。
3. 固定普通地面可恢复与起跳／下降不可逆的分类边界。
4. 固定规划 rollout 和 FixedRoute 当前候选输入、预测状态、依赖和完成结果的对照样本。
5. 固定输入晚 1 tick、单帧丢输入、目标修订、相关世界变化、无关世界变化和外力偏离。
6. 固定无进展达到上限后有界重规划；预算耗尽必须到达有类型终态。

错误副本至少包括：

- 规划和执行使用不同候选排序；
- rollout 忽略停止尾迹；
- 执行跳过当前支撑或净空查询；
- 接纳器把真实速度改成代表速度；
- terminal edge 使用固定成本或直线距离；
- 只生成一种 L 形；
- 恢复 completion 面积阈值；
- 在 A* 外依次重试候选；
- 普通地面结果误用到起跳或下降；
- 停滞没有上限；
- 相关依赖变化后继续沿用旧末段；
- 入口已满足时仍发送方向输入。

**完成条件：** 每个 RED 和错误副本都由行为断言发现；严格动作的现有证据逐项不变。

## 5. 批次 1：抽出唯一纯 GroundTrackingPolicy

1. 从现有 FixedRoute 候选生成与评分中抽出一个纯策略入口。
2. 输入只含 `PhysicsState`、route、completion、profile、world query、输入资格和停止尾迹约束。
3. 返回类型化的下一步输入、预测状态、进度、安全依赖和状态结论。
4. 正常不可行返回 typed 结果，不抛 `ContractViolation`。
5. 原 `FixedRouteController` 改为调用该策略，不另存第二套候选和评分规则。
6. 使用旧样本做逐项行为等价比较；浮点比较采用现有冻结容差。

**完成条件：** 原普通 FixedRoute 样本的输入、进度、完成和失败分类逐项一致；删除任一关键安全查询的错误副本都会失败。

## 6. 批次 2：用同一策略做规划 rollout

1. 为直线、X→Z、Z→X 生成确定性路线，退化路线去重。
2. 用 `GroundTrackingPolicy` 从明确的代表起始状态 rollout，直到完成、typed 否定、停滞或预算结束。
3. rollout 使用与执行相同的 tick、停止尾迹、碰撞、支撑和净空规则。
4. 记录预计 tick 成本、依赖、策略版本和终态分类。
5. 按预计 tick、路线种类和固定坐标顺序稳定选择，不读取墙钟完成顺序。
6. rollout 结果只用于可跟踪性和成本估计，不宣称证明整个连续入口窗口。

**完成条件：** 独立逐 tick 驱动与规划 rollout 得到相同输入选择和预计成本；输入顺序扰动不改变结果。

## 7. 批次 3：把末段成本接入一次 A*

1. `SurfaceTerminalApproachEdge` 保存 route、completion、policy version、预计 cost 和 dependencies。
2. 需要移动的边使用正 tick 成本；已经满足 completion 时使用零输入、零 tick。
3. `PlannerStateKey.terminal_approach_id` 继续区分普通图状态与完成状态。
4. `goal_test` 只接受已经选择 terminal edge 的状态。
5. A* 的 `g` 值包含末段预计成本。
6. 不使用面积阈值、虚拟汇点或候选外层重试。
7. 保持通用 A* 循环源码门禁不变。

**完成条件：** 正式 A* 与独立参考搜索选择相同终点、路线和总成本；没有零代价循环，也没有按候选面多次启动 A*。

## 8. 批次 4：真实状态接纳、重锚和逐 tick 安全执行

1. RouteAdmitter 用最新正式 `PhysicsState` 复核模式、姿态、支撑、净空、completion、规则版本和依赖。
2. 将真实位置和速度映射到已选 route 的确定进度；不能改成 rollout 的代表状态。
3. ActionRoute 使用搜索选中的同一条 route、completion 和 policy version。
4. 执行器每 tick 调用 `GroundTrackingPolicy`，并用最新世界事实复核本 tick 输入和停止尾迹。
5. 输入必须受现有租约约束。晚到、丢输入或偏离时先保持安全，再从当前观察重锚或请求重规划。
6. 相关依赖变化使旧末段失效；无关变化不触发重新选 route。
7. 连续无进展达到冻结上限后有界重规划；重复失败消耗现有预算并到达类型化终态。

**完成条件：** 搜索、witness、接纳、ActionRoute、执行和复核持有相同 route、completion、policy version 和依赖；实际每 tick 输入来自当前状态而非回放计划。

## 9. 批次 5：Windows 分层回归与性能

严格按以下顺序运行，任一步失败即停止：

1. GroundTrackingPolicy 组件、行为等价和错误副本；
2. 剩余 9 项、原 51 项、F2S-C-04；
3. F2-S v9 新增 216、v8 固定 104、v8 杂乱 1800；
4. v7 2000、F2 528、F2-R 1904；
5. 五组行为、严格动作和协调集合；
6. 完整 motion_nav 正序和逆序；
7. D061；
8. 1、4、16、32 个终点的规划性能矩阵。

门槛：

- 历史成功退步 `0`；
- 新安全事件、伤害额度超出、来源泄漏和终态潜行均为 `0`；
- 单次规划默认期限 `0.5 s`；
- 控制生产 P95 `≤8 ms`、P99 `≤15 ms`、最大值 `<30 ms`；
- 完整夹具最大值 `<50 ms`；
- deadline miss 为 `0`；
- 单列 rollout 数量、每条路线 tick、A* 展开数、总规划耗时和缓存命中。

## 10. 批次 6：Fabric 正式链

最低覆盖：

- 直线、X→Z、Z→X；
- 起点即满足 completion 和需要先走普通 Walk；
- 四方向 normal／首条晚1；
- 合法入口速度的低、中、高代表值，但不把三点冒充连续窗口证明；
- 单帧丢输入、目标修订和外力偏离；
- 相关世界变化和无关世界变化；
- 停滞后的有界重规划；
- 至少一个严格动作对照，证明其仍走原证明链。

必须通过 profile 4、Runtime、NavigationSession、PlannerWorker、RouteAdmitter、ActionRouteExecutor 和真实输入账本。记录策略版本、预计成本、真实用时、实际应用 tick、重锚、重规划、依赖变化和来源注销。

## 11. 止损条件

出现以下任一情况立即停止：

- 需要修改通用 A* 循环；
- 需要新增 Session 状态、等待、重试或候选队列；
- 规划和执行无法共用同一纯 GroundTrackingPolicy；
- 需要把真实入口改成代表速度或近停；
- 需要恢复面积阈值、候选顺序或场景 ID 特判；
- 普通地面修正改变了严格动作证明；
- 无法在现有预算内有界处理停滞和重规划；
- 两轮局部修正后仍有历史成功退步；
- 1／4／16／32 终点矩阵超出既有规划期限；
- Windows 或 Fabric 安全门槛失败。

最多允许两轮局部修正。超过两轮仍不通过时，保留已通过的纯策略和测试，阶段记为未通过，另写决定再改变方向。

## 12. 关闭条件

1. 剩余 9 项完成，原成功退步为 `0`；
2. 规划 rollout 与 FixedRoute 执行共享唯一 GroundTrackingPolicy；
3. 末段预计成本进入一次 A*；
4. 正式执行从真实状态接纳并逐 tick 安全闭环；
5. 停滞、偏离和输入问题有界重锚或重规划；
6. 不可逆动作仍使用原严格证明；
7. Windows、D061、规划性能和 Fabric 全部通过。

F2-GP 通过后只解除 F2-SG 的当前阻塞。F2-SG 仍需继续完成自己的停止点，不得直接继承 F2-GP 的结论。

## 13. 实施结果与停止点

提交 `61fefa78` 保留了批次 1—2 的可独立验证部分：

- 新增唯一纯 `GroundTrackingPolicy`，普通 `FixedRoute` 已调用它生成和排列候选；
- 保留原 `FixedRoute` 的候选输入、进度、阻塞和不支持分类；
- 同一策略可以对直线、X→Z 和 Z→X 路线做有界 rollout，并返回预计 tick、依赖和类型化结果；
- 策略不能生成跳跃、潜行或疾跑许可，也不修改严格动作证明。

Windows 定向检查共 `51/51` 通过。命令与结果见 `evidence/motion_navigation/F2GP-ground-tracking-policy-v1/stop-report.json`。

批次 3 没有通过。把 rollout 接到 terminal edge 后，F2S-C-04 的窄完成区域仍返回 `planning_no_known_route`。首轮当时把它概括成“两 tick 输入租约和松键尾迹冲突”。后续核对发现，正式普通 Walk 已按玩家 tick提交；不一致发生在 `GroundTrackingPolicy` 固定使用两 tick模拟评分。它会拒绝一 tick控制加完整停止尾迹仍然安全的候选。

项目做了两轮局部修正：

1. 按第一个真实 tick 的完成进度和制动效果排序，不再只看两 tick 末态；
2. 中性输入胜出时展开九种地面控制，优先选择留在完成区域内且下一 tick 速度更低的输入。

两轮都没有关闭 F2S-C-04，因此已按第 11 节撤销未提交实验。`known_map_planner`、`route_admission`、`safe_ground_control` 和 terminal edge 接线均回到 `61fefa78` 的已提交状态。正式 A* 尚未使用预计 tick 成本，RouteAdmitter 也尚未按该策略从真实状态重锚。

本轮没有运行 F2-S 大集合、五组行为、完整正逆序、D061、1／4／16／32 终点性能和 Fabric。原结果与失败均不改写。

后续按 [D081](../decisions/0081-select-ground-control-duration-from-stopping-margin.md) 和 [F2-PC](F2PC-ground-precision-control.md) 实施。策略依据停止距离和完成余量选择一或两 tick；一 tick仍验证完整停止尾迹；policy、decision 和意图租约保持同一个 `control_ticks`。F2-PC 通过后才恢复本阶段批次 3。不能继续修改评分、加入面积阈值、压低入口速度或用场景特判绕过该问题。
