# F2-PC：普通地面精细控制实施方案

日期：2026-10-08。状态：已实施一次并按 fail-fast 停止；阶段未通过。生产实现已撤销，只保留 RED 和历史失败证据；后续由 [F2-SC](F2SC-safe-tail-and-completion.md) 重新实施。

**目标：** 让普通 `STANDING + WALK` 在远处保持两 tick 历史包络，在完成余量不足时改用经过完整停止尾迹验证的一 tick 控制。规划 rollout、正式决策和 Runtime 意图必须使用同一个 `control_ticks`。

**依据：** [D081](../decisions/0081-select-ground-control-duration-from-stopping-margin.md)、[D080](../decisions/0080-share-ground-tracking-policy-for-terminal-approach.md)、[F2-GP](F2GP-shared-ground-tracking-policy.md)及其[验收](../acceptance/F2GP-shared-ground-tracking-policy.md)。

## 1. 当前事实

提交 `61fefa78` 已保留唯一纯 `GroundTrackingPolicy`。普通 `FixedRoute` 和规划 rollout 能调用它，直线与两种 L 形路线的定向检查为 `51/51`。

F2-GP 批次 3 没有通过。正式普通 Walk 已按玩家 tick提交，策略评分却固定模拟两 tick 同向输入。窄完成区域前，这个模型会拒绝一 tick 可安全进入、随后可以制动的候选。

未提交的 planner、A*、接纳和执行接线已经撤销。F2-PC 先修预测与许可的一致性。它不会直接关闭 F2-GP。

## 2. 修改范围

预计修改：

- `mc2p/motion_nav/ground_tracking_policy.py`；
- `mc2p/motion_nav/fixed_route.py`；
- 普通 Walk 的意图组装位置；
- 对应 `tests/motion_nav/`、`tests/sim/`、性能脚本和四类正式文档。

只有确有调用关系时才修改其他模块。

明确不改：

- 通用 A* 搜索循环；
- Session、PlanningCoordinator 和 PlannerWorker 生命周期；
- completion region、目标面选择和路线候选集合；
- JumpGap、JumpUp、ControlledDrop 等严格动作；
- D068 的既有白名单；
- 新的评分权重、入口速度区间或完成阈值。

## 3. 实施顺序

每批先写失败检查，再改生产代码。Windows 是正式平台。任何一步失败都先在最小场景查明原因，不直接运行更大的集合。

### 批次 0：冻结 RED 和错误副本

先记录当前行为，不改生产代码：

1. F2S-C-04 继续稳定失败，且失败发生在 terminal rollout；
2. 一个远离 completion 的普通 Walk 保持两 tick 历史选择；
3. 一个两 tick 会越界、一 tick 完整尾迹安全的窄区域场景，应选择一 tick；
4. 一个连一 tick 完整尾迹也不安全的场景，应返回 typed 不可用；
5. 起跳、下降、潜行、疾跑和空中动作不得进入新规则；
6. 一 tick 输入晚到时必须被压下，并从下一观察重新锚定。

错误副本至少包括：

- 始终固定两 tick；
- 所有普通 Walk 都强制一 tick；
- 一 tick 只检查控制帧，不检查完整停止尾迹；
- policy 选择一 tick，`FixedRouteDecision` 或意图仍写两 tick；
- 精细输入取得 D068 的晚启动余量；
- 晚到精细输入仍被应用；
- 用场景 ID、动作名或固定“距终点多少格”选择时长；
- 收到新观察后仍回放旧多帧决定；
- 新规则误用于严格动作。

**完成条件：** 每个错误副本都能被行为断言发现；原 F2-GP `51/51` 不改写。

### 批次 1：实现纯时长选择

1. 为候选分别计算一 tick和两 tick的控制段；
2. 两种候选都继续模拟完整松键尾迹；
3. 从扫掠、停止距离、completion 余量和路线走廊得到确定性选择；
4. 远处优先保留两 tick；两 tick消耗过多余量而一 tick安全时选择一 tick；
5. 两种都不安全时返回 typed 结果；
6. 选择结果显式携带 `control_ticks`，不靠 reason 字符串推断。

同一输入重复运行必须得到相同结果。候选枚举顺序扰动不能改变结果。

**完成条件：** 组件 RED 转绿；错误副本全部被发现；没有场景或动作名分支。

### 批次 2：贯通同一个 control_ticks

将选中的 `control_ticks` 逐层传入：

1. `GroundTrackingPolicy` 结果；
2. `FixedRouteDecision`；
3. 普通 Walk 移动意图；
4. Runtime 输入租约和输入账本；
5. 规划 rollout 和诊断记录。

每收到一个新的玩家 tick观察，都重新运行策略。不能缓存并回放上一帧的方向决定。

精细一 tick意图的最早和最晚生效 tick 必须相同。过期后 Runtime 只压下该帧输入，控制器从下一正式观察重新锚定。远处两 tick意图继续使用已有包络，不能把精细规则扩散到所有 Walk。

**完成条件：** 五处记录的 `control_ticks` 一致；一 tick晚到反例不会应用输入；两 tick历史对照保持。

### 批次 3：只检查 F2S-C-04

恢复 terminal edge 所需的最小正式接线，只运行：

- GroundTrackingPolicy 定向检查；
- 普通 FixedRoute 定向检查；
- F2S-C-04 正常与必要负对照。

若实现契约正确，而 F2S-C-04 仍未完成，立即停止。不得调评分、增加候选、放宽 completion 或追加第二轮策略。

若 F2S-C-04 完成，再进入后续回归。

### 批次 4：Windows 分层回归和性能

严格按顺序执行：

