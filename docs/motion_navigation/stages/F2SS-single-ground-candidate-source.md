# F2-SS：普通地面候选单一事实源方案

日期：2026-10-08。状态：核心接线已保留；v8 出现三项历史成功退步，按 fail-fast 暂停，阶段尚未通过。

**目标：** 正式普通 `STANDING + WALK` 每帧只调用一次 `GroundCandidateVerifier`，让完整十八项报告同时服务世界安全、路线质量和候选选择，删除第二套普通 Walk 物理推演。

**依据：** [D085](../decisions/0085-use-one-source-for-ordinary-ground-candidates.md)、[D084](../decisions/0084-separate-world-safety-and-route-fit.md)、[F2-WR 失败记录](../acceptance/F2WR-world-safety-route-fit.md)和 [F2-CV](F2CV-closed-ground-candidate-verification.md)。

## 1. 当前事实

- F2-WR 已完成世界安全与 `GroundRouteFit` 的职责分离；
- 纯 `GroundCandidateVerifier` 的 Windows P95/P99/max 为 `5.5608/5.5958/5.7890 ms`；
- 正式链仍先运行旧简化候选，再运行正式验证器，P95 约为 `26—28 ms`；
- 一次未提交去重试验只能降到约 `16 ms`，仍超过 `8 ms`；
- remaining9 为 `3/9`，两个历史成功只恢复一个；
- remaining42、大集合、D061 和 Fabric 没有运行；
- F2-WR 保持未通过，不能把已完成的职责分离记作阶段通过。

F2-SS 只消除正式普通 Walk 的重复候选事实源。它不继续修补两套模型之间的映射。

## 2. 预计修改范围

允许修改：

- `ground_candidate_verifier.py` 的完整报告契约；
- `ground_tracking_policy.py` 的路线／completion 质量与 typed 选择；
- `fixed_route.py` 的普通 `STANDING + WALK` 接线；
- 普通 Walk 旧 rollout、evaluate、safe-tail 和 replay 入口；
- 对应测试、证据和四类文档。

明确不改：

- A*、PlannerWorker、Session、Runtime 和输入账本；
- corridor、completion、候选输入、候选数量和评分权重；
- F8、身体 owner 和恢复预算；
- 严格动作、UNKNOWN、危险接触和伤害门槛；
- Sprint、Sneak／探边、Crawl、Swim、Climb 和离地动作。

## 3. 批次 0：冻结逐项基线

在不改生产行为前，保存三类证据。

### 3.1 十八项 oracle

对每个 `(forward, strafe, control_ticks)` 比较：

- typed 安全状态；
- `tracking_end`；
- `stopped_end`；
- 前缀与中性尾迹；
- 世界依赖；
- 验证路径和缺失事实。

oracle 使用正式 1.21 规则逐项计算，不调用 `GroundTrackingPolicy` 的简化 rollout。候选枚举顺序扰动后，按身份比较的结果必须相同。

### 3.2 正式行为

先冻结 F2-WR 已经运行的聚焦场景、C02/C03、remaining9 以及两个历史退步。保存终态、原因、最终身体、伤害、安全事件、输入、恢复和轨迹签名。

这些记录是迁移对照，不改写 F2-WR 的失败结果。

### 3.3 调用计数

为正式普通地面决策帧记录：

- `GroundCandidateVerifier` 调用次数；
- 每次报告的候选数；
- 普通 Walk 旧 rollout、evaluate、safe-tail 和 replay 的入口次数；
- 1.21 计算器 step、查询缓存和物理前缀数量。

RED 必须证明当前正式链存在重复候选计算。检查不能只按函数名搜索，必须让正式 `FixedRoute` 场景实际走到对应入口。

## 4. 批次 1：冻结完整报告契约

让 `GroundCandidateVerifier` 一次返回十八项稳定有序的候选。每项至少包含：

```text
identity
typed_status
tracking_end
stopped_end
verified_prefix
verified_neutral_tail
dependencies
verification_path
```

约束如下：

1. `tracking_end` 是控制前缀结束时的状态；
2. `stopped_end` 是完整中性尾迹的终点；
3. 依赖是前缀和尾迹实际读取事实的并集；
4. 所有十八项结束后，family 状态才可为 `COMPLETE`；
5. 任一项预算耗尽时，family 返回 typed `BUDGET_EXHAUSTED`；
6. 部分已完成候选不向选择层开放；
7. 输出顺序固定为 `CLOSED_GROUND_MOVEMENTS` 的既有顺序，每个方向内先一 tick、再两 tick，不受墙钟、分数和历史胜者影响。

