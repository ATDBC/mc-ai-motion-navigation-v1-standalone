# D123 实施计划：证明依赖与 transport loss 收敛

2026-10-20，D123-1 已明确授权并执行到固定视点只读验证，现为 **PARTIAL／STOPPED，独立审查 PASS／已签署**。全部 67 个依赖（含 12 个下层格）保留。Windows 原生可见性核心完成四方向共 1248 个相机朝向样本，每方向 12 格均未出现可见空气；其中 9 格另有不依赖相机朝向的完整立方体遮挡论证。仅转头无法补齐所有依赖，继续需要改变观察位置或新增 sensing，触发预登记停止条件。未启动新的 Minecraft／Fabric，未提交 movement trial；actor-only 实机校准和两阶段启动命令未交付。D123-0 继续 PARTIAL／STOPPED，删除依赖未授权；本次签署仅批准 D123-1 PARTIAL／STOPPED 的停止收尾，不授权 D123-2；D123-2／D123-3 未开始。见 D123 验收第 10 节和 `evidence/motion_navigation/d123-proof-and-transport-v1/d123-1/fixed-eye-run-2/stop.json`。


日期：2026-10-20。状态：**D123-0 PARTIAL／STOPPED，已签署；D123-1 PARTIAL／STOPPED，独立审查 PASS／已签署**。正式平台：Windows。

依据：[D123 决定](../decisions/0123-resolve-proof-dependencies-and-transport-loss.md)、[D122 验收](../acceptance/d122-client-timing-and-safe-default.md)、[D123 验收](../acceptance/d123-proof-dependency-and-transport-loss.md)。

## 1. 目标

先查清 D122 Fabric 观察门禁中的 12 个 `UNKNOWN` 哪些读取真的影响证明，再只实施实验支持的混合方案。随后把“命令已证明未应用”“发送后交付未知”和“整个通道失联”分开，最后用一条两阶段前置命令守住两次正式 Fabric 运行。

本计划共四个批次。实施默认由子 agent 执行，每个批次完成后由独立 reviewer 检查。前一批没有通过，不进入后一批。

## 2. 全局约束

- Windows 是正式开发和验收平台；不得用 Linux 结果替代正式结论。
- D122 v1—v8 原始失败和证据保持原字节；D123 只追加新目录。
- 仍参与安全结论的 `UNKNOWN` 不能当作 `AIR`。在严格碰撞屏障和扫掠几何前提下已证明无关、并已从依赖中移除的格，不再要求可见；前提失效后必须恢复依赖和 `NEEDS_WORLD`。
- 所有决定和结果都不得绕过 Runtime。需要新观察时必须经过 backend；`local_not_dispatched` 由 Runtime 本地 decision 证明，未发命令不得伪造 backend receipt，后续 neutral observation 使用独立 sequence。
- 不增加新动作、Session 状态、worker 管线或通用观察规划器。
- 不修改连续跳跃策略、planner、路线优化、伤害额度、支撑阈值和物理规则。
- 每个批次先跑代表 RED，再做最小修改，再跑受影响检查，并由独立 reviewer sign off 后进入下一批。批次范围外的新问题登记后停止，不能现场扩张。
- 每批最多两轮限定修正。第二轮后仍有清单外阻塞就保存证据并停止。观察路线若需要新增 Java 观察能力、viewpoint 规划或通用校准，停止并另立阶段，不能硬塞进四批。

## 3. 预计文件边界

D123-0 只能新增只读 probe、测试和证据。D123-1 的生产文件由实验结果决定，但必须局限在现有 proof 查询／依赖记录或现有观察准备入口。D123-2 优先修改现有 backend、Runtime 结果映射和 driver 收尾入口，并允许新增一个 driver 级 `UNRESOLVED` 终态；协议可行性 RED 若证明现有 wire 无法表达 full-request 拒绝和新观察，允许最小 Java／wire 变更。若既有 inbox／worker close 不能组合出有界资源关闭，只允许增加一个薄的 driver／Session resource-only close 入口，不增加 Session 状态。D123-3 只改夹具、验收和证据；若 Fabric 暴露生产缺陷，停止而不是现场修复。

