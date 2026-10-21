# D123 验收：证明依赖与 transport loss 收敛

日期：2026-10-20。状态：**D123-0 PARTIAL／STOPPED；独立审查 PASS／已签署**。正式平台：Windows。

依据：[D123 决定](../decisions/0123-resolve-proof-dependencies-and-transport-loss.md)、[实施计划](../stages/d123-proof-dependency-and-transport-loss-plan.md)。前序事实来源为 [D122 验收](d122-client-timing-and-safe-default.md)和 `evidence/motion_navigation/d122-client-timing-v1/d122-4/`。

## 1. 当前 RED

D123 开始前冻结以下事实：

| 编号 | 现行结果 | D123 要证明什么 |
|---|---|---|
| D123-RED-01 | Fabric v8 的 67 个 proof 依赖只有 55 known，12 `UNKNOWN`；真实协议 0／3 | 按格子和读取原因判断哪些可严格短路、哪些必须由 actor 观察 |
| D123-RED-02 | 10 格被遮挡，2 格在视野外 | 夹具声明 AIR 不能替代观察事实 |
| D123-RED-03 | backend `TimeoutError` 使 Runtime failed，driver stopping，source 未释放 | 超时只能说明交付未知，不能冒充“命令已证明未应用” |
| D123-RED-04 | 默认执行后的健康只有 sidecar 物理线索，Runtime observation 已过期 | 旧观察不能证明默认后的健康 |
| D123-RED-05 | 通道失联时没有正式新观察 | 只能报告身体责任未重新确认，不能伪造 release |

D122-3 的 20／20、完整 Windows／standalone 门禁和 D122-4 的 0／3 均保持原结论。D123 不回写这些数字。

## 2. D123-0 验收表

状态：**PARTIAL／STOPPED（仅指删除证明未签署）**。实验已经运行；现有证据支持 D123-1 保留全部 12 格并做 actor-only 观察校准。没有据此签署删除依赖，观察路线也没有被判定不可行。详见第 8 节和第 9 节。

| 检查 | 门槛 | 结果 |
|---|---|---|
| 12 格逐格读取归因 | 12／12 有调用点和读取原因 | 已运行：四方向各 12／12；160 条逐读取记录。3 格明确影响父恢复或后继；9 格删除证明未通过 |
| `AIR`／完整方块／`UNKNOWN` 对照 | 每个“格子＋读取原因”的轨迹、风险、伤害、接纳、依赖均有逐项签名 | 已运行：144 项单格对照；轨迹等签名已存。repair-1 用正式 checker 复算门禁：AIR 通过、完整石块 mismatch 拒绝、UNKNOWN unknown 拒绝；旧字段仅为 knowledge-only 历史 |
| 上方支撑 mutation | 去掉或改变支撑后，不得沿用无关依赖结论 | 已运行：96 项完整证明变异；没有签署无关结论或删除依赖；相关 UNKNOWN 仍被正式门禁拒绝 |
| 组合／property mutation | 相邻两格同时变化；支撑形状和扫掠边界同时变化；短路不按方块名 | 已运行：80 项相邻双格、72 项材料边界、144 项支撑／扫掠组合；扫掠 144／144 返回 NEEDS_WORLD；未实现短路算法 |
| 四方向 | 结论一致；差异必须有几何原因 | 已运行：南／东／北／西各 67 依赖、12 下层格；分类一致。材料边界向量不代表原版实际形状 |
| D120 冻结入口 | 972 项旧成功退步为 0 | 已运行并于 repair-1 重算：972 入口／2916 后缀与历史原始结果完全相同，旧成功退步 0；仅验证未改生产基线 |

**通过判定：** 允许混合方案，但每类读取必须有独立证据。仍参与结论的事实变成 `UNKNOWN` 时必须拒绝；在严格屏障前提下已证明无关并移除的格，`AIR`、完整方块和 `UNKNOWN` 都不得改变结果；屏障或扫掠前提失效后，`UNKNOWN` 必须恢复 `NEEDS_WORLD`。证据不足即停止。

## 3. D123-1 验收表

状态：**PARTIAL／STOPPED，独立审查 PASS／已签署**。