先用错误副本证明下面的错误会失败：交换 `tracking_end` 和 `stopped_end`、漏依赖、缺候选、把部分报告标为完整、按完成先后排序。

## 5. 批次 2：把策略收窄为质量和选择

`GroundTrackingPolicy` 的输入改为完整验证报告、路线几何和 completion。它只做：

- `control_prefix_inside` 与 `neutral_tail_inside`；
- 路线进展、横向偏差和 completion 距离；
- terminal 历史进展；
- 现有质量键；
- typed 选择结果。

它不再推演身体，不再查询世界，也不再产生安全状态。

候选选择必须满足：

```text
family == COMPLETE
world_safety == SAFE
control_prefix_inside == true
```

`neutral_tail_inside == false` 继续只作为 D084 规定的容错质量。质量层不得覆盖 `UNSAFE`、`NEEDS_INFORMATION`、`UNSUPPORTED` 或 `BUDGET_EXHAUSTED`。

## 6. 批次 3：接入 `FixedRoute` 并退出旧路径

普通 `STANDING + WALK` 每个决策帧执行固定顺序：

1. 构造正式身体、路线和世界查询上下文；
2. 调用一次 `GroundCandidateVerifier`；
3. 把完整报告交给 `GroundTrackingPolicy`；
4. 选择获胜候选；
5. 用报告中的 `tracking_end` 推进名义路线；
6. 持有获胜项的尾迹依赖和证明时域。

接线完成后，普通 Walk 的旧 rollout、evaluate、safe-tail 和 replay 入口必须删除，或由调用计数证明正式路径无法进入。不能保留“新报告失败时退回旧简化候选”的兼容分支。

严格动作继续走原入口。普通 Walk 与严格动作的负对照必须同时通过。

## 7. 批次 4：性能门槛

先在 Windows 上运行正式 `FixedRoute` 代表最坏场景，并同时记录验证器、策略选择、依赖合并和整帧时间。

门槛为：

- 生产控制 P95 `≤8 ms`；
- P99 `≤15 ms`；
- 最大值 `<30 ms`；
- deadline miss `0`；
- 每个进入普通 Walk 候选决策的正式帧，验证器调用恰好为 `1`。

首次超限时只允许一轮低风险优化。可做的优化限于：同帧查询缓存、公共前缀、已生成轨迹和不可变报告复用。不得减少十八项候选、缩短尾迹、恢复简化安全筛选或按墙钟改变结果。

一轮优化后仍超限，立即停止。保留报告契约和证据；不运行 remaining42、大集合、D061 或 Fabric。

## 8. 批次 5：分层行为回归

性能通过后，按 fail-fast 顺序运行：

1. F2-WR 两个历史退步；
2. C02/C03 和 remaining9；
3. remaining42；
4. F2-SC／F2-GP／F2-SG 聚焦检查与严格动作负对照；
5. v9、v8、v7、F2 和 F2-R；
6. 五组行为、协调集合和完整 motion_nav 正序／逆序；
7. D061；
8. Fabric。

两个历史成功必须恢复，remaining9 不得低于历史 `4/9`，后续所有冻结集合的旧成功退步、新安全事件、伤害超额、来源泄漏和终态仍持有输入都必须为 `0`。

任一层失败即停，不用大集合覆盖前层根因。

## 9. A* 后续边界

F2-SS 不修改 A*。将来修 terminal 成本时，只能在搜索已经缩小到少量终点状态后复用候选物理核心。

不得在大图每个展开节点运行完整十八项验证。若局部 terminal rollout 仍超出后台规划预算，应先减少终点状态或复用缓存，不能把在线控制的完整验证直接搬进全图搜索。

## 10. Fabric 范围

Fabric 必须走 profile 4、Runtime、NavigationSession、PlannerWorker、RouteAdmitter、ActionRouteExecutor、`FixedRoute` 和真实输入账本。

至少覆盖：

- 开阔地直线与四方向拐弯；
- 墙边、坑边、支撑边缘、危险材质和 UNKNOWN；
- neutral tail 出 corridor、控制前缀仍在 corridor 的 D084 正例；
- terminal 超调和反向修正；
- 晚到、丢失、目标修订、路线修订和相关世界变化；
- JumpGap 或 ControlledDrop 严格动作负对照。

