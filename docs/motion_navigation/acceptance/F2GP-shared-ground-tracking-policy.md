# F2-GP 共享地面跟踪策略验收

日期：2026-10-08。状态：批次 1—2 定向检查通过；批次 3 失败，F2-GP 未通过。后续控制时长由独立 [F2-PC 验收](F2PC-ground-precision-control.md) 管理。

本验收判断三件事：规划与执行是否使用同一份普通地面策略；末段成本是否进入一次 A*；正式执行是否从真实状态逐 tick 保持安全。

## 1. 历史证据边界

| 集合 | 当前结果 | F2-GP 处理方式 |
|---|---:|---|
| F2-SG 原 v8 杂乱层 | `1696/1800` | 保留首次完整结果 |
| 旧成功退步 | `51` | 不改写 |
| 零边末段修复恢复 | `42` | 防止再次退步 |
| 仍未关闭 | `9` | 主 RED |
| F2S-C-04 | 主例完成 | 保持区域目标能力 |
| F2-ST typed 定向检查 | `11/11` | 保留类型和单状态检查，不冒充窗口证明 |
| F2-ST 非零入口诊断 | 未提交 `9/9` | 只说明根因方向，不计为通过 |

F2-ST 继续记为未通过。其 RED、typed 结果和通用 terminal edge 契约可以复用；近停入口和“一个代表状态证明整个窗口”都不得恢复。

## 2. 共享策略验收

同一个 GroundTrackingPolicy 必须同时被以下路径调用：

- 后台 terminal route rollout；
- 正式 FixedRoute 每 tick 决策。

对同一 `PhysicsState`、route、completion、world 和 profile，两条路径必须得到相同的：

- 候选输入与稳定排序；
- 下一状态预测；
- 路线进度；
- 支撑、净空、碰撞和停止尾迹依赖；
- 完成、继续、重锚、重规划或不支持分类。

策略不得读取 Session、Runtime、worker、墙钟或全局可变状态。正常不可行使用 typed 结果，数据损坏和坏调用才允许抛契约异常。

## 3. 风险分级验收

普通 `STANDING + WALK` 末段按可恢复地面移动验收：

- 不要求一份输入脚本覆盖整个连续入口窗口；
- 接纳时读取真实位置、速度和支撑；
- 每 tick 重新判断当前输入与停止尾迹是否安全；
- 偏离时可以重锚或有界重规划。

严格动作对照必须证明：

- 起跳、跨隙、受控下降和空中落地没有改走 GroundTrackingPolicy 的宽松入口；
- 原入口、轨迹、落地和伤害证明仍生效；
- 普通地面策略不能为严格动作生成执行许可。

## 4. 路线与成本验收

至少覆盖：

- 直线唯一可行；
- X→Z 唯一可行；
- Z→X 唯一可行；
- 三条都可行时选择预计 tick 最低者；
- 成本相同按冻结顺序选择；
- 退化路线去重；
- 已满足 completion 时零输入、零 tick；
- UNKNOWN、碰撞、支撑不足、材质不支持、停滞和预算耗尽。

`SurfaceTerminalApproachEdge` 必须保存同一条 route、completion、policy version、预计 cost 和 dependencies。A* 的总成本包含末段预计 tick。不得使用面积阈值、直线距离常数、虚拟汇点或候选外层重试。

正式 A* 与独立参考搜索必须选择相同的 terminal surface、末段路线和总成本。通用搜索循环源码门禁保持不变。

## 5. 接纳与逐 tick 执行验收

RouteAdmitter 必须使用最新正式观察：

- 接受可映射到已选路线走廊的真实状态；
- 保留真实速度，不替换成 rollout 代表速度；
- 拒绝错误姿态、模式、失效支撑、净空不足和规则版本不符；
- 相关 UNKNOWN 继续阻塞；
- 不重新选择另一条直线或 L 形。

执行阶段每 tick 必须证明：

- 当前输入租约有效；
- 当前身体仍可安全执行候选；
- 输入有效期和松键停止尾迹内没有越过支撑、碰撞或危险边界；
- 进度没有越过 completion 或路线走廊；
- 连续无进展有冻结上限。

晚到、丢输入、外力偏离或世界变化时，先停止扩大偏差，再从当前观察重锚或请求重规划。达到既有预算上限后进入 typed 终态，不能永久等待。

## 6. 身份和复核验收

搜索结果、witness、RouteAdmitter、ActionRoute、ActionRouteExecutor、RouteValidator 和最终报告都记录：

- terminal approach ID；
- route 点列；
- completion region；
- GroundTrackingPolicy version；
- estimated cost ticks；
- dependencies。

相关依赖变化使旧末段失效。无关事实变化不应改变已经绑定的路线。复核只检查绑定路线是否仍安全，不重新运行路线选择。

## 7. 错误副本

至少证明下列错误都会被发现：

1. 规划和执行使用不同候选排序；
2. rollout 不检查停止尾迹；
3. 执行不检查当前支撑或净空；
4. 接纳器把真实速度改成代表速度；
5. terminal edge 使用固定或直线距离成本；
6. 只生成一种 L 形；
7. completion 面积阈值重新参与可执行性；
8. A* 外按候选重试；
9. 普通策略授予严格动作许可；
10. 停滞计数没有上限；
11. 相关依赖变化后继续执行旧路线；
12. 已满足 completion 时仍发送方向输入；
13. 输入租约过期后仍发送旧提案；
14. 复核悄悄选择另一条路线。

