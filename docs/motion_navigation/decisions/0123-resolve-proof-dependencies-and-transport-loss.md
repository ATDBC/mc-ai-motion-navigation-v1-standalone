# D123：先查清证明依赖，再区分传输事实

2026-10-20，D123-1 已明确授权并执行到固定视点只读验证，现为 **PARTIAL／STOPPED，独立审查 PASS／已签署**。全部 67 个依赖（含 12 个下层格）保留。Windows 原生可见性核心完成四方向共 1248 个相机朝向样本，每方向 12 格均未出现可见空气；其中 9 格另有不依赖相机朝向的完整立方体遮挡论证。仅转头无法补齐所有依赖，继续需要改变观察位置或新增 sensing，触发预登记停止条件。未启动新的 Minecraft／Fabric，未提交 movement trial；actor-only 实机校准和两阶段启动命令未交付。D123-0 继续 PARTIAL／STOPPED，删除依赖未授权；本次签署仅批准 D123-1 PARTIAL／STOPPED 的停止收尾，不授权 D123-2；D123-2／D123-3 未开始。见 D123 验收第 10 节和 `evidence/motion_navigation/d123-proof-and-transport-v1/d123-1/fixed-eye-run-2/stop.json`。


日期：2026-10-20。状态：**D123-0 PARTIAL／STOPPED，已签署；D123-1 PARTIAL／STOPPED，独立审查 PASS／已签署**。

关联：[D122](0122-enforce-first-effect-window-and-prearm-safe-default.md)、[阶段计划](../stages/d123-proof-dependency-and-transport-loss-plan.md)、[验收](../acceptance/d123-proof-dependency-and-transport-loss.md)。

## 1. 要解决什么问题

D122-3 已证明首效窗口和预装安全默认可以在正式组件链工作。D122-4 还没有取得真实协议样本。第一场 Fabric 试次开始前，67 个证明依赖中有 12 个保持 `UNKNOWN`；其中 10 个被遮挡，2 个在视野外。夹具知道这些位置是空气，正式 actor 不知道，因此不能继续。

同一轮复核还确认，现有 `never` 把三种不同事实混在了一起：

1. 命令在发送前被本地拦下，或远端在任何输入绑定／采样前明确拒绝整条 request，因此可以证明没有应用；
2. 命令已交给传输层，但是否送达、应用仍未知；
3. 整个客户端通道失联，Python 无法取得失联后的新观察。

第一种情况只有在通道仍活、并取得新观察后才能正式收尾。后两种情况必须立即报告身体责任尚未重新确认，不能用旧观察证明默认实际执行、身体着地或健康，也不能声称 source 已经安全释放。

D123 先关闭这两个设计缺口，再恢复 Fabric。它不新增运动能力。

## 2. 证明依赖先做只读实验

先固定 D122 的第一跳、第二跳和潜行尾段，不修改生产代码。对 12 个下层格逐格改变事实，记录碰撞、安全和依赖收集为什么读取它们。

每个格子至少比较 `AIR`、不会伤人的完整方块和 `UNKNOWN`。上方已知支撑保持不变。实验必须比较：

- 主轨迹和出口状态；
- 风险区间、伤害和 fallback；
- 接纳结果和最终依赖集合；
- 去掉或改变上方支撑后的结果。

结论按“格子＋读取原因”分类，同一个格子可以有不同结论：

- **在严格屏障前提下无关**：已知完整支撑把身体扫掠与下层空间隔开，且该读取不参与其他风险判断。此时可以从依赖中移除；该下层格为 `AIR`、完整方块或 `UNKNOWN` 都不得改变结果。
- **仍参与结论**：格子会影响碰撞、扫掠、支撑、伤害或 fallback。此时必须保留依赖，并通过真实 actor 的新观察位置或分阶段观察取得事实；事实变成 `UNKNOWN` 必须拒绝。