2026-10-20，D123-1 已明确授权并执行到固定视点只读验证，现为 **PARTIAL／STOPPED，独立审查 PASS／已签署**。全部 67 个依赖（含 12 个下层格）保留。Windows 原生可见性核心完成四方向共 1248 个相机朝向样本，每方向 12 格均未出现可见空气；其中 9 格另有不依赖相机朝向的完整立方体遮挡论证。仅转头无法补齐所有依赖，继续需要改变观察位置或新增 sensing，触发预登记停止条件。未启动新的 Minecraft／Fabric，未提交 movement trial；actor-only 实机校准和两阶段启动命令未交付。D123-0 继续 PARTIAL／STOPPED，删除依赖未授权；本次签署仅批准 D123-1 PARTIAL／STOPPED 的停止收尾，不授权 D123-2；D123-2／D123-3 未开始。见 D123 验收第 10 节和 `evidence/motion_navigation/d123-proof-and-transport-v1/d123-1/fixed-eye-run-2/stop.json`。

### 3.1 依赖或观察修复

- 四方向动作开始前，所有仍必要的事实必须 known 且 0 mismatch；新的严格查询必须证明被移除的读取确实不参与结论；
- 仍必要的 `UNKNOWN` mutation 必须拒绝；已证明无关并移除的格在三种事实下结果必须相同；
- 支撑删除、碰撞形状变化和身体扫掠进入下层空间时，查询必须恢复依赖或返回 `NEEDS_WORLD`；
- D120 972 个入口逐项不退步；
- 不允许直接把 fixture 真值写入 WorldModel。
- 若走观察路线，必须用 actor-only 校准冻结最大 movement tick 和墙钟上限；需要新增 Java 观察能力、viewpoint 规划或通用校准时停止。

### 3.2 一条命令、两个阶段的前置门禁

| 项目 | 门槛 | 结果 |
|---|---|---|
| JDK／Gradle／缓存 | 项目 JDK、Gradle 8.8、真实非 reparse 缓存 | 未运行 |
| 惰性导入 | 全部部署入口可解析 | 未运行完整 offline preflight；停止前仅执行定向测试导入 |
| 生产语义 | D122-3 的 27／27 文件匹配 | 27／27 匹配，见 fixed-eye-run-2/stop.json |
| proof 事实 | 四方向剩余必要事实 known；已移除读取的两面 mutation 全部通过 | 未通过 actor 校准；67 依赖全部保留。历史真实 actor 为 55 known／12 unknown／0 mismatch |
| validator mutation | 全部检出 | 未运行 |
| offline preflight | 环境、语义哈希、依赖分类、mutation、standalone；这些项失败不启动 Minecraft | 未运行 |
| calibration-only | transport RED 未关时只允许运行 pre-trial，代码入口禁止运动 trial | 未交付 live 入口；只读探针没有 launch 或 trial 路径，拒绝启动的结构测试通过 |
| pre-trial gate | actor-only fixture、传送、健康、起点和必要事实；失败不启动运动 trial | 未运行：固定视点无法解除遮挡，按停止条件退出 |
| 组件与 transport 阻塞 | timely／`L+2` 通过；transport 可行性 RED 阻止启动，留给 D123-2 关闭 | 未运行 |
| standalone | 导入、清单、哈希全部通过 | 初次 3061 文件仅校验旧清单，遗漏本批紧凑停止证据；限定修正见第 11 节。仅为停止切片检查 |

offline 的环境、语义、proof 或导出项失败时，命令必须在启动 Minecraft 前非零退出。它们通过后可用 `calibration-only` 启动 pre-trial，但 transport RED 仍阻止任何运动 trial。pre-trial 失败时必须关闭运行。

## 4. D123-2 验收表

状态：**NOT STARTED**。

### 4.1 协议可行性 RED

- 冻结 Runtime `local_not_dispatched`、客户端 `remote_rejected_before_sampling`、发送后交付未知和整个通道失联四个注入点；
- 优先复用 full-request `rejected`。一次性 `operation_rejected` 仍可能留下 movement／look 租约，不能证明整条 request 未应用；
- `not_applied` 必须绑定 episode、sequence、generation、首效窗口和因果 tick；
- send／receive 异常或连接 seal 必须分类为交付未知或通道失联；
- 现有协议不能表达时，允许最小 typed `not_applied` Java／wire 变更；需要第二控制通道、通用重连或新 Session 状态时停止。
- Java／wire 有修改时：action ≤ 16 KiB、receipt 集合 ≤ 64 项；客户端 tick 无证明、摘要或阻塞 I/O；v1 原字节兼容；D122-3 正式 20／20 不退步；解析／排程性能通过。