## 8. Windows 回归

按以下顺序运行：

1. 共享策略组件、行为等价和错误副本；
2. 主 9 项、原 51 项、F2S-C-04；
3. v9 216、v8 固定 104、v8 杂乱 1800；
4. v7 2000、F2 528、F2-R 1904；
5. 五组行为、严格动作和协调集合；
6. 完整 motion_nav 正序和逆序；
7. D061。

每一步都保存命令、提交、源码指纹、结果索引和失败详情。任一步失败立即停止。历史成功退步必须为 `0`。安全事件、伤害额度超出、来源泄漏和终态潜行必须为 `0`。

## 9. 性能验收

分别报告 1、4、16、32 个 terminal surface：

- 三类 route 的生成、去重和 rollout 数量；
- GroundTrackingPolicy rollout P50/P95/P99/max；
- 预计 tick 成本；
- A* 展开数和总耗时；
- 同作业缓存命中；
- deadline miss。

门槛：

- 默认规划期限 `0.5 s`；
- 控制生产 P95 `≤8 ms`、P99 `≤15 ms`、最大值 `<30 ms`；
- 完整夹具最大值 `<50 ms`；
- deadline miss 为 `0`；
- 结果不依赖机器速度或墙钟完成顺序。

## 10. Fabric 验收

最低覆盖：

- 直线、X→Z、Z→X；
- 起点即满足和需要移动；
- 四方向 normal／首条晚1；
- 三个合法代表速度；
- 单帧丢输入和外力偏离；
- 目标修订；
- 相关世界变化与无关世界变化；
- 停滞后的有界重规划；
- 严格动作对照。

必须走 profile 4、Runtime、NavigationSession、PlannerWorker、RouteAdmitter、ActionRouteExecutor 和真实输入账本。保存真实观察、搜索边、预计成本、逐 tick 输入、实际应用 tick、停止尾迹、重锚、重规划、完成状态和来源注销。

## 11. 止损和通过条件

最多允许两轮局部修正。任一轮若需要修改通用 A*、新增 Session 生命周期状态、恢复面积阈值或候选重试、把入口压成近停、或削弱严格动作证明，立即停止。

通过要求：

- 剩余 9 项完成，原成功退步 `0`；
- rollout 与执行共享唯一纯策略；
- 末段预计成本进入一次 A*；
- 正式执行从真实状态逐 tick 闭环；
- 偏离和停滞有界处理；
- 严格动作证据不变；
- Windows、D061、规划性能和 Fabric 全部通过。

通过后只解除 F2-SG 当前阻塞。F2-SG、F2-S 和路线优化器仍按各自剩余门槛推进。

## 12. 本轮实际结果

### 12.1 已通过部分

| 检查 | 结果 | 结论 |
|---|---:|---|
| 纯策略、FixedRoute 等价、路线 rollout、F2-ST 与区域规划定向检查 | `51/51` | 通过 |
| 普通策略生成跳跃／潜行／疾跑许可 | `0` | 通过 |
| 通用 A*、Session 生命周期、严格动作证明的生产改动 | `0` | 通过 |

实际命令：

```powershell
.venv\python.exe -m unittest tests.motion_nav.test_f2gp_ground_tracking_policy tests.motion_nav.test_fixed_route_walk tests.motion_nav.test_f2_non_center_ground_route tests.motion_nav.test_f2st_terminal_approach tests.motion_nav.test_goal_region_planning
```

### 12.2 未通过门槛

批次 3 的 F2S-C-04 正式链仍返回 `planning_no_known_route`。窄完成区域内，策略会在进入区域和降低速度之间振荡。两轮局部评分修正均未使它稳定完成，因此按冻结止损规则撤销。

这不是 A* 找不到图路径。问题发生在 terminal route rollout。正式普通 Walk 已按玩家 tick提交，纯策略却仍固定用两 tick同向输入评分，再计算松键停止尾迹。窄完成区域前，一 tick控制加完整尾迹可以安全，固定两 tick模型却把候选排除。

后续 [D081](../decisions/0081-select-ground-control-duration-from-stopping-margin.md) 要求根据停止距离和 completion 余量确定 `control_ticks=1|2`。一 tick仍验证完整尾迹，并且 policy、`FixedRouteDecision`、移动意图和账本必须使用同一个值。精细一 tick输入晚到时要压下并重锚，不能使用 D068 的晚启动余量。

### 12.3 未运行部分

以下检查没有获得运行授权，也不得从本轮定向检查推断为通过：

- F2-S v9、v8、v7 大集合；
- 五组行为、严格动作和协调集合；
- 完整 motion_nav 正序和逆序；
- D061；
- 1／4／16／32 终点性能矩阵；
- Fabric 正式链。

机器可读边界见 `evidence/motion_navigation/F2GP-ground-tracking-policy-v1/stop-report.json`。阶段结论为“未通过”；F2-SG 当前阻塞保持打开。