允许混合方案：只短路已经证明无关的读取，只观察仍然必要的读取。屏障缺失、碰撞形状变化或身体可能进入下层空间时，已移除的依赖必须恢复；对应事实为 `UNKNOWN` 时必须返回 `NEEDS_WORLD`。不能为了通过夹具预选方便的分类。只读实验在一个批次内无法给出明确结论时，D123 停止。

mutation 同时覆盖两面：仍必要的单格和相邻两格变成 `UNKNOWN` 或实体时，检查必须拒绝或改变结论；已短路的格在严格前提成立时，`AIR`、完整方块和 `UNKNOWN` 必须等价。还要同时改变支撑碰撞形状与扫掠边界，确认前提失效后依赖恢复。短路只能依据碰撞屏障和扫掠几何，不能按方块名称维护白名单。

## 3. 三种传输事实使用不同终态

### 3.1 命令已证明未应用，通道仍活

只有两种证据可以证明命令没有应用：

- `local_not_dispatched`：Runtime 的 dispatch decision 绑定 episode、sequence、generation、首效窗口和因果 tick，并证明没有调用 backend。随后取得新观察时，必须另发一条有自己 sequence 的正式 observation-only／neutral exchange；不能伪造一个属于未发命令的 receipt。
- `remote_rejected_before_sampling`：远端在任何输入绑定／采样前返回绑定原命令身份的 full-request `rejected` 或精确 typed `not_applied`。账本必须显示该 request 的 `input_applications=0`、timing event 为拒绝，且没有 lease／default owner 误归。

仅有本地超时、EOF、“没有看到回执”或一次性 `operation_rejected` 都不够；`operation_rejected` 不会自动撤销 movement／look 租约。

通道仍能完成正式交换时，客户端继续执行已经安装的父动作安全默认，并在默认结束后返回新观察。Runtime 保持可用；driver 根据新观察完成、失败或重规划；source 按正式身体责任流程释放。

必须冻结 `default_exhausted → 新正式观察或 typed 终态` 的最大 movement tick 和墙钟上限。超过上限不能继续等待。

D123-2 先做协议可行性 RED，优先复用客户端已有的 full-request `rejected`。若现有协议无法在输入绑定前返回完整身份和新观察，允许最小新增 typed `not_applied` 及必要的 Java／wire 变更。不得把 `operation_rejected` 提升为整条请求未应用。所有路径都不得绕过 Runtime；需要新观察时必须经过 backend。`local_not_dispatched` 由 Runtime 本地 decision 证明，未发命令不要求也不得伪造 backend receipt；后续 neutral observation 使用独立 sequence。需要第二控制通道、通用重连或新 Session 状态时停止。真正的 send／receive 异常或连接 seal 只能归入交付未知或通道失联。

发送后交付状态未知时，即使通道稍后恢复，也先按 `BODY_RESPONSIBILITY_UNRESOLVED` 处理，不能把它改写成拒绝。D123 的 Fabric smoke 和 36 场矩阵只使用“已证明未应用且通道仍活”的场景。

### 3.2 发送后交付未知

命令已经交给传输层，但远端没有返回绑定身份的应用或拒绝事实。此时 Python 不知道命令是否进入物理采样。Runtime 进入 `failed`；driver 进入新增的 driver 级 `UNRESOLVED` 有界终态，不新增 Session 状态。只清理 driver 的 `_prepared_deadline` 和 `_prepared_proposal`，防止再次提交。不得走普通 discard／cancel 把远端命令标成已撤销或 expired。

先封存 `InputApplicationLedger` 的 `IN_FLIGHT`、`ExecutionSupervisor` 的 controller／route、proof inbox 的 `EXECUTING`／`SAFE_TAIL` lease、source 和 owner 记录，得到不可回写的 `safety_responsibility_unresolved=true`。driver 从下一帧开始不再 propose、tick 或等待恢复，只向父层返回一次 typed `BODY_RESPONSIBILITY_UNRESOLVED`。