每帧记录验证器调用数、十八项身份与状态、`tracking_end`、`stopped_end`、依赖、质量键、获胜候选、实际生效 tick 和身体 owner。

## 11. 止损与关闭条件

出现下列任一情况立即停止：

- 正式普通 Walk 同一帧调用验证器超过一次；
- `GroundTrackingPolicy` 重新 rollout、查世界或判断硬安全；
- 部分候选报告参与选择；
- 候选顺序或墙钟改变安全集合；
- 需要调 corridor、completion、评分或候选数；
- 需要新增 Session 状态、等待、重试或后台工作；
- 严格动作、UNKNOWN、危险接触或伤害门槛改变；
- 一轮低风险优化后仍超过 8／15／30 ms；
- 任一历史成功退步或出现新安全事件。

F2-SS 只有在逐项 oracle、唯一调用、旧路径退出、性能、remaining9／42、大集合、D061 和 Fabric 全部通过后才能关闭。

关闭 F2-SS 表示普通地面在线候选只剩一个物理事实源。它不自动关闭 F2-WR、F2-CV、F2-SC、F2-GP、F2-SG、F2-S 或路线优化器；这些阶段仍按各自未完成门槛签署。

## 12. 本轮实施结果

本轮已完成并保留下面的结构改动：

- 正式普通 `STANDING + WALK` 每个候选决策帧只生成一份十八项验证报告；
- 报告显式保存 `tracking_end`、`stopped_end`、前缀、完整中性尾迹和世界依赖；
- `GroundTrackingPolicy` 只读取完整报告，计算路线与 completion 质量并作 typed 选择；
- `FixedRoute` 的跟踪、制动、交接和完成判断共用同一份报告；
- 正式路径不再进入旧的普通 Walk rollout、world query、safe-tail 或 replay。历史组件夹具仍可走显式的 component-only 入口，但 profile 4 正式路径不能进入；
- 严格动作在普通验证器之前分流，原证明链保持不变。

Windows 聚焦检查为 `87/87`，其中阶段核心层为 `44/44`。纯验证器 3000 个样本的 P95/P99/max 为 `5.3776/5.5914/6.4265 ms`；正式控制微基准 770 帧为 `5.6667/5.9748/16.5341 ms`，deadline miss 为 `0`。因此继续运行分层行为集合。

行为结果如下：

- 两个 F2-WR 历史退步恢复为 `2/2`；
- remaining9 为 `6/9`，其余三项有界返回 `no_safe_ground_candidate`；
- remaining42 为 `42/42`；
- v9 新层为 `188/216`，原安全门槛通过；
- v8 杂乱层为 `1747/1800`，比冻结结果 `1715/1800` 多完成 32 项，但有三项原成功退步。

三项退步都是窄 completion 的末端精度问题：身体或完整停止点只超出区域 `0.0061—0.0229` 格，十八项候选的世界安全均成立，但离散键盘的一 tick输入无法在不越过窄区域的情况下完成修正。旧记录来自简化完成模型，可能是假阳性；在用正式 1.21 物理重新证明前，既不能删除旧成功，也不能把当前失败改写成成功。

## 13. 当前停止点

本轮在 v8 首次发现历史成功退步后立即停止。没有运行 v7、F2／F2-R、五组行为、协调集合、完整 motion_nav、D061 或 Fabric。

阶段暂不最终关闭。后续必须另行决定怎样表达窄 completion 与键盘控制分辨率的关系。这个决定不能通过扩大 completion、调评分、增加候选或恢复简化模型来绕过。现有单一事实源、性能改进和 typed 报告先保留，等待下一轮设计与复核。

## 14. 后续移交

[D086](../decisions/0086-solve-bounded-ground-terminal-sequences.md) 已把下一轮工作独立为 [F2-TS](F2TS-bounded-ground-terminal-sequences.md)。F2-TS 用有界多 tick末段序列处理控制分辨率，不修改本阶段的十八项单帧候选、completion 或安全门槛。

本阶段继续保持暂停。三个 v8 场景仍是冻结 RED，不因新方案建立而改记为能力已恢复。F2-TS 完成独立 holdout、变异和性能门槛后，才允许回放这三个 oracle；通过后再回到本阶段尚未运行的 v7、大集合、D061 和 Fabric。