不预建新 architecture 文档。只有 D123-1 或 D123-2 改变公共接口或长期职责时，才更新现有对应架构文档。

## 4. D123-0：只读依赖必要性实验

**交付：** 一张按“格子＋读取原因”分类的解释表，以及每类读取选择“严格短路”或“保留并观察”的结论。允许混合方案。

- [ ] 冻结 D122 parent、successor、fallback proof 及其 67 个依赖，记录 12 个下层格的坐标、读取调用点和读取原因。
- [ ] 对每个下层格分别运行 `AIR`、无伤害完整方块、`UNKNOWN`；上方支撑保持已知且不变。
- [ ] 比较主轨迹、出口状态、风险区间、伤害、fallback、接纳结果和依赖集合。
- [ ] mutation 去掉上方支撑、改变其碰撞形状、让身体可能进入下层空间；此时被短路的依赖必须重新出现或正式返回 `NEEDS_WORLD`。
- [ ] 组合／property mutation 覆盖相邻两格同时为 `UNKNOWN`、同时变为实体，以及“支撑形状变化＋扫掠边界变化”同时发生。短路依据碰撞屏障与扫掠几何，不能按方块名称写白名单。
- [ ] 四方向和 D120 的 972 个冻结入口至少做结果签名比较，不能只测朝南代表例。
- [ ] 输出每个“格子＋读取原因”的结论：必要、在明确屏障前提下可短路，或证据不足。不得用“fixture 已设为空气”作为结论。

**通过：** 每个读取都能解释；混合方案中的每一类都有独立证据；单格和组合 mutation 都能推翻错误短路。仍参与结论的事实变成 `UNKNOWN` 时必须拒绝；已证明无关并移除的格在 `AIR`、完整方块和 `UNKNOWN` 下结果相同；屏障或扫掠前提失效后必须恢复 `NEEDS_WORLD`。

**停止：** 一个批次内仍不能判断必要性，或者安全结论依赖未建模材料、实体、流体或未知物理。停止时不改生产代码、不运行 Fabric。

2026-10-20 只读结果：四方向各 67／12 依赖身份已冻结；144 个单格对照、248 个其它完整证明变异及 144 个扫掠变异已完成。每方向 3 个无屏障格在父恢复证明或后继中仍需要观察；9 个有屏障格的三态行为相同，但最大形状高度处的材料接触检查仍能改变结论。当前 `BlockGeometry` 正式契约下，尚不能证明删除 12 个下层依赖格安全，因此按本节停止条件保存为 PARTIAL／STOPPED；独立审查 PASS／已签署，没有批准任何删除。正式 gate 重算 396 行：PASS 52、FACT_MISMATCH 160、NEEDS_WORLD 100、NEEDS_WORLD_AND_FACT_MISMATCH 84。D120 的 972 个入口、2916 个后缀与原始 coverage 精确一致，旧成功退步 0；最终定向检查 40／40。

D123-1 没有自动进入；现有证据支持后续保留全部 12 个依赖并验证 actor-only 观察／校准路线。该校准和 D123-1—D123-3 尚未运行，不能声称 actor 校准或 Fabric 已通过。详细分类、门禁修正和证据边界见验收第 8／9 节，历史失败不改写。各任务框继续表示原通过门槛，不能把“已经做过实验”勾成“短路已证明”。

## 5. D123-1：实施依赖结论，并建立一条命令的前置门禁

**依赖：** D123-0 已完成读取分类，并为每一类选定短路或观察方案。

对 D123-0 判定可短路的读取：

- [ ] 在正式查询或依赖记录入口加入最小短路，只覆盖“已知完整支撑把身体与下层空间隔开”的已证前提。
- [ ] 上方支撑缺失、碰撞形状变化或身体扫掠可能进入下层时，保持原保守结果；此时相关事实为 `UNKNOWN` 必须返回 `NEEDS_WORLD`。
- [ ] 用 D123-0 mutation 和 D120 972 个入口证明没有扩大安全范围。