### 4.2 命令已证明未应用，通道仍活

- `local_not_dispatched` 由 Runtime dispatch decision 证明未调用 backend；decision 绑定完整身份和因果 tick，不为未发命令制造 receipt；
- 后续 observation-only／neutral exchange 使用自己的 sequence；其 receipt 不得绑定、引用或冒充未发命令的 receipt；
- `remote_rejected_before_sampling` 由原命令身份的 full-request `rejected`／typed `not_applied` 证明；不得用超时或 `operation_rejected` 推断；
- 两条路径的目标请求都没有客户端物理样本；远端拒绝路径的 `input_applications=0`、timing event 为拒绝，且没有 lease／default owner 误归；
- 父默认完整执行；
- backend 返回带 episode、sequence、generation、窗口和因果 tick 的 typed `not_applied`；
- 新观察晚于 `default_exhausted`；
- 新观察证明着地和健康；
- Runtime 仍可用；
- driver 有界结束或重规划；
- prepared frame 清空，source 正式释放；
- `default_exhausted → 新观察或 typed 终态` 不超过冻结的 movement tick 和墙钟上限。

### 4.3 发送后交付未知

- Runtime 状态为 `failed`；driver 状态为 driver 级 `UNRESOLVED`；只清 `_prepared_deadline`／`_prepared_proposal` 并禁止重发；
- 不走普通 discard／cancel；封存 ledger `IN_FLIGHT`、supervisor controller／route、proof inbox `EXECUTING`／`SAFE_TAIL` lease、source 和 owner 记录；
- 安全结果为不可回写的 `safety_responsibility_unresolved=true`；
- 下一帧不允许 driver propose／tick 或继续导航控制；
- 立即返回一次 typed `BODY_RESPONSIBILITY_UNRESOLVED`，不等待恢复，不声称命令未应用或身体安全。

### 4.4 整个通道失联

- 只凭失联前 `fallback_armed` receipt 证明默认已安装，并按客户端合同应有界执行；
- 没有新观察时，不声称默认实际执行、健康未下降或身体已经着地；
- Runtime 为 `failed`，driver 为 `UNRESOLVED`；只清 `_prepared_deadline`／`_prepared_proposal` 并禁止重发；
- 立即报告一次 typed `BODY_RESPONSIBILITY_UNRESOLVED`，下一帧禁止继续推进；
- 不调用普通 discard／cancel；封存 ledger `IN_FLIGHT`、supervisor controller／route、proof inbox `EXECUTING`／`SAFE_TAIL` lease、source 和 owner 记录；
- 安全结果为不可回写的 `safety_responsibility_unresolved=true`；
- 旧 observation、伪造 release、把 `operation_rejected` 冒充 `not_applied`、把交付未知当明确拒绝、提前释放 lease 或普通 discard／cancel 的 mutation 全部被检出；
- 无界等待为 0。

### 4.5 `BODY_RESPONSIBILITY_UNRESOLVED` 结果表

| 场景 | Runtime | driver | prepared 本地帧 | 安全结果 | 下一帧 |
|---|---|---|---|---|---|
| 发送后交付未知 | `failed` | driver 级 `UNRESOLVED`，只报告一次 | 只清 deadline／proposal，禁止重发 | `safety_responsibility_unresolved=true`；ledger／supervisor／proof lease、source、owner 记录已封存 | 禁止 propose、tick 和导航／控制推进 |
| 整个通道失联 | `failed` | driver 级 `UNRESOLVED`，只报告一次 | 只清 deadline／proposal，禁止重发 | `safety_responsibility_unresolved=true`；ledger／supervisor／proof lease、source、owner 记录已封存 | 禁止 propose、tick 和导航／控制推进 |

这张表是 D123-2 的固定合同。不能退回 `stopping` 后继续逐帧等待。

### 4.6 resource-only close 验收

关闭顺序固定为：封存 unresolved 结论和 owner 记录 → Runtime／backend close → driver／Session resource-only close。最后必须同时满足：

- `safety_responsibility_unresolved=true`，且在整个关闭过程不变；
- `local_resources_closed=true`，worker、线程、句柄和资源容器有界结束；
- ledger、owner 和远端命令没有被改写为 `released`、`expired` 或 `cancelled`；
- 没有用单个 `released` 布尔混用两种结果。

优先复用既有 inbox／worker close；若不足，只允许新增薄 driver／Session resource-only close 入口。mutation 必须检出关闭顺序颠倒、资源 close 回写安全结论、提前释放 lease 和混用 released 布尔。

