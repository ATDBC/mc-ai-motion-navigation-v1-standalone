# D121：先证明带安全尾段的动量交接，再考虑规划接入

日期：2026-10-19，Task 5 结果补充于2026-10-20。状态：**PARTIAL／STOPPED（同一 D121，仅组件范围）。Task 4 及时输入组件执行 PASS；真实 transport 把 `[L+1,L+1]` 后继拖到 L+2 后仍会物理采样，修复需要修改本阶段禁止的 ActionSnapshot／wire／Java。S0、S1 和历史 S2 direct-copy 结果保留，S4 与 Fabric 未运行。**

关联：[实施计划](../stages/d121-fallback-backed-momentum-handoff-plan.md)、[验收](../acceptance/d121-fallback-backed-momentum-handoff.md)、[D117](0117-close-prepared-successor-handoff.md)、[D120](0120-resume-momentum-entry-calibration.md)。

## 1. 要解决什么问题

D120 已证明冻结的 972 个落地入口和三种后缀与实机校准结果一致，但它没有证明这些样本之间的所有状态都安全，也没有把连续两跳接入正式执行链。

下一步要解决的是一个更小的问题：第一跳已经承诺后，机器人落到短平台时，必须在下一拍执行两个互斥选择中的一个：

- 第二跳证明仍然有效时，继续第二跳；
- 第二跳不可用时，执行已经证明的潜行尾段，留在平台上。

D121 开始时，安全证明把“松开全部输入”写死为唯一收尾。S1 已完成声明式 fallback，prepared proof 和在线求解器都能证明潜行尾段。本次继续解决消息表达和落地交接，不重做 S0／S1。

历史 S2 的测试侧 direct-copy 联合包重复序列化当前与后继完整证明，超过 2 MiB，保留为失败的替代方案。临时生产实现已撤回，生产联合消息没有运行。原 `s2/stop.json` 不改写；当时授权的组件修订继续使用 D121，也不把历史替代方案的失败当作当前阶段必须终止的依据。

## 2. 本轮决定

先建立一个通用的“严格动作后继或安全尾段”契约。它描述一个当前动作、一个严格后继和一个已经证明的 fallback，不写 `JumpGap -> JumpGap` 组合特判。

第一跳越过最后可放弃点前，必须同时满足：

1. 从预计出口可以证明后继的固定输入；
2. 后继未获批时，有一条绑定同一世界、generation、owner、绝对 tick、依赖和伤害额度的 fallback；
3. Runtime 能在看到 authoritative landed post-state `L` 的当帧作出选择，并让唯一选中的输入在 `L+1` 生效。

采用更小的表达：不新增联合证明消息。修订后的当前动作证明须沿用 S1 已证明的精确出口 fallback，安全性由这条尾段承担。当前证明只增加紧凑的后继可行性绑定，记录同一请求／generation、预计出口、后继固定输入身份和摘要、精确出口 fallback 身份等必要对应关系。可行性绑定只说明固定后继已被核对，不能代替完整后继证明或交接批准。

完整后继证明继续使用现有 D117 prepared 消息、registry 引用和接纳／apply／清理生命周期。当前完整证明和后继完整证明各自在原有消息中只出现一次，其他关联只传紧凑身份和摘要；请求、绑定和结果之间不得再次嵌入完整对象图。摘要在后台或准备阶段计算，控制帧和 `L` 帧只比较已算好的值。

这里的“已证明 fallback”目前只在精确出口成立，不能声称 S1 已证明 D120 整条第一跳。现有 `VerifiedMotionResult` 要求每个轨迹边界都有 `FallbackTailProof`；实测边界 15、23、24、25 立即执行 neutral／back／sneak 都失败，只有 `L=26` 的 sneak 成功。修订实现必须先建立 RED：承诺后是否能以 typed 引用表示“继续已证明主轨迹后缀到 `L`，再执行精确出口 fallback”。引用应复用主轨迹切片，不复制状态，不增加联合消息；该合法当前证明尚未实现。

在 `L`：

- 默认选择已证明的 sneak fallback，在 `L+1` 生效；
- 后继入口、依赖、时序和 D117 交接批准都仍有效时，才替换为后继；
- 后继的首条输入窗口严格为 `[L+1,L+1]`；未在 `L+1` 生效即过期，不得推迟到 `L+2` 起跳；
- 两条路径只能有一个 movement winner；
- fallback 完成前，当前 owner 不能释放身体责任。

以上是待实现、待验证的契约。须区分两种迟到：到 `L` 仍未 READY 的后继，可由 Python 选择 fallback；在 `L` 已提交、却在客户端 `L+2` 才实际生效的后继，现有代码只能事后发现。`CandidateExecutionWindow` 的精确窗口没有传入 `ActionSnapshotV1`，也没有随它到达 Java／backend。因此第二种情况必须作为 RED 和完成门槛，不能声称已阻止传输迟到。Java 仍不在授权范围；若现有链无法稳定保证 `L -> L+1`，按第 6 节停止。