对 D123-0 判定仍必要的读取：

- [ ] 只使用正式 actor 视点和现有观察请求取得必要事实；不得直接把 fixture 世界写入 WorldModel。
- [ ] 先做 actor-only 校准，冻结取得事实的最大 movement tick 和墙钟上限。观察必须在该上限内结束，并在动作开始前得到所有剩余依赖 known、0 unknown、0 mismatch。
- [ ] 遮挡无法通过现有观察解决，或需要新增 Java 观察能力、viewpoint 规划／通用校准时停止；本阶段不建设这些能力。

混合方案共用的前置命令分两阶段：

- [ ] offline preflight 检查 JDK、Gradle 8.8、缓存、reparse point、惰性导入、27 个生产语义哈希、依赖分类与 mutation、standalone 导入与导出哈希。组件区先覆盖 timely 和 `L+2`，并把尚未修正的 transport RED 记为产品运行阻塞。
- [ ] offline preflight 每一项输出 typed 状态和证据路径。环境、语义、proof 或导出项失败时命令非零退出，且不得启动 Minecraft。
- [ ] 同一命令提供 `calibration-only` 模式。offline 的环境、语义、proof 和导出项通过后，可以启动 Minecraft，但入口只能运行 fixture、传送和 actor observation pre-trial；必须从代码路径上禁止提交运动 trial。transport RED 仍阻止 smoke／matrix。
- [ ] 启动后 pre-trial gate 只用正式 actor 事实检查 fixture、传送完成、健康、起点着地静止和所有剩余必要依赖。任一失败时不得启动运动 trial。
- [ ] mutation 分别破坏一个语义哈希、一个 proof 事实、一个起点条件和一个 standalone 文件，命令必须拒绝。

**通过：** 混合方案让四方向依赖门禁通过；一条命令的 offline 阶段能在启动 Minecraft 前发现环境和语义错误，`calibration-only` 只能运行 pre-trial actor 校准，不能提交运动 trial。D123-2 尚未完成时，transport RED 继续拒绝产品 smoke／matrix。

**停止：** 需要把 `UNKNOWN` 当空气、放宽 proof、安全门槛或引入通用观察规划器。

## 6. D123-2：先证明协议可行，再区分三种传输事实

**交付：** 一份协议可行性 RED，以及三条互不混淆的正式 Runtime 生命周期。

### 协议可行性 RED

- [ ] 冻结四个注入点：Runtime 决定 `local_not_dispatched`、客户端 `remote_rejected_before_sampling`、发送后交付未知、整个通道失联。
- [ ] 优先验证现有同一会话能否返回绑定身份的 full-request `rejected`，并在安全默认结束后返回新正式观察。一次性 `operation_rejected` 仍可能保留 movement／look 租约，不能作为整条 request 未应用的证据。
- [ ] 未应用结果必须绑定 episode、sequence、generation、首效窗口和因果 tick。缺任一身份都不能证明命令未应用。
- [ ] 真正的 send／receive 异常或连接 seal 一律属于交付未知或通道失联，不能归为“命令未应用且通道仍活”。
- [ ] 若现有协议无法表达上述结果，允许一次最小 typed `not_applied` Java／wire 变更；仍需第二控制通道、通用重连或新 Session 状态时停止。
- [ ] 若修改 Java／wire，action 仍 ≤ 16 KiB、receipt 集合仍 ≤ 64 项；客户端 movement tick 不得做证明、摘要或阻塞 I/O；v1 原字节兼容、D122-3 正式 20／20 不退步；客户端解析、排程和采样性能不得超过既有门槛。

### 命令已证明未应用，通道仍活