三类结果都不能绕过 Runtime。需要新观察时必须经过 backend。`local_not_dispatched` 由 Runtime 本地 decision 证明；未发命令不要求、也不得伪造 backend receipt，后续 neutral observation 使用独立 sequence。夹具合成 receipt 不计入验收。

D123-2 结束时还要把最终三态接入 D123-1 的同一条 offline preflight。`timely`／`late_window_rejected`／`remote_rejected_before_sampling` 必须 3／3；`local_not_dispatched` 另在正式组件层通过。在此之前命令始终不得启动产品 smoke。

## 5. D123-3 Fabric 验收表

状态：**NOT STARTED**。

### 5.1 南向 smoke

| 场景 | 数量 | 门槛 | 结果 |
|---|---:|---|---|
| `timely` | 1 | 后继只在 `L+1` 首效并完成 | 未运行 |
| `late_window_rejected` | 1 | `L+2` 在输入采样前 full-request 拒绝；后继样本 0；默认后新观察确认安全 | 未运行 |
| `remote_rejected_before_sampling` | 1 | 本应 `L+1` 及时生效的请求在独立注入点、输入绑定前 full-request 拒绝；reason 不同于 `window_missed`；默认后取得新观察并释放 source | 未运行 |

smoke 必须 3／3。任一失败，36 场记为“未运行”，不得补跑或现场修正。

### 5.2 第二次正式运行：正式矩阵

四方向 × `timely`／`late_window_rejected`／`remote_rejected_before_sampling` × 3 次，共 36 场，只允许运行一次。smoke 与矩阵是两次正式运行。

每场检查：

- v2 握手和生产语义哈希；
- 父 default 在 `L` 前安装，`L` 已 armed；
- first-effect window 和客户端真实采样 tick；
- Runtime receipt、F-TICK、sequence 和 episode 对应；
- 单一 movement winner，owner 无空档；
- 默认覆盖完整证明时长；
- 默认后的新观察、健康和身体状态；
- driver 终态、prepared 清理和 source 释放；
- 零伤害、零 stale observation、零无界等待。

通过门槛为 36／36。任何一项失败都保留原始结果并停止。

offline 和 pre-trial 两阶段都通过，才计为一次 Fabric attempt。环境或夹具在首个运动 trial 前失败，不消耗 36 场矩阵 attempt，但 D123-3 仍停止；不得现场修正后重跑。

## 6. 性能和回归

- Python 生产控制准备 P95 ≤ 8 ms、P99 ≤ 15 ms、最大值 < 30 ms；
- 同步提交计算 < 50 ms；
- 合法等待单列，不混入同步计算门槛；
- Windows 源码树和独立 standalone 正逆完整分片全绿；
- Java／Python 相邻动态门禁、overlay 编译、导出清单和哈希全部通过；
- 具体旧成功集合退步为 0：D120 972 个冻结入口、D122-3 正式 20 场、D122 v1 兼容 120 项，以及 D123 修改模块对应的冻结集合；
- 失败、错误、超时、跳过、意外成功和清理失败均为 0。

## 7. 证据边界

D123-0 只读证据位于 `evidence/motion_navigation/d123-proof-and-transport-v1/d123-0/`。后续证据仍只追加到该阶段目录。每个批次必须保留 RED、最终结果、mutation、运行环境和生产语义哈希。Fabric 原始目录可以留在本地 artifacts，但公开索引、哈希和精简结果必须能独立核对。

若 D123 停止，文档必须写明停在哪个批次、哪条预登记门槛，以及后续批次为何未运行。不能把安全停止写成能力通过。

每个批次必须有独立 review 和 sign off，最多两轮限定修正。未签署的批次不能作为下一批的输入。

## 8. D123-0 只读实验结果

最终结论为 **PARTIAL／STOPPED；独立审查 PASS／已签署**。已解释四方向的 12 个下层格为何进入查询，并完成冻结样本对照，但当前 `BlockGeometry` 正式契约下，尚不能证明删除这 12 个下层依赖格安全。签署的是只读实验及停止结论，没有批准任何删除。没有修改生产代码，也没有运行 Runtime、Java 或 Fabric。

