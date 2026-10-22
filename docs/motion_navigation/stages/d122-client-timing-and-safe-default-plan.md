# D122 实施计划：首效窗口与预排安全默认输入

日期：2026-10-20。状态：**D122-0—D122-3 已完成；D122-4 为 PARTIAL／STOPPED**。第二轮独立代码审查和最终 Windows／standalone／Java 门禁已经通过。正式平台：Windows。

依据：[决定](../decisions/0122-enforce-first-effect-window-and-prearm-safe-default.md)、[架构规格](../architecture/client-first-effect-window-and-safe-default-v2.md)。D121 发布修复的四轮门禁和独立审查已通过；D121 本身保持 `PARTIAL／STOPPED`。

## 1. 交付范围

用现有 Runtime、worker 和客户端输入出口，在 1-1-4 组件课程中做到：后继按时则跨过去，后继过期／失联／被拒绝则潜行停住。最后一条 incumbent 请求须在客户端采样 L 前安装默认，L 响应确认 `fallback_armed`，绝对激活点为 `L+1`，后继也只允许 `L+1` 首效。父请求自身迟到或丢失仍由现有 input-loss 责任处理，不在本阶段保证范围内。

不改 Session 状态、planner、路线接纳、四格正式 profile、物理计算器、安全支撑阈值、伤害额度或证明算法。不把有限入口升级成连续包络，不扩大成产品连续跑酷。Fabric 只验证这项传输合同。

默认按项目约定由一个实现子 agent 逐步实施，主 agent 负责规格和独立复核。先读本步依赖，再改本步代码；每步单独提交和保留 RED。相关小检查先暴露问题，稳定后集中跑完整回归。停止必须对应下列已登记条件，不能因为表示方式写大了就另造阶段。

## 2. 预计文件

| 范围 | 预计文件 |
|---|---|
| Python contract／回执 | `mc2p/contracts/action_v1.py`、`mc2p/contracts/action_receipt.py`、`mc2p/runtime/backend_v1.py` |
| Runtime 投影 | `mc2p/runtime/arbiter_v1.py`、`mc2p/runtime/player_runtime_v1.py` |
| backend 与握手 | `mc2p/backends/client_behavior_payload.py`、`fabric_behavior.py`、必要的部署会话 decoder 和 `craftground_runtime.py` 补丁清单／指纹 |
| Java 输入采样 | `runtime_overlays/mc121_actions/ClientActionRequest.java`、`ClientRequestGate.java`、`ClientBehaviorInput.java`、`ClientBehaviorExecutor.java`、必要的 receipt／session envelope 文件 |
| 组件与探针 | `tests/sim/backend.py`、`scripts/d121_transport_late_probe.py` 的可复用场景、一个新 D122 探针；旧 D121 原始输出不改 |
| 检查 | 现有 action／receipt／Runtime／Fabric 测试、`tests/java/ClientActionDecodeTest.java` 及 request gate／input 测试、新 D122 时序矩阵 |
| 发布 | standalone manifest、导出必需文件检查、D122 验收和新的精简证据 |

实际文件名以现行调用点为准。遇到预计范围外的生产模块，先说明它如何参与同一合同；若需改 Session／planner／第二条输入链，按停止条件处理。

## 3. D122-0：只读最小时序 probe、合同和 RED

**交付：** 先在现行正式循环做只读最小时序 probe，确认调用点成立，再冻结 Python／Java 共用的转换表和可运行场景。初始检查要在现行代码上真实失败，不能断言“新类型尚不存在”。

- [ ] 只读核实部署请求、`receiptReady`、客户端自主 movement 推进与观察释放的先后顺序；分别记录 future pending、late／missed 默认收尾和 never 的可观察边界。不能靠组件独有的同步观察完成这一步。
- [ ] 冻结计时边界：默认至少 8 tick 期间持有 missed 响应的合法等待，单列 tick／墙钟时长；Python 同步提交与控制准备只测各自同步计算区间，等待不混入 <50 ms 或 8／15／30 ms。端到端响应和有界等待仍完整报告。
- [ ] 核实 `prepareForPlayerTick` 在原版读取 sneak 前执行，`Input.tick` 只消费一次；记录同拍默认／后继切换和清除旧 sneak 所需的最小调用点，不改生产代码。
- [ ] 用现行正式 Task 5 重现：过期后继在 `L+2` 应用；never 场没有预排默认而离开平台。保存 RED，不改 D121 记录。
- [ ] 冻结 v2 两个字段、协商确认和含 `fallback_armed` 的六类回执；新 Java 单测从严格 decoder／request gate／Input 采样入口推进，不只扫描源码。
- [ ] 建立转换表用例：早到、窗口内、晚到、never、重复 sequence、pending 替换、取消 pending、reset、episode 切换、许可撤销、默认耗尽。
- [ ] 在非零起始 movement tick 和至少两个不同默认 duration 下重复；证明没有硬编码 L、27 或 8。
- [ ] 冻结父请求前提：第一跳最后一条 incumbent 在采样 L 前安装默认，正常 lease 覆盖到 L，L 响应确认 `fallback_armed`，父 wall deadline 覆盖默认结束。父请求自身迟到／丢失不列入 D122 安全负例，不借此扩展续租或 heartbeat。

