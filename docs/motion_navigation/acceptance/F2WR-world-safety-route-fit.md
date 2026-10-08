# F2-WR 世界安全与路线适用性验收

日期：2026-10-08。状态：已运行到正式接线门槛并止损；验收未通过。

本验收回答五个问题：世界安全是否不再受 corridor 影响，候选的实际控制前缀是否仍留在路线内，neutral tail 离开路线时是否仍有安全退出，真实偏离是否经过现有恢复入口，以及这项拆分是否仍满足控制期限。

## 1. 冻结基线

| 项目 | 当前结果 | F2-WR 要求 |
|---|---:|---|
| F2-CV 纯验证器 | `18/18` 分类 | 保留 |
| F2-CV Windows 微基准 P95/P99/max | `5.3708/5.3963/5.7788 ms` | 新边界重测且不超门槛 |
| F2-CV remaining9 | `3/9` | 先恢复历史 `4/9` 下限 |
| F2-CV 历史成功退步 | `2` | `0` |
| F2-CV 正式接线 | 已撤出 | 前置门槛通过后重新接线 |
| remaining42／大集合／D061／Fabric | 未运行 | 前层通过后运行 |

历史退步场景为：

- `f2r/clutter/0.1/11/1/product`；
- `f2r/clutter/0.2/9/6/product`。

它们的旧成功、F2-CV 退步和本阶段候选结果必须并排保留，不改写原记录。

## 2. 世界安全独立性

对同一个物理候选，只改变 corridor、completion 或便宜分数，必须保持：

- typed 世界安全状态不变；
- 世界依赖不变；
- 完整前缀数、物理 step 和尾迹停稳结论不变。

分别变异下列检查，至少一个行为断言必须失败：

1. 扫掠碰撞；
2. 支撑和高度连续性；
3. UNKNOWN／缺失事实；
4. 危险材质与伤害规则；
5. 姿态和运动模式；
6. 中性停稳上限；
7. 实际读取依赖的登记。

把 corridor 或 completion 重新放入世界安全结果时，错误副本必须失败。

## 3. `GroundRouteFit`

每个声明候选都必须单独报告：

- `control_prefix_inside`；
- `neutral_tail_inside`；
- 可选的前缀和尾迹最大 corridor 偏离，只用于诊断。

组合表必须满足：

| 世界安全 | `control_prefix_inside` | `neutral_tail_inside` | 执行资格 |
|---|---:|---:|---|
| `SAFE` | 是 | 是 | 有 |
| `SAFE` | 是 | 否 | 有，记录容错质量 |
| `SAFE` | 否 | 任意 | 无 |
| 非 `SAFE` | 任意 | 任意 | 无 |

候选顺序、评分或墙钟快慢不能改变该表。F2-WR 不为 `neutral_tail_inside` 新增排序权重。

## 4. 实际偏离与预测容错

至少覆盖：

- 预测 tail 离开 corridor，正式观察仍在 corridor：不重规划；
- 下一正式观察仍在 corridor：从新观察继续每 tick 闭环决定；
- 正式观察已离开 corridor：路线执行器返回 typed `NEEDS_REPLAN`；
- `NEEDS_REPLAN` 只经过 D048／F8 购买一次任务恢复；
- 身体 owner 在共同交接完成前不释放责任；
- 相同偏离事件重放不重复扣费；
- 恢复预算耗尽时安全收尾并返回现有 typed 结果。

错误副本要能检出：预测 tail 一离开就购买恢复；实际偏离仅记日志但继续沿旧路线走；重规划时立即清空 owner。

## 5. tail 依赖生命周期

对这些时间点分别断言：

1. 候选只被评估，未获资格：tail 依赖不安装到执行链；
2. 候选获胜并开始许可窗口：tail 依赖正常持有；
3. 相关事实在许可窗口内变化：原证明失效，未生效旧输入不再授权；
4. 无关事实变化：原资格不变；
5. 新正式观察重锚定且新安全证明已经接管：旧 tail 依赖释放；
6. 没有新证明接管：旧依赖保留到最后可能生效 tick 后的完整中性停稳时域结束；
7. 目标或路线修订：只撤销未生效资格，已承担的身体责任正常收尾。

这些依赖不得永久加入路线依赖，也不得因为目标修订泄漏或无上限增长。

## 6. rollout 和名义路线

对一 tick、两 tick、晚到和部分生效分别核对：

- rollout 只向前推进一个玩家 tick；
- 下一名义状态来自 `tracking_end`；
- neutral tail 终点不改变路线进展、动作成本或下一步起点；
- 账本确认的实际生效前缀与新观察重锚定；
- 不回放旧 tail 终点冒充现实身体。

错误地用 tail 终点作为 rollout 输出时，状态、路线进展或后续候选断言必须失败。

## 7. 封闭候选和预算

D083 的门槛全部保留：

- 九种按键、一／两 tick 许可，共十八个声明候选；
- 全部候选取得 typed 结果后才能选择；
- 候选顺序不改变安全集合；
- `BUDGET_EXHAUSTED` 不返回部分候选集；
- 中性尾迹未证明时不发新输入；
- 不为预算耗尽增加无界等待或重试。

世界安全与路线适用性的分离不得减少前缀或尾迹验证数量。

## 8. 分层行为门槛

Fail-fast 顺序是：

