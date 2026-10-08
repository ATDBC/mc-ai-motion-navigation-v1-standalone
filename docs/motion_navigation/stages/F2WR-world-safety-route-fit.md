# F2-WR：世界安全与路线适用性分离方案

日期：2026-10-08。状态：已实施到正式接线并按 fail-fast 停止；阶段未通过。

**目标：** 保留 F2-CV 已验证的封闭候选和性能，把世界硬安全与 tracking corridor 适用性分成两个 typed 结果，再接回普通 `FixedRoute`。

**依据：** [D084](../decisions/0084-separate-world-safety-and-route-fit.md)、[D083](../decisions/0083-verify-the-closed-ground-candidate-family.md)、[F2-CV 失败记录](../acceptance/F2CV-closed-ground-candidate-verification.md) 和 [D048](../decisions/0048-converge-recovery-by-risk-and-product-evidence.md)。

## 1. 当前事实

- 纯 `GroundCandidateVerifier` 已经能完整分类 `9 × 2` 候选族；
- Windows 1000 样本／场景微基准为 P95 `5.3708 ms`、P99 `5.3963 ms`、最大值 `5.7788 ms`；
- 首次正式接线把 neutral tail 离开 corridor 当作世界不安全；
- remaining9 从历史 `4/9` 退步到 `3/9`，两个历史成功场景退步；
- F2-CV 正式接线已撤出，纯验证器、oracle、typed 预算和微基准保留；
- remaining42、大集合、D061 和 Fabric 没有运行；
- F2-CV、F2-SC、F2-GP、F2-SG 和 F2-S 都保持未通过。

F2-WR 不回写上述失败。它只处理 F2CV-C-01，并重新建立正式接线的前置证据。

## 2. 预计修改范围

允许修改：

- 普通地面候选的世界安全结果；
- 新的 `GroundRouteFit`；
- `GroundCandidateVerifier` 到 `FixedRoute` 的结果映射；
- 中性尾迹依赖的短期持有；
- 实际观察偏离时的 typed `NEEDS_REPLAN` 接线；
- 普通地面 rollout 只使用 `tracking_end` 推进的接线；
- 相关测试、证据和四类文档。

明确不改：

- corridor 宽度、completion、候选种类和评分权重；
- `9 × 2` 候选域、公共前缀、尾迹内核和 typed 预算语义；
- A*、PlannerWorker、Session 状态、等待和重试；
- D048／F8 恢复预算与 owner 交接规则；
- 严格动作证明、输入账本、UNKNOWN 和伤害门槛。

## 3. 批次 0：冻结职责分离 RED

先在不改生产行为的情况下冻结下列反例：

1. 世界安全、输入前缀在 corridor 内、neutral tail 离开 corridor：候选仍有执行资格；
2. 输入前缀离开 corridor，之后 neutral tail 回到 corridor：候选没有执行资格；
3. neutral tail 留在 corridor 内，但会撞墙、失去支撑、接触危险材质或进入 UNKNOWN：世界安全失败；
4. 预测 neutral tail 离开 corridor：不返回 `NEEDS_REPLAN`；
5. 正式观察显示身体已离开 corridor：返回 `NEEDS_REPLAN`；
6. tail 依赖在许可窗口内变化：原安全证明失效；
7. rollout 的下一名义状态是 `tracking_end`，不是 neutral tail 终点；
8. 调换候选顺序或修改便宜分数，世界安全集合不变；
9. 两个 F2-CV 历史退步场景必须先稳定重现。

错误副本至少覆盖：把 `neutral_tail_inside` 合并进硬安全；忽略前缀 corridor；用预测 tail 偏离直接重规划；丢弃 tail 依赖；用 tail 终点推进 rollout。每个错误副本都要被行为断言发现。

## 4. 批次 1：拆分纯结果

建立两个不相互改写的结果：

1. 世界安全结果保留 `SAFE`、`UNSAFE`、`NEEDS_INFORMATION`、`UNSUPPORTED` 和 `BUDGET_EXHAUSTED`，并保存实际读取的世界依赖；
2. `GroundRouteFit` 保存 `control_prefix_inside` 和 `neutral_tail_inside`，并可记录最大偏离用于诊断。

世界安全计算不接收 corridor、completion 或评分参数。路线适用性不修改 typed 世界安全状态。

组合资格的唯一规则是：

```text
eligible = world_safety == SAFE and route_fit.control_prefix_inside
```

`neutral_tail_inside` 只进入诊断。本批不新增评分权重。

**完成条件：** 错误副本全部被检出；世界安全的结果与候选顺序、corridor 宽度和 completion 无关。

## 5. 批次 2：接线短期依赖与真实偏离

1. 将每个已获资格候选的 tail 世界依赖绑定到当前移动决定；
2. 持有期从输入可能生效窗口开始，到新观察和新安全证明接管；没有新证明接管时，保留到最后可能生效 tick 后的完整中性停稳时域结束；
3. 相关 tail 事实变化时立即撤销未生效的旧资格，不撤销已经承担的身体责任；
4. 只有正式观察的身体已离开 corridor 时返回 `NEEDS_REPLAN`；
5. `NEEDS_REPLAN` 继续经过 D048／F8 和当前 owner 的安全收尾，不建新恢复路径。

**完成条件：** 无关世界变化不影响候选；相关变化会使旧资格失效；真实偏离有界地进入现有恢复入口；预测 tail 偏离不购买恢复。

## 6. 批次 3：正式 `FixedRoute` 接线

接回已保留的封闭候选验证器，但正式选择只使用第 4 节的组合资格。