- [ ] `local_not_dispatched` 由 Runtime dispatch decision 证明没有调用 backend；decision 绑定 episode、sequence、generation、窗口、注入原因和因果 tick，不为未发命令制造 receipt。
- [ ] 本地未发出后，另用自己 sequence 的正式 observation-only／neutral exchange 取得新观察；该 exchange 的 receipt 不得绑定、引用或冒充未发命令的 receipt。
- [ ] `remote_rejected_before_sampling` 使用原命令身份的 full-request `rejected` 或 typed `not_applied`，不能用超时或 `operation_rejected` 推断。
- [ ] 远端拒绝对应 request 的 `input_applications=0`，timing event 为拒绝，且没有 lease／default owner 误归。
- [ ] 客户端完整执行父安全默认；新观察必须晚于 `default_exhausted`，并带正式健康和身体状态。
- [ ] driver 根据新观察有界结束或重规划，prepared frame 清空，source 由正式 owner 释放。
- [ ] 冻结 `default_exhausted → 新正式观察或 typed 终态` 的最大 movement tick 和墙钟上限，超限即失败。

### 发送后交付未知

- [ ] 没有远端明确应用或拒绝事实时，Runtime 进入 `failed`；driver 进入 driver 级 `UNRESOLVED` 终态并只返回一次 typed `BODY_RESPONSIBILITY_UNRESOLVED`。
- [ ] 只清 driver `_prepared_deadline`／`_prepared_proposal` 并禁止重发；不得走普通 discard／cancel 把远端命令标成撤销或 expired。
- [ ] 封存 ledger `IN_FLIGHT`、supervisor controller／route、proof inbox `EXECUTING`／`SAFE_TAIL` lease、source 和 owner 记录，输出不可回写的 `safety_responsibility_unresolved=true`。
- [ ] 下一帧 driver 不再 propose、tick 或等待恢复。不得声称命令未应用或身体安全。

### 整个通道失联

- [ ] RED：通道关闭后没有新观察，旧观察不能满足健康门禁。
- [ ] 只用失联前的 `fallback_armed` receipt 证明默认已经安装，并按客户端合同应有界执行；没有新观察时不能声称默认实际执行、身体着地或健康未下降。
- [ ] Runtime 进入 `failed`；driver 进入 `UNRESOLVED`，只清 `_prepared_deadline`／`_prepared_proposal` 并立即报告一次 typed `BODY_RESPONSIBILITY_UNRESOLVED`。
- [ ] 不调用普通 discard／cancel；封存 ledger `IN_FLIGHT`、supervisor controller／route、proof inbox `EXECUTING`／`SAFE_TAIL` lease、source 和 owner 记录，输出不可回写的 `safety_responsibility_unresolved=true`。下一帧禁止继续导航／控制推进，不等待恢复。

### unresolved 后的资源关闭

- [ ] 严格按顺序执行：封存 unresolved 结论和 owner 记录 → Runtime／backend close → driver／Session resource-only close。
- [ ] resource-only close 有界停止 worker、线程、句柄和资源容器。优先组合既有 inbox／worker close；不足时只加薄 driver close 入口。
- [ ] 关闭后单独输出 `local_resources_closed=true`。不得把 ledger、owner 或远端命令标成 `released`、`expired`、`cancelled`，不得改变 `BODY_RESPONSIBILITY_UNRESOLVED`。
- [ ] 禁止用一个 `released` 布尔同时表达安全责任和本地资源关闭。

### 共用门禁

- [ ] 三类传输事实都不绕过 `PlayerRuntimeV1 → RuntimeNavigationDriver`；需要观察时经 backend。`local_not_dispatched` 停在 Runtime dispatch decision，未发命令没有 backend receipt，后续 neutral observation 使用独立 sequence。
- [ ] mutation 把 `operation_rejected` 冒充 `not_applied`、把明确拒绝改成 Runtime failure、把交付未知伪装成拒绝、复用旧观察、调用普通 discard／cancel、提前释放任一 IN_FLIGHT／EXECUTING／SAFE_TAIL lease、用 released 混淆两种结果、资源 close 回写安全结论或伪造 source release，检查必须失败。
- [ ] 交付未知和整个通道失联不进入产品完成率；`local_not_dispatched` 只留在正式组件层；`remote_rejected_before_sampling` 进入 D123-3 Fabric 三态。
- [ ] 把最终正式三态接入 D123-1 的同一条 offline preflight；到此 `timely`／`late_window_rejected`／`remote_rejected_before_sampling` 必须 3／3，命令才允许退出 calibration-only 并启动产品 smoke。