### 3.3 整个通道失联

Python 只能根据已确认的 `fallback_armed` receipt 证明客户端在失联前安装了安全默认，并根据冻结的客户端合同说明它应当有界执行。没有失联后的观察时，不能声称默认实际执行、身体着地或健康未下降。

Runtime 同样进入 `failed`，driver 进入 `UNRESOLVED`，只清 `_prepared_deadline`／`_prepared_proposal` 并禁止重发。不得调用普通 discard／cancel，也不得释放 ledger `IN_FLIGHT`、supervisor controller／route 或 proof inbox 的 `EXECUTING`／`SAFE_TAIL` lease。先封存 `safety_responsibility_unresolved=true` 和 owner 记录，再执行下述资源关闭。下一帧禁止继续导航或控制推进。不能等待连接恢复形成活循环，也不能回写这次报告。

### 3.4 资源关闭顺序

交付未知和通道失联使用同一关闭顺序：

1. 封存 unresolved 安全结论、ledger、lease、source 和 owner 记录。封存后不可回写。
2. 关闭 Runtime 和 backend。
3. 调用明确的 driver／Session resource-only close，有界停止 worker、线程、句柄和资源容器。优先组合既有 inbox／worker close；现有入口不足时，只允许新增一个很薄的 driver close 入口，不新增 Session 状态。
4. 输出 `local_resources_closed=true`。资源关闭不得把 ledger、owner 或远端命令标成 `released`、`expired` 或 `cancelled`，也不得改变 `BODY_RESPONSIBILITY_UNRESOLVED`。

`safety_responsibility_unresolved` 和 `local_resources_closed` 是两个独立结果。不得再用一个 `released` 布尔同时表达安全责任和本地资源是否已经关闭。

发送后交付未知和整个通道失联只在组件和客户端协议层验证。它们不计入运动完成率，也不以 source 已释放作为通过条件。D123 不建设断线重连系统。

## 4. Fabric 前置改为一条命令、两个阶段

第一阶段是离线 preflight。它必须在启动 Minecraft 前完成：

- 项目 JDK、固定 Gradle 8.8 和真实缓存；
- 不依赖 Junction 或其他 reparse point；
- 部署所需的惰性导入全部可解析；
- D122-3 的 27 个生产语义文件逐项匹配；
- 四方向依赖分类、预期依赖集合和离线 mutation；已证明无关并移除的格不再要求可见；
- 当前 validator 的反例和 mutation 全部被拒绝；
- 正式 Runtime 链的及时、`L+2` 和 `remote_rejected_before_sampling` 三态；校准阶段允许第三项保持 RED，但不能进入产品 smoke；
- standalone 导入、导出清单和哈希。

离线阶段的环境、语义、proof 或导出检查失败时不能启动 Minecraft。D123-1 提供明确的 `calibration-only` 模式：这些离线项通过后，即使 transport 可行性仍为 RED，也可以启动 Minecraft，但只运行 fixture、传送和 actor observation pre-trial；该模式在代码入口上禁止提交任何运动 trial。观察获取必须有冻结的 tick 和墙钟上限。pre-trial 失败时可以关闭 Minecraft，但不能启动运动 trial。观察方案只能使用 actor 事实，不能读取 fixture 真值。

D123-2 关闭 transport RED 后，同一命令才允许进入产品 smoke／matrix 模式。这样观察校准不会被尚未完成的协议工作自锁，也不会提前运行运动场景。

修正后必须从头重跑整条命令，不能从失败位置继续拼接结论。

## 5. Fabric 运行规则

先只运行南向三场：及时后继、`L+2` 窗口前拒绝整条请求、`remote_rejected_before_sampling`。第三场必须仍处于及时窗口，在独立注入点、输入绑定前拒绝整条 request，并使用不同于 `window_missed` 的 typed reason；不能把第二场换个名字重复一次。本地 `local_not_dispatched` 只在正式组件链验证，不进入 Fabric 矩阵。三场必须同时满足：