**通过：** RED 对应现有真实缺口；字段和事件定义无歧义。Java 可独立推进采样时序，不依赖 Minecraft 启动。

**停止：** 正式循环不能在 pending 响应未完成时自主推进、不能在默认结束前有界持有 missed 响应、不能在原版读取 sneak 前选择本拍输入，或必须引入通用排程才能运行代表场景。此时报告具体调用点，不扩大设计。

## 4. D122-1：Python snapshot、Runtime 和握手

**依赖：** D122-0 的字段、转换表和兼容规则已复核。

- [ ] action intent／snapshot 保留精确首效窗口和一个 typed 默认；arbiter 只从同一 movement winner 携带它们，look／operation 不挟带默认。
- [ ] wire 只在握手确认 v2 后编码新字段。旧客户端普通行为仍使用 v1 原字节语义，需要新字段的请求在发送前拒绝。
- [ ] Runtime／账本支持六类回执，armed 和排队都不算应用，默认应用归属父请求；只沿用现有 owner 和收尾流程。
- [ ] CalculatorBackend 实现转换表：物理前选当前／pending／默认，之后才 physics step。注入时序不靠诊断名称选择。
- [ ] 组件同样持有 pending 响应：future 到窗口才返回；late／missed 到默认有界结束才返回；never 通过客户端 trace 或延后释放最终观察验证，不生成假的逐 tick 同步观察。
- [ ] mutation：丢掉窗口投影、提前替换当前、窗口拒绝后清默认、普通更新后复活旧默认、把默认计时放到父到达时、missed 后过早释放响应，全部必须被检查发现。

**通过：** Python contract、Runtime 与组件转换表全绿；v1 配对结果不变；新字段不增加第二条控制或回执链。

本切片只提供合同和组件支持，不启用正式严格动作。正式 Fabric host／probe 的 v2 构造点迁移留在 D122-3；Java 实现前不能把 Python opt-in 当作端到端能力。默认 wall budget 至少覆盖名义证明时长；成功 arm 后不能因 wall 或父响应期限到期提前截断真实采样。账本必须用完整默认采样或合法接管／取消事实结束责任。

missed／未激活的 future 响应须持有到默认真实 duration 完成，wall／父期限到期不能先交还普通响应。无 movement tick 时物理不推进；外层硬终止走既有 reset／许可撤销，不作为普通控制继续。取消只结束由 v4 事件／真实采样确认的 current、pending、default 成员和取消请求，不能清掉无关的旧 ambiguous 历史。

**停止：** 需要伪造客户端 v2 能力或在 Python 事后回执中冒充物理前拒绝；默认只能在 `L` 之后补发。

## 5. D122-2：Java 采样门禁与默认

**依赖：** 与 D122-1 完全相同的字段和转换表，不允许 Java 另解释 tick。