**通过：** 三条生命周期的终态、观察新鲜度和 source 责任互不混淆；协议能返回绑定身份的未应用事实；无界等待为 0。

**停止：** 需要断线重连系统、新 Session 状态或第二条输入通道。

## 7. D123-3：Fabric smoke 和一次正式矩阵

**前置：** D123-1／D123-2 的 offline preflight 全绿；最终生产语义哈希已经封存。启动 Minecraft 后，pre-trial gate 也必须全绿。只有两阶段都通过，才计为一次 Fabric attempt。进入运动 trial 后不再改代码。

### 第一次正式运行：南向 smoke

- [ ] 只运行 `timely`、`late_window_rejected`、`remote_rejected_before_sampling` 三场，各一次；`local_not_dispatched` 只留在组件层。
- [ ] `remote_rejected_before_sampling` 使用本应在 `L+1` 及时生效的 request，在独立注入点、输入绑定前 full-request 拒绝；reason 与 `late_window_rejected` 的 `window_missed` 不同。
- [ ] 记录 v2 握手、父 default 在 `L` 前安装、`L` armed 回执、后继精确窗口、Runtime receipt、F-TICK、movement winner、owner、health、身体状态和 source 释放。
- [ ] timely 只在 `L+1` 出现后继输入；`late_window_rejected` 和 `remote_rejected_before_sampling` 的后继输入样本为 0。
- [ ] 两个负例完整执行已证明默认，并由默认后的新观察确认着地和零伤害。

任何一场失败就保存原始证据并停止，不运行矩阵。

### 第二次正式运行：正式矩阵

- [ ] smoke 3／3 后，另行运行四方向 × 三状态 × 三次，共 36 场，只运行一次。smoke 和矩阵是两次正式运行，不合并计数。
- [ ] 36 场全部使用 smoke 的同一 trial 函数、validator 和生产字节。
- [ ] 矩阵期间禁止现场修正、改门槛、补运行或删失败样本。
- [ ] 控制准备继续满足 P95 ≤ 8 ms、P99 ≤ 15 ms、最大值 < 30 ms；合法等待单列，不混入同步计算门槛。

**通过：** 36／36 满足时序、安全、健康、owner、source 和有界终态门槛；零旧成功退步。

**停止：** smoke 非 3／3，或矩阵任一场出现窗口外输入、伤害、旧观察、owner 空档、source 泄漏、无界等待或生产字节不一致。

环境或夹具在首个运动 trial 前失败时，该运行不消耗 36 场矩阵 attempt，但 D123-3 仍立即停止。修正必须另立后续决定；本阶段不得现场修正后重跑。

## 8. 最终门禁与收尾

- [ ] Windows 源码树正序、逆序完整分片全绿；失败、错误、超时、跳过、意外成功和清理失败均为 0。
- [ ] 独立 standalone 正序、逆序全绿，导出文件与哈希通过。
- [ ] Java／Python 相邻动态门禁和 Fabric overlay 编译通过。
- [ ] 当前 D123 生产语义哈希逐项匹配；D122 历史语义和失败证据不改写。
- [ ] 具体旧成功集合零退步：D120 972 个冻结入口、D122-3 正式 20 场、D122 v1 兼容 120 项，以及 D123 实际修改模块对应的冻结集合。
- [ ] 独立 reviewer 检查四个批次边界、正式 Runtime 调用链和 Fabric 原始证据。

D123 通过后，才回到 1-1-4 连续跨隙产品能力。D123 自己不宣布连续跑酷已经交付。