## 3. D120 证据怎么使用

D120 的有限格点只用于回归、性能估算和发现边界。它不能变成 `SegmentEntryWindow`，也不能用外接框给未枚举状态放行。

真实落地状态必须使用同一次请求绑定的固定输入和当前世界事实重新核对。若要从连续范围直接接纳，必须另行给出连续安全证明；D121 不做这件事。

## 4. 授权范围

D121 授权：

- 组件 RED、固定输入重放和控制路径成本测量；
- 通用 typed fallback 及其证明；
- 在现有 worker、prepared registry、D117 原子交接和唯一 Runtime 输入出口内完成落地二选一；
- 组件后端的四方向、延迟、失联、依赖变化和负载矩阵；
- 必要的文档、测试、精简证据和公开导出登记。

本次修订不新增 owner、账本或 worker 管线，不新增 Session 状态。规划接入、生产四格 profile、Java 和 Fabric 均保持未授权。

D121 不授权：

- 修改规划器、路线接纳或 Session；
- 新增正式四格跨隙 profile 或组合边；
- 运行新的 Fabric 产品矩阵；
- 修改 Java 协议；
- 放宽 UNKNOWN、伤害规则、动作入口或全局数值容差；
- 把 D120 有限样本声明为连续包络。

## 5. 性能和安全边界

落地当帧只允许固定输入的有界重放、身份比较和依赖复核，不能运行候选搜索。行为不能读取墙钟；墙钟只用于验收。

现有单消息上限保持 2 MiB。代表场景的每条正式请求和结果消息都必须不超过上限的 50%，即 `1,048,576` bytes；同时报告规范编码和实际传输编码体积。不能通过增加上限或拆出第二条 worker 管线通过验收。

完成门槛是在“一格间隙—一格平台—四格间隙”（1-1-4）组件课程中：已准备且有效的后继完成跨越；被拒绝、过期、依赖变化或迟到的后继都选择已证明 sneak fallback，停在中间平台。全部分支必须零伤害、单一 movement winner、owner 无空档。后继依赖变化用例必须保留 fallback 自身的有效依赖；不能把 UNKNOWN 或失效支撑当作安全。

先测量 `L` 帧的实际物理步数和 Windows 控制耗时。若固定重放也超过控制预算，阶段停止，不能通过增加缓存层、放宽门槛或把未知状态当作安全来绕过。

## 6. 停止条件

出现任一情况立即停止并保留证据：

- 需要把有限样本升级成连续安全包络；
- 第一跳承诺前无法证明 fallback；
- `L` 帧选择不能稳定在 `L+1` 生效；
- 需要新增 Session 状态、第二个 owner、第二本账本或第二个输入出口；
- 需要修改规划器或生产四格 profile 才能验证组件语义；
- 需要放宽 UNKNOWN、伤害规则或全局容差；
- 同一组合特判扩散到三个或更多模块；
- 两轮限定修正后仍出现新的跨层阻塞；
- 出现旧成功退步、安全违规、无界等待或无关行尾变化。

阶段通过后，规划组合边和 Fabric 仍需新的决定授权。

## 7. Task 5 补充决定：按真实 transport 门槛停止

2026-10-20，Task 4 的及时组件链已经通过。下一项按原决定检查：Python 在 L 提交窗口严格为 `[L+1,L+1]` 的后继，但 transport 到 L+2 才让客户端物理采样。

组件证据显示，过期 jump 在 L+2 真实生效，Python 输入账本随后才把它分类为 `APPLIED_OUTSIDE_WINDOW`。现有 `ActionSnapshotV1` 和客户端 wire 不携带 exact movement tick window 或 prepared generation，所以客户端在物理前没有这项判断依据。

这属于原决定已经登记的停止条件。修复需要扩展 action contract／wire／Java，超出当前“无 Java、无协议变化”的授权。D121 因此保持已经通过的 Task 2—4 切片，整体改为 `PARTIAL／STOPPED`；不运行 S4、Fabric 或规划接入。后续 D122 尚未开始，须在发布修复全部通过后另写决定。客户端首效窗口、安全默认输入和握手兼容边界需要一起定义，不能把 receipt 的事后检测写成通过。

## 发布修复 Task 4：诊断与入口更正（2026-10-20）

新的 Task 5 诊断使用 v2 schema，分别记录 `applied_in_window`、`applied_outside_window` 和 `not_delivered`。及时和未送达都没有执行“协议在物理前拒绝过期输入”的检查，因此 `expired_application_prevention=not_applicable`，兼容字段 `protocol_prevented_expired_application=null`。null 不表示成功；晚到后仍应用则明确为 `not_prevented` 和 false。

原 Task 5 证据不回写。新结果追加到 `evidence/motion_navigation/d121-momentum-handoff-v1/release-repair/task-4/`；三场的物理、输入和停止结论保持不变，聚合仍为 `FAILED_STOPPED`。公开入口统一为 `PARTIAL／STOPPED`；D122 尚未开始，必须先完成发布修复的全量 Windows 与独立快照门禁。