- [x] 严格 v2 decoder／握手解码；v1 的精确键集合保留；拒绝超界 tick、反向窗口、递归默认和超额度输入。
- [x] pending 槽只容纳一条请求。早到不触发按键／look／operation，窗口内激活才原子替换默认。
- [x] 本阶段只接纳 movement＋look 的窗口请求；`operation` 非空时在首次接纳前以 `windowed_operation_unsupported` 拒绝，不能接受后延迟执行或静默丢弃。普通非窗口 operation 不变。
- [x] pending 接纳只消费一次 sequence、核对一次 observation；激活不再调用完整 `validate`，只检查登记 pending、episode／reset、许可、wall deadline 和首效窗口。Java 用例必须覆盖排队后观察生成／响应已推进、后继仍能按期激活，以及 reset／替换后不能激活。
- [x] 在 `prepareForPlayerTick`、原版读取 sneak 前选定默认或后继并检查过期；late jump 不进入 vanilla 物理。同拍切换覆盖旧 sneak 投影，`Input.tick` 只消费一次并扣一次 lease。
- [x] 在证明绑定的绝对 tick、父 lease 结束后激活默认，tick／wall 从该拍开始；父请求提前过期不能提前使用出口默认。成功 arm 后，即使 wall 先到也不能截短声明的真实默认采样数；missed／未激活的 future 响应持有到真实 duration 完成，不能提前交还普通响应。无 tick 时物理不推进；外层硬终止须走 reset／许可撤销。合法普通更新或 cancel 清整份旧安排；不实现跨普通续租的默认保留。
- [x] 完整执行取消、reset、episode、higher sequence、断连和强制许可撤销表，回执与真实 sample 身份对应。
- [x] future 请求保持 `receiptReady=false`，到窗口采样后才响应；late／missed 保持该次响应到默认有界结束后返回最终观察。
- [x] Java mutation：跳过过期检查、拒绝时清默认、错误起算默认、重复一次性操作、保留旧 sneak 投影、`Input.tick` 重复消费，全部必须使检查失败。

**通过：** Java 单测和 Python 相同转换表逐项一致；旧 Java 检查全绿；JVM 采样不做证明／摘要／I/O；action 大小不超 16 KiB。

**停止：** 必须放宽 `L+1` 到 `L+2`、增加多默认或 wire generation；新字段无法由握手隔离。

## 6. D122-3：正式 Runtime 组件三态

**交付：** 重跑 D121 Task 5 的同一正式单场路径，结果写入新的 D122 目录，不回写 D121。

- [x] 最小三场：及时 `[L+1,L+1]` 完成；transport 到 `L+2` 物理前拒绝；never 使用默认停住并完成 driver 生命周期收尾。
- [x] 扩大为四方向 × 正式链可达的按时／晚到／never／明确拒绝／取消，共 20 场；另覆盖 high-sequence pending 替换和 reset。正式 producer 到 `L` 才产生后继，early 不能在不改 proof 的前提下进入这条正式链，因此由共用客户端转换表覆盖。
- [ ] 每场完整记录父请求在采样 L 前的默认安装、L 响应 armed、首效窗口、接纳和真实采样 tick、默认激活／耗尽、owner、winner、健康与身体终态；同时记录实际响应释放 tick，never 不伪造同步观察。
- [ ] 972 个 D120 冻结入口重放默认至少 8 tick；这些数字仅作为有限回归，不作为连续入口许可。
- [ ] mutation：允许 `L+2` 补跳、将默认改中性、把 pending 当应用，全部触发不变量失败。
- [ ] Windows 性能：计时与调用计数分两次，报告生产同步控制计算 P95／P99／最大值及 L 帧；沿用 8／15／30 ms 和同步提交计算 <50 ms。missed 响应等待默认至少 8 tick 的合法持有时长单列，不能计入上述同步计算门槛；端到端响应完整报告并检查有界。所有 proof／digest 工作在准备阶段完成。
- [ ] 代码冻结后跑 Windows 源码与独立公开快照完整正序／逆序，以及 Java 完整检查；零失败、错误、超时和跳过。导出用已验证字节基线，纯 EOL 噪声为 0。

**通过：** 三态和扩大组件矩阵全部满足安全门槛，旧成功退步为 0，公开证据可复算。

**停止：** 父默认未预排、late jump 真实采样、安全违规、owner 空档或无界等待；两轮限定修正后再有清单外阻塞也停止。不得用“任务 failed 但本次没有观测伤害”冒充安全停住。

## 7. D122-4：Fabric 受控传输延迟

**依赖：** D122-3、完整 Windows／公开快照与独立代码审查均通过。正式 Fabric 前先在组件后端执行同一条命令的外层组装、reset 和汇总。

- [ ] 父请求走正常传输，确认在客户端采样 L 前已安装默认，L 响应包含 `fallback_armed`。只对后继注入 transport 延迟，不能用 worker 延迟代替；父请求自身迟到／丢失不算本阶段覆盖。
- [ ] 单方向 smoke 先跑及时、L+2、never 三场。若不符，保留失败，不扩大。
- [ ] smoke 通过后，同一正式命令跑四方向 × 三态 × 3 次，共 36 场；每场起点着地、静止与传送稳定硬检查。
- [ ] 用 Runtime receipt、physics trace 和 F-TICK 对齐全部请求。及时场首效为 L+1；late 场窗口拒绝发生在物理前；never／late 场默认连续覆盖证明时长，真实健康无下降。
- [ ] 报告新 Java 采样成本、完整生产控制时间与端到端交付。机器状态和正式环境按既有规范记录，不能根据负载改行为。
- [ ] 总结实际通过范围。即使全部通过，也只签署协议与该有限组件／Fabric 课程，不宣布产品规划或任意连续跑酷完成。