| 检查 | 结果 |
|---|---|
| D122 输入冻结 | 四方向各 67 个联合依赖、12 个下层格；南向平移为 `(x+16,y-64,z+33)` 后与 v8 的 12 格完全相符；父 proof、固定后继、全部 fallback 和所需事实已保存 |
| 单格对照 | 四方向 × 12 格 × AIR／完整石块／UNKNOWN，共 144 项 |
| 上方支撑变异 | 去掉支撑或改成下半砖，四方向共 96 项完整证明重放 |
| 相邻组合 | 每方向 10 对相邻格，各做双实体／双 UNKNOWN，共 80 项 |
| 材料合同边界 | 每方向 9 个有上方支撑的格，各做两格高普通／非普通材料形状，共 72 项 |
| 支撑与扫掠组合 | 去掉支撑、支撑改半砖并降低身体、身体进入下层空间，共 144 项；全部返回 NEEDS_WORLD，并包含目标格 |
| D120 原始输入 | 四份原始 coverage 的 SHA-256 与历史索引逐项相符；972 个入口、2916 个后缀重算与原结果完全相同，旧成功退步 0 |
| 定向验证 | 新探针与 D120 相邻检查共 34／34；新探针自身 8／8 |

南向格子按 `(0,62,z)` 表示。其它方向保存旋转后的逐格结果。v8 对应 `(16,-2,z+33)`。

| z | AIR／石块／UNKNOWN 的父与后继结果 | 读取结论 |
|---|---|---|
| -6 | 主轨迹、出口、风险、伤害、fallback 行为相同；UNKNOWN 的父与后继均 solved | 碰撞读取只形成屏障候选；材料读取仍有反例；依赖保留 |
| -5 | 同上 | 同上 |
| -4 | 同上 | 同上 |
| -3 | 同上 | 同上 |
| -2 | 同上 | 同上 |
| -1 | 同上 | 同上 |
| 0 | UNKNOWN 使父动作恢复证明 NEEDS_WORLD；后继 binding 仍可 solved | 必须保留并观察，不能只核后继结果 |
| 1 | 上方有支撑，三态行为相同 | 碰撞读取只形成屏障候选；材料读取仍有反例；依赖保留 |
| 2 | UNKNOWN 使父与后继均 NEEDS_WORLD | 必须保留并观察 |
| 5 | UNKNOWN 时父 proof 仍 solved，后继 NEEDS_WORLD | 必须保留并观察，不能只核父轨迹 |
| 6 | 上方有支撑，三态行为相同 | 碰撞读取只形成屏障候选；材料读取仍有反例；依赖保留 |
| 7 | 同上 | 同上 |

调用点分为四类：`physics_1_21.step:475` 的普通碰撞查询、`:504` 的 step 查询、`unsupported_offset:181` 的潜行支撑查询，以及 `_contacts_unmodeled_material:248` 的独立材料检查。`required_cells_for_sweep` 会额外纳入下一层形状 owner，因为其碰撞形状允许向上伸出。`read-paths.json` 保存每个格子、每个读取原因、调用链和次数，包括最终没有选用的 fallback 尝试。依赖集合来自正式重放。run-1 的 `offline_dependency_gate` 实际只查 UNKNOWN，属于 knowledge-only 历史，不能称为正式接纳；第 9 节追加正式 checker 的 unknown／mismatch 检查。两者都不是 Runtime admission。

现行 `BlockGeometry` 允许本地 Y 到 2。把受支撑格的下层形状改成两格高，普通石块仍可通过；相同形状改用非普通材料后，父动作或后继会拒绝。四方向 36 个受支撑格都能构造这项差异。这说明碰撞轨迹相同不足以删除独立材料读取。该向量是现行数据合同的边界，不表示原版 Minecraft 存在两格高的蜂蜜块，也不是 Fabric 失败。未知材料和形状的可达范围尚没有足够证据支持完整短路，因此触发本批“安全结论依赖未关闭的材料边界时停止”的条件。

原始 `run-1/classifications.json` 只比较父动作，未把 local z=5 的后继差异计入该候选分类。原文件不回写；最终结论使用 `classifications-final.json`，同时检查上方几何、父动作、后继和材料读取。没有移除任何依赖，也没有签署短路算法。D120 重算验证的是当前未修改的生产基线，不能作为删依赖算法的回归结论。

第一次定向检查为 4 通过、1 失败、1 错误：测试误要求 gap 的父与后继同时失败，并把包含 bytes 的 snapshot 送入 JSON 摘要。修正仅涉及测试断言，原日志保留。之后 6／6 通过，补齐证据与最终分类检查后，新模块 8／8，与 D120 合跑 34／34。`git diff --check` 的本批范围检查见最终自检记录。