1. F2-PC 组件、错误副本和 F2-GP 原 `51/51`；
2. F2S-C-04、剩余 9 项和已恢复 42 项；
3. F2-S v9 216、v8 固定 104、v8 杂乱 1800；
4. v7 2000、F2 528、F2-R 1904；
5. 五组行为、严格动作和协调集合；
6. 完整 motion_nav 正序和逆序；
7. D061；
8. 1、4、16、32 个终点的规划性能矩阵。

任何一层失败即停止，不继续跑后续大集合。

性能门槛：

- 控制生产 P95 `≤8 ms`、P99 `≤15 ms`、最大值 `<30 ms`；
- 完整夹具最大值 `<50 ms`；
- 默认规划期限 `0.5 s`；
- deadline miss 为 `0`。

另报告每帧候选数量、一／两 tick选择比例、物理 rollout 次数和策略耗时。不得为了性能跳过完整停止尾迹。

### 批次 5：Fabric 正式链

最低覆盖：

- 四方向窄 completion，normal 与精细输入晚一 tick；
- 远处普通 Walk 的两 tick历史对照；
- 一 tick接近后下一玩家 tick重新计算；
- 一 tick晚到被压下，下一观察重新锚定；
- 单帧丢输入、目标修订和无关世界变化；
- 一个相关世界变化反例；
- JumpGap 或 ControlledDrop 严格动作对照。

必须走 profile 4、Runtime、NavigationSession、PlannerWorker、RouteAdmitter、ActionRouteExecutor 和真实输入账本。保存 policy、decision、intent、requested／latest／actual tick、停止尾迹、重锚和来源注销。

## 4. 安全和性能边界

- 一 tick仍验证完整停止尾迹；
- UNKNOWN 继续按未知处理；
- 不降低支撑、净空、碰撞、危险接触或伤害门槛；
- 不增加 Session 状态、等待、重试和候选队列；
- 不用墙钟决定行为；
- 不对所有 Walk 强制两次物理计算。只有两 tick可能消耗 completion 或安全余量时才需要比较一 tick，具体短路条件也必须由物理余量决定；
- 正式控制仍每玩家 tick重算，不能因为获得两 tick许可而跳过新观察。

## 5. 止损条件

出现任一情况立即停止：

- 正确实现本方案后 F2S-C-04 仍未关闭；
- 需要继续调评分才能让主例完成；
- 需要场景、动作名、目标尺寸或固定距离特判；
- 需要放宽 completion、入口速度、停止尾迹或 UNKNOWN；
- 需要修改通用 A* 或新增 Session 生命周期；
- 严格动作证据改变；
- 历史成功出现退步；
- Windows、D061 或 Fabric 安全门槛失败。

止损后保留已独立通过的纯策略和测试，F2-PC 记为未通过。新的根因必须另写决定，不能在本阶段继续打补丁。

## 6. 关闭条件

F2-PC 只有同时满足以下条件才通过：

1. F2S-C-04 完成，原成功退步 `0`；
2. control duration 只由物理余量决定；
3. 一 tick和两 tick都验证完整停止尾迹；
4. policy、decision、intent、账本和 rollout 的 `control_ticks` 一致；
5. 精细输入晚到会被压下并从新观察重锚；
6. 远处两 tick历史行为保持；
7. 严格动作仍走原证明链；
8. Windows、D061、性能和 Fabric 全部通过。

F2-PC 通过后，回到 F2-GP 批次 3。F2-GP 仍需完成 A* 成本、真实状态接纳及其后续验收，不能直接继承 F2-PC 的通过结论。


## 7. 实际实施结果

### 7.1 已验证的局部事实

最小 RED 先于生产修改提交。一次实现能够在组件层区分：

- 远处普通 Walk：继续选择两 tick；
- 两 tick 停止尾迹越过窄区域、一 tick 尾迹仍在区域内：选择一 tick；
- 连一 tick 尾迹也越界：返回 typed 阻塞；
- 一 tick 仍包含完整中性停止尾迹；
- `GroundTrackingPolicy`、`FixedRouteDecision` 和 ActionRoute 的精确生效窗口使用同一个时长；
- 一 tick 精细输入没有取得 D068 的晚启动余量。

定向组件、原 F2-GP、FixedRoute 和首动窗口组合检查为 `46/46`。

### 7.2 止损触发

随后只运行 F2S-C-04。第一次结果是 `no_safe_ground_candidate`，最终位置为 `(1.15004, 64, -2.7)`。完成区域的 x 范围是 `[1.0, 1.09115]`。

检查发现，一 tick 候选被简化地面模型判为越界后，还需要走现有正式 1.21 计算器复核。补齐这项接线后，结果变成 `fixed_route_stalled`，最终位置为 `(0.99, 64, -2.69)`。安全违规和伤害仍为零，但任务没有完成。

第二次改动只修正证明链：正式复核也使用候选声明的一 tick，并检查完整中性尾迹。它没有改评分、候选集合、completion、速度门槛或 A*。因此这已经满足“契约与接线正确，但 F2S-C-04 仍不完成”的止损条件。

### 7.3 保留与撤销

保留：

- 提交 `81522012` 的最小 RED；
- 一 tick／两 tick 的组件输入和两次 F2S-C-04 失败结果；
- 新根因：安全停止尾迹与任务 completion 不能使用同一个逐帧门槛。

撤销：

- `GroundTrackingPolicy` 的生产时长选择；
- `FixedRouteDecision` 和 ActionRoute 的一 tick 接线；
- 正式计算器复核一 tick 候选的未提交改动。

没有运行剩余 9 项、大集合、D061、性能矩阵或 Fabric。F2-PC、F2-GP、F2-SG 和 F2-S 都保持未通过。