**停止：** 计算器／实机离散状态不符、首效 tick 不符、父默认预排不成立、未送达后无法安全停住，或超过两轮限定修正；不以调大窗口、时长硬编码或改历史失败方式通过。

## 8. 证据和收尾

证据放在 `evidence/motion_navigation/d122-client-timing-v1/`。保留 RED、mutation、组件结果、正式命令、字节／语义哈希、Java 结果和 Fabric 原始失败。Git 只放可复核的必要索引、哈希和精简轨迹；大型原始数据沿用现有归档位置。

D122 验收文档只追加实际数字。阶段头部、AGENTS、packaging 入口同步更新；没有运行的步骤明确写“未运行”。提交按合同、Python、Java、组件和 Fabric 结果拆开，不推开发分支到公开仓库。

## 9. 计划审查修正（2026-10-20）

撤回最后可放弃点前 armed 的要求，也撤回据此提出的父请求迟到／丢失安全负例。冻结轨迹边界 15 后已不可放弃，最后请求 sequence 25 到 tick 26=L 才应用；原要求无法在现有每 tick 一请求的正式链上满足。

保证范围改为最后父请求在采样 L 前已安装默认、L 响应确认之后的后继传输行为。另补两项实施前检查：一问一答必须能有界持有响应并自主推进；默认／后继选择必须早于原版读取 sneak，且 Input.tick 单次消费。三份文档已统一这些边界；本次仅修设计，没有代码修改或运行结论。

最后一次复核补齐 pending 的一次接纳规则和计时区间：增加“排队后观察生成／响应推进仍可按期激活”的 Java 检查；missed 响应为默认收尾而合法等待的时长单列，不混入同步提交与控制准备门槛。

## 10. D122-4 停止结果

组件后端先执行了与 Fabric 共用的 timely／`L+2`／never 外层命令，3／3 通过。最终 v8 前置为 95／95 个定向检查通过、overlay 编译通过。v1—v7 的环境、缓存、装配和导入失败全部保留。

v8 在第一场协议 trial 前的 actor 观察门禁停止：67 个 proof 依赖中，55 个已知，12 个仍为 `UNKNOWN`；真实客户端查询把这 12 个位置分为 10 个 `occluded` 和 2 个 `outside_view`。因此真实协议样本为 0／3，四方向 × 三态 × 3 次的 36 场矩阵没有运行。该结果命中“正式观察与组件前提不一致”的停止条件，不能用夹具已放置 AIR 代替 actor 证据。

本计划到此冻结为 `PARTIAL／STOPPED`。若继续，只能二选一：先在夹具／观察准备层让 12 个格子真实可见并稳定；或另立决定，证明这些下层格不属于必要依赖后再缩小查询。第二条会改变 proof 语义，不属于本阶段。两条路线都禁止把 `UNKNOWN` 当作 `AIR`。

## 11. 收尾复核新增的 never 门槛

夹具复核证明，Fabric 风格的 transport `TimeoutError` 会在 Runtime 内转成 typed failure。外层不会捕获异常，也不能继续调用已经失败的 Runtime。此时旧 observation 早于客户端的默认输入，不能证明默认结束后的身体和健康；driver 仍在 `stopping`，source 也没有正式释放。

所以 D122-4 的共用 validator 现在要求：typed Runtime failure 与场景严格匹配；身体终态取精确父请求的 physics sidecar 最后一个 `post_state`；健康必须来自后续正式只读／中性观察或既有正式诊断；driver 必须有界终结并释放 source。证据缺失就失败，不用旧 observation 补空。

下一次继续前需另立语义决定，区分“单请求丢失但通道仍活”和“通道失联”。前者需要正式 typed backend rejection、新观察和责任收尾；后者只能执行默认后报告身体责任未解决或对应 typed 终态。本阶段不实现这两条新路径，不再运行 Fabric。

## D126 纠偏

运动夹具现在从服务器实际使用的同一组有序 fixture rows 生成已知地图，并写入 Runtime 自己的 owner。原 D122-4 可见性门槛保留为历史失败，不再作为运动 trial 前置。D122-4 的 transport 问题仍保持打开，转由 D123-2 处理。