D122 v8 没有保存历史序列化 proof 摘要。本批冻结的是相同确定性 `fixed_case` 构造器重建的 proof，并绑定当前源码、67／12 依赖身份及 v8 坐标；不把不存在的历史摘要伪造成校验结果。原始 D120 的四份完整结果则确实可取得并逐字节核验。

WorldModel 的 owner 版本命名空间在每次构造时不同。`run-1` 保存的是该次实例的原始 proof 和依赖版本；事后补采的 `required-facts.json` 仅提供另一次实例的语义事实，不能称为同一个 snapshot。第一次自检误要求两实例 proof 摘要相同，已失败并保留记录。最终自检分别核对各自来源，不改写任何 proof 摘要。

D123-1—D123-3 均未运行。后续若要短路，必须先关闭“碰撞屏障之外还有哪些材料读取”的合同边界；若选择观察，当前仍须为全部保留依赖取得正式 actor 事实。不能用本次三态等价把 UNKNOWN 改成 AIR。

现有证据支持后续保留全部 12 个依赖并验证 actor-only 观察／校准路线；D123-1 没有自动进入。STOPPED 仅表示删除依赖的证明未签署，不表示观察路线不可行。保留全部事实不需要先证明这 9 格都可删除；校准仍须真实取得事实并通过正式门禁。本批没有运行 actor-only 校准，也没有 actor 校准或 Fabric 通过结论。

## 9. 独立审查修正 round 1

修正仅涉及门禁量尺和结果表，没有扩大物理实验。原始 run-1、旧最终索引和旧自检均保持原字节。追加证据位于 `d123-0/repair-1/`。

旧 `offline_dependency_gate` 只检查 UNKNOWN，因此会把已知但与冻结 fixture 不符的石块记为 `known`。这个字段现在明确归为 knowledge-only 历史，不能作为正式依赖接纳。新分析对原有 396 行（4 baseline、144 单格、96 支撑变化、80 相邻组合、72 材料边界）逐行重建事实覆盖，并直接调用 `inspect_fabric_dependency_knowledge()`。每行记录 known、unknown、mismatch 数量、坐标和 typed 结果；unknown 或 mismatch 任一非零即拒绝。没有改变 proof、观察事实或 checker 本身。

受支撑格的 AIR 对照得到 67 known／0 unknown／0 mismatch 并通过；完整石块得到 66 known／0 unknown／1 mismatch 并拒绝；UNKNOWN 得到 66 known／1 unknown／0 mismatch 并拒绝。碰撞轨迹相同不能覆盖事实不匹配，三个状态不能再共用一个“known 即接纳”的结论。覆盖测试、D120 精确重算和最终自检结果见 repair-1 的日志与摘要。

396 行正式复算为 52 PASS、160 FACT_MISMATCH、100 NEEDS_WORLD、84 NEEDS_WORLD_AND_FACT_MISMATCH；其中 160 行曾被旧 knowledge-only 字段记为 `known`，正式门禁均拒绝。新修正测试 6／6，合并原探针与 D120 相邻检查为 40／40，零失败、错误和跳过；D120 972／2916 再次精确相同，旧成功退步 0。历史文件哈希、自检和 `git diff --check` 通过。

## 10. D123-1 固定视点验证及停止

本批按 brief 授权保留全部 67 个依赖，先验证现有 actor-only 观察路线。最终为 **PARTIAL／STOPPED，独立审查 PASS／已签署**。继续需要改变观察位置或新增 sensing，触发预登记停止条件，因此没有交付 live calibration-only 入口或完整两阶段启动命令，也没有启动新的 Minecraft／Fabric。D123-2 和 D123-3 未开始。

这里区分三份证据：