普通 rollout、进展、下一帧预测起点和名义路线都只推进到 `tracking_end`。neutral tail 终点只保留在世界安全、容错诊断和短期依赖中。

先运行：

1. F2CV-C-01 的两个历史退步；
2. F2SC-C-03 四个稳定反例；
3. remaining9。

两个历史成功必须恢复，零安全事件。remaining9 不得低于历史 `4/9`。如果仍需调整 corridor、completion、候选或评分，立即停止。

## 7. 批次 4：Windows 分层回归

前层通过后按下列顺序运行：

1. remaining42；
2. F2-SC／F2-GP 聚焦检查和严格动作负对照；
3. v9、v8、v7、F2、F2-R；
4. 五组行为、协调集合和完整 motion_nav 正序／逆序；
5. D061；
6. Fabric。

Windows 是正式验收平台。任一层失败即停，不用后续大集合淹没前层根因。

## 8. 性能

F2-CV 原微基准保留为历史证据。F2-WR 改变结果边界后要重跑同规格 Windows 微基准，同时报告世界安全、路线适用性和依赖合并的耗时。

门槛保持：

- 纯候选验证 P95 `≤8 ms`；
- P99 `≤15 ms`；
- 最大值 `<30 ms`；
- deadline miss `0`；
- D061 控制生产 P95 `≤8 ms`、P99 `≤15 ms`、最大值 `<30 ms`；
- 完整夹具最大值 `<50 ms`。

`GroundRouteFit` 必须复用已经生成的候选状态，不得为查 corridor 重放一遍物理。

## 9. Fabric 范围

至少覆盖：

- 开阔地长路线和拐弯：neutral tail 暂时离开 corridor，但世界安全且输入前缀在 corridor 内；
- 支撑边缘、坑边、墙边、危险材质和 UNKNOWN；
- 相关 tail 依赖变化与无关世界变化；
- 真实外力把身体推出 corridor，但身体仍处于安全支撑；
- 晚到、丢失、目标修订和路线修订；
- JumpGap 或 ControlledDrop 严格动作负对照。

每帧记录世界安全结果、`control_prefix_inside`、`neutral_tail_inside`、获胜候选、`tracking_end`、tail 终点、短期依赖窗口、实际观察是否偏离、恢复许可和当前身体 owner。

## 10. 止损与关闭条件

出现下列任一情况立即停止：

- 需要改 corridor 宽度、completion、候选数量、顺序或评分权重；
- 世界安全仍读取 corridor、completion 或分数；
- `neutral_tail_inside == false` 仍剥夺候选资格；
- 预测 tail 偏离直接购买恢复；
- 需要新增 Session 状态、等待、重试或并行控制器；
- 短期尾迹依赖无法与长期路线依赖分开；
- 严格动作、UNKNOWN、危险接触或伤害门槛改变；
- 任一历史成功退步或出现新安全事件；
- 微基准或 D061 超限。

F2-WR 只有在 RED、纯结果、依赖生命周期、两个历史退步、remaining9／42、Windows 分层回归、D061 和 Fabric 全部通过后才能关闭。

关闭 F2-WR 只表示 F2CV-C-01 的职责混合已解除。F2-CV 与 F2-SC 仍要按各自未运行门槛继续验收。

## 11. 实际结果

批次 0—2 的核心实现已经保留：

- 纯职责分离和错误反例通过；
- 安全平地上，控制前缀在 corridor 内、tail 在 corridor 外的候选仍有资格；
- 坑、墙、危险材质和 UNKNOWN 继续按世界安全拒绝；
- rollout 使用 `tracking_end`；
- 相关 tail 依赖变化返回 typed `NEEDS_REPLAN`，无关变化不影响当前证明；
- 聚焦检查共 `57/57` 通过。

27 个 edge-guard 失败不是本轮新回归。干净 `7c005bb5` 和候选代码都出现同样的 `27` 个失败；冻结的 16 个行为签名在结果、原因、终点、安全事件、依赖和行为哈希上逐项一致。因此本轮没有修改或跳过这些测试。

正式接线没有达到进入批次 4 的条件：

| 门槛 | 结果 | 判定 |
|---|---:|---|
| C02/C03 | `1/5` | 保留失败 |
| remaining9 | `3/9` | 低于 `4/9` 下限 |
| 两个历史成功 | 恢复 `1/2` | 未通过 |
| 新安全事件 | `0` | 通过 |
| 纯验证器 P95/P99/max | `5.5608/5.5958/5.7890 ms` | 通过 |
| 正式 clutter10 P95/max | `27.6944/30.3298 ms` | 未通过 |
| 正式 clutter20 P95/max | `25.9004/30.2700 ms` | 未通过 |

一次未提交的重复 rollout 去除把正式 P95 降到约 `16 ms`，仍超过 `8 ms`。profile 证明剩余开销来自旧简化门禁和正式验证器两套不同模型，不再存在可以直接删除的同轨迹重放。该试验已撤销。

项目在两轮限定修正后止损。remaining42、大集合、D061 和 Fabric 没有运行；F2-WR、F2-CV、F2-SC、F2-GP、F2-SG 和 F2-S 均保持未通过。证据位于 `evidence/motion_navigation/F2WR-world-safety-route-fit-v1/`。

后续不在 F2-WR 内继续协调两套候选模型。[D085](../decisions/0085-use-one-source-for-ordinary-ground-candidates.md) 和独立 [F2-SS](F2SS-single-ground-candidate-source.md) 已冻结“单一普通地面候选事实源”的迁移顺序。F2-WR 的失败数字和未运行门槛保持原样。