- 首效 movement tick 与冻结窗口一致；
- 过期或丢失的后继没有物理输入样本；
- 安全默认完整覆盖证明时长；
- 默认后的新观察证明着地、零伤害和身体状态；
- driver 有界结束，source 正式释放；
- 没有 stale observation 或第二个 movement winner。

只有 smoke 3／3 通过，才另行运行四方向 × 三状态 × 三次的 36 场矩阵。smoke 与矩阵是两次正式运行，矩阵只运行一次。运行中不修改代码、场景、门槛或结果解释；失败就保存原始证据并停止。

offline 和 pre-trial 两阶段都通过，才计为 Fabric attempt。环境或夹具在第一个运动 trial 前失败不消耗矩阵 attempt，但 D123-3 仍立即停止，不能现场修正后重跑。

## 6. 批次和边界

D123 最多四个批次：

1. D123-0：只读依赖必要性实验；
2. D123-1：实施实验选出的依赖或观察修复，并建立一条命令的前置门禁；
3. D123-2：先验证协议可行性，再区分已证明未应用、交付未知和通道失联，并走正式 Runtime 链；
4. D123-3：先做 Fabric 三场 smoke，通过后只运行一次 36 场矩阵。

先前讨论中的“Fabric D123-4”并入 D123-3，不另开第五个批次。

本阶段不增加新动作，不改连续跳跃控制策略，不重构 Session，不建设通用观察规划器，不做路线优化，不改变伤害额度、支撑阈值或物理规则。出现这些需求时停止并登记后续阶段。

每个批次结束都要经过独立 review 和 sign off。每批最多两轮限定修正。观察路线若需要新增 Java 观察能力、viewpoint 规划或通用校准，也停止并另立阶段，不能为了凑满四批扩大范围。

## 7. 与现行连续运动方向的关系

D123 不改变 D099—D101 的方向。普通安全移动仍采用短开环输入、廉价逐 tick 监视、事件触发重算和关键节点严格检查。本阶段处理的是严格动作开始前必须知道哪些世界事实，以及 transport 失联后谁还能证明身体安全。

D122 的历史结果保持不变：D122-3 已通过，D122-4 仍为 `PARTIAL／STOPPED`，Fabric v8 仍是 0／3。D123 的新证据只能追加，不能把旧失败改写成通过。

## 8. D123-0 停止记录

2026-10-20，Windows 只读探针已冻结四方向的父 proof、固定后继、fallback 和 67 个联合依赖。每方向 12 格中，3 格在父恢复证明或后继中仍为必要；9 格的 AIR／完整石块／UNKNOWN 在当前支撑下行为相同，但材料接触读取仍存在现行最大形状高度允许的反例，不能从轨迹等价推出整格无关。没有签署任何短路，也没有改变生产语义。

D123-0 最终结论为 PARTIAL／STOPPED，独立审查 PASS／已签署。当前 `BlockGeometry` 正式契约下，尚不能证明删除 12 个下层依赖格安全；签署的是只读实验及停止结论，没有批准删除。正式 gate 重算 396 行：PASS 52、FACT_MISMATCH 160、NEEDS_WORLD 100、NEEDS_WORLD_AND_FACT_MISMATCH 84。D120 四份原始 coverage 的历史哈希核验通过，972 个入口和 2916 个后缀重算完全一致，旧成功退步 0；最终定向检查 40／40。实验、修正和历史失败见验收第 8／9 节及 `evidence/motion_navigation/d123-proof-and-transport-v1/d123-0/`。

D123-1 没有自动进入；现有证据支持后续保留全部 12 个依赖并验证 actor-only 观察／校准路线。该校准尚未运行，D123-1—D123-3 仍未运行，也没有 D123 Fabric 通过结论。D122 和本批历史失败均保持原结论。