- D122 v8 的真实 actor 观察是 55 known、12 unknown、0 mismatch，12 格中 10 个 `occluded`、2 个 `outside_view`。原 trace SHA-256 重算与历史索引一致；传送稳定后的 sequence 3—6 都没有在正式 block 事实中提供这 12 格。v8-preflight 的 67／67 来自离线 fixture，不能作为 actor 校准通过。
- 本批只读探针把冻结 fixture 的不透明支撑几何送入现有 Windows native 可见性核心，固定眼睛位置，扫描 yaw 每 15°、pitch 每 15°。四方向各 312 个朝向，共 1248 个计算；每方向 12 格均没有出现 visible air。这是诊断计算，不是新 actor observation，没有写入 WorldModel。
- 对每方向 9 格，另计算从眼睛到候选完整立方体任意点的线段。线段穿过支撑顶面时，交点始终落在已声明的不透明支撑矩形内部。交点范围由 8 个顶点的极值给出。这一论证不依赖 yaw／pitch，补足了有限角度采样不能穷尽所有相机朝向的边界。至少这些格无法靠固定位置转头取得事实；其余 3 格只作有限扫描结论。

先保存了缺少 calibration 入口的 RED。新增测试覆盖全部 67／12 依赖、UNKNOWN 拒绝、静止／健康条件、双时钟边界、产品模式拒绝和 offline 失败发生在 launch 之前的结构顺序。环境、语义、proof、standalone 四种失败分别确认 launch 未调用。这些是独立结构测试，不表示完整 offline preflight 已实现。现有正式 adapter 的多帧累计测试也通过；本批没有引入 live adapter 或 fixture 回填。

Windows 定向检查 66／66，零失败、错误和跳过。Windows native 构建首次因未设置 JAVA_HOME 失败，原日志保留；显式选用仓库既有 OpenJDK 21 后构建和 JNI 动态检查通过。只读探针以退出码 2 报告停止；run-1 保留，run-2 追加核验原始 actor trace 哈希及多帧事实。D122-3 27 个生产语义文件全部匹配。生产代码、物理、proof、安全阈值、Java sensing 和 transport 均未修改。

原始证据入口：`evidence/motion_navigation/d123-proof-and-transport-v1/d123-1/`，最终探针为 `fixed-eye-run-2/stop.json`。只读命令为：

```text
.venv/python.exe scripts/d123_actor_calibration.py --output <不存在的新证据目录>
```

探针不会启动 Minecraft，不支持 smoke／matrix，也不能把退出码 2 当作 calibration PASS。其 60 秒、1248 次 native 调用上限只约束只读诊断，movement tick 为 0；它不是已验收的实机观察期限。测试中的 80 tick／30 秒仅为未执行的 live 合同草案，不构成发布门槛。

停止后只运行受影响检查与 standalone 导出验证。初次独立快照 verify 3061 个文件，隔离导入 44 个 runner 依赖并运行本批 11／11 测试，全部通过；这次 verify 没有发现旧清单遗漏本批紧凑证据，第 11 节修正该项；没有执行完整 Windows 正逆矩阵、D120 972 入口的新一轮全量重算、D123-2 transport 修复或产品性能验收。既有 D123-0、D120 与 D122 历史证据和结论保持原字节。

## 11. D123-1 独立审查限定修正 round 1

独立审查发现一项 P2：当前入口引用 `fixed-eye-run-2/stop.json`，但原导出清单没有选择 D123-1 停止证据。verify 只能检查已登记文件，不能从文档引用自动发现遗漏。初次 3061 文件通过只说明旧清单内部一致，不构成公开停止证据完整。

本轮将最终 `stop.json`、原 `index.json` 及索引引用的全部紧凑文件逐项加入 include 和 required。没有使用会带入未来原始轨迹的宽泛 glob。原 D123-1 运行结果、旧索引和失败日志不回写。新增三项导出测试检查公开文件及索引哈希、遗漏任一 include 必须拒绝，以及同时删除文件与 SHA256SUMS 对应行仍因 required 缺失而拒绝。RED 为 3 项测试、5 个断言失败，真实复现遗漏。

原始 actor trace 只由主项目保管，不进入公开 standalone；其 SHA-256 为 `a893782ad9121847e7fa6ff5cb489cd90f5fecf0dd78a9c30238c08d2cdb3bf4`，由最终停止证据绑定。公开快照可以核对紧凑结果及索引内部一致性；完整重放和原始 trace 哈希重算仍需要主项目原始文件，不能声称已公开该大 trace。

限定修正的源码检查 17／17 通过。重导的独立 standalone 包含 3077 个文件，verify 通过；`-I` 隔离环境导入 44 个 runner 依赖，并执行三个新增导出测试和本批 11 项检查，共 14／14 通过，失败、错误和跳过为 0。生产代码未改，旧停止证据及索引哈希保持不变。本轮只关闭导出完整性 P2，不改变 D123-1 PARTIAL／STOPPED 或启动后续批次。