1. 纯职责分离、错误副本和依赖生命周期；
2. F2CV-C-01 的两个历史退步；
3. F2SC-C-03 四项；
4. remaining9；
5. remaining42；
6. F2-SC／F2-GP 聚焦层和严格动作负对照；
7. v9、v8、v7、F2、F2-R；
8. 五组行为、协调集合和完整 motion_nav 正序／逆序；
9. D061；
10. Fabric。

第 2 层必须 `2/2` 恢复。remaining9 至少恢复历史 `4/9`。后续层历史成功退步、新安全事件、伤害超额、来源泄漏和终态仍持有输入都必须为 `0`。

## 9. 性能门槛

Windows 是正式平台。重跑与 F2-CV 相同规格的候选微基准，单独报告世界安全、路线适用性、依赖合并和总耗时。

- P95 `≤8 ms`；
- P99 `≤15 ms`；
- 最大值 `<30 ms`；
- deadline miss `0`。

D061 保持生产控制 P95 `≤8 ms`、P99 `≤15 ms`、最大值 `<30 ms`；完整夹具最大值 `<50 ms`；deadline miss `0`。

新结构必须重用已计算的候选轨迹。为生成 `GroundRouteFit` 重放物理轨迹的错误副本必须被计数或性能门槛发现。

## 10. Fabric 正式链

Fabric 必须走 profile 4、Runtime、NavigationSession、PlannerWorker、RouteAdmitter、ActionRouteExecutor 和真实输入账本。

至少验证：

- 开阔地长路线和四方向拐弯；
- neutral tail 出 corridor 但实际前缀留在内的正例；
- 支撑边缘、坑边、墙边、危险材质和 UNKNOWN 反例；
- 相关／无关 tail 依赖变化；
- 真实外力偏离后的 `NEEDS_REPLAN`、F8 和 owner 安全收尾；
- 晚到、丢失、目标修订和路线修订；
- JumpGap 或 ControlledDrop 严格动作对照。

证据必须能区分预测容错和实际偏离，不能只记录一个“路线不合格”布尔值。

## 11. 严格动作负对照

Sprint、Sneak／探边、Crawl、Swim、Climb、JumpGap、JumpUp、ControlledDrop、离地和连续高度严格入口，继续使用原证明、生效窗口、落地责任和伤害额度。

错误地让这些动作通过 F2-WR 普通地面语义获准时，负对照必须失败。

## 12. 关闭判定

只有第 2—11 节全部通过，F2-WR 才能关闭。

通过表示普通地面候选不再因预测 neutral tail 暂时离开 corridor 而被冒充为世界不安全，也不代表 F2-CV、F2-SC、F2-GP、F2-SG、F2-S 或路线优化器已经通过。

## 13. 本轮验收结果

### 13.1 已确认的职责边界

- 世界安全与 `GroundRouteFit` 已分开；
- 安全候选即使 neutral tail 出 corridor，只要控制前缀仍在 corridor 内，仍有执行资格；
- 控制前缀出 corridor 时没有资格；
- 坑、墙、危险材质和 UNKNOWN 没有被路线适用性覆盖；
- 预测 tail 出 corridor 不触发重规划；正式观察出 corridor 才返回 `NEEDS_REPLAN`；
- tail 依赖在证明时域内持有，相关变化失效、无关变化保持；
- 受影响的聚焦检查为 `57/57`。

### 13.2 edge-guard 原有失败

`tests.motion_nav.test_f2_ground_route_edge_guard` 在干净 `7c005bb5` 和本轮候选上都是 15 个测试、27 个失败。冻结的 16 个正式场景签名逐项一致。它们是进入本轮前已经存在的 RED，不作为 F2-WR 新回归，也没有被改成 expected failure。

### 13.3 正式行为

| 集合 | 结果 | 安全事件 |
|---|---:|---:|
| C02/C03 | `1/5` | `0` |
| remaining9 | `3/9` | `0` |

历史成功 `f2r/clutter/0.2/9/6/product` 已恢复；`f2r/clutter/0.1/11/1/product` 仍退步。remaining9 没达到 `4/9`，所以 remaining42 和后续大集合没有运行。

### 13.4 性能和止损

纯验证器的 Windows 1000 样本／场景结果为：

- P95 `5.5608 ms`；
- P99 `5.5958 ms`；
- 最大值 `5.7890 ms`；
- deadline miss `0`。

正式链的 retained 结果为：

- clutter10 P95 `27.6944 ms`，最大值 `30.3298 ms`；
- clutter20 P95 `25.9004 ms`，最大值 `30.2700 ms`。

一次未提交的重复 `safe_tail` rollout 去除把正式 P95 降到约 `16 ms`，仍超过 `8 ms`，remaining9 也仍是 `3/9`。剖析结果显示：71 次旧策略候选计算累计 `0.727 s`，37 次正式验证累计 `0.351 s`；正式轨迹的路线投影只有约 `0.012 s`。剩余成本来自两套不同精度模型，不能在不改变候选触发和排序的前提下继续删除。

因此本轮按冻结规则止损。未运行 remaining42、分层大集合、D061 和 Fabric。完整紧凑证据见 `evidence/motion_navigation/F2WR-world-safety-route-fit-v1/`。

后续由 [D085](../decisions/0085-use-one-source-for-ordinary-ground-candidates.md) 和 [F2-SS 验收](F2SS-single-ground-candidate-source.md) 处理两套候选计算。F2-WR 仍为未通过；后续结果不能回写本节失败。
