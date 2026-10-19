# D121 带安全尾段的动量交接验收

日期：2026-10-19，Task 4 与 Task 5 记录补充于2026-10-20。状态：**PARTIAL／STOPPED（同一 D121）。Task 4 及时输入组件执行 PASS；Task 5 的真实 transport L+2 RED 发生过期物理补跳，现有 wire 只能事后识别，不能在应用前阻止。历史 S2 direct-copy STOPPED 保留。**

关联：[决定](../decisions/0121-prove-fallback-backed-momentum-handoff.md)、[实施计划](../stages/d121-fallback-backed-momentum-handoff-plan.md)。

## 1. 证据边界

- 正式平台是 Windows；
- 本阶段只验收组件链，不运行 Fabric；
- D120 的 972 个入口只作有限样本回归，`sound_envelope` 继续为 `unverified`；
- 规划组合边、生产四格 profile 和正式能力保持未授权。

本次授权修订继续 D121，不新开 D122。历史 direct-copy 表达的失败及 `s2/stop.json` 保持原样；它只说明重复完整图的替代方案超限，不能替代修订方案的验收。

修订方案不使用联合证明消息。安全使用 S1 已证明的精确出口 fallback；Task 2 已建立明确许可下的 D120 固定第一跳组件证明，普通生产请求仍要求每个边界都有立即安全尾段。Task 3 已完成后继可行性绑定；Task 4 的显式入口才可接纳执行，普通接纳器继续拒绝。后继完整证明继续走 D117 prepared 消息和原生命周期。两份完整证明各自在既有消息中只出现一次；摘要在后台或准备阶段计算，控制帧及 `L` 帧只作比较。

当前 D121 源码尚未重跑完整 Windows 和独立公开快照矩阵。D120 的历史全绿结果不能作为当前代码的完整发布结论。Task 2 只修改在线证明表达和风险区间检查，没有接入后继或落地选择。

## 2. S0 前置结果

S0 的最小诊断通过。生产行为未修改；这份结果不签署联合证明或连续两跳能力。

最终证据为 [`s0/final.json`](../../../evidence/motion_navigation/d121-momentum-handoff-v1/s0/final.json)，其中保留逐次耗时、60 条 Runtime 结果、身体状态、D120 manifest 身份和源文件 SHA-256。先前仅调整导入方式的成功诊断保留在同目录 `diagnostic.json`；独立审查发现首条回执内容没有进入门禁后，旧结果保留在 `history/pre-receipt-gate/final.json`。三次计时不混合。

| 检查 | 实测结果 | 证据边界 |
|---|---|---|
| 独立第一跳出口 | 同一夹具从 `(0.5,64,-0.5)` 静止起跳，正式单动作求解成功；出口在 `(0.5,64,1.43780692137306)`，纵向速度 `0.0064323187` 格/tick；D120 名义入口为 `0.2593207914` 格/tick。用独立出口重放 D120 固定第二跳失败 | 起点与 D120 冲刺起跑不同；只证明当前独立动作的停稳出口不能接这条固定后继，不据此证明所有可能后继都失败 |
| 名义后继 | 从 `L=26` 重放 20 步，最终在目标平台停稳，零伤害 | 单个冻结入口 |
| neutral／back 对照 | 分别在第 1／11 步失去中间平台支撑；失败原因均为 `middle_platform_support_lost` | 保留失支撑时的身体状态，不把尚未扣血记成安全 |
| sneak fallback | 8 步停稳，最终纵向位置 `2.2994475301245236`，最小纵向支撑重叠约 `0.00055247` 格，零伤害 | 余量很小，不扩大入口范围 |
| 未枚举中间点 | 偏移 `(.001,0,0,0,0)` 在 D120 外接框内，但分类为 `unproven_midpoint` | 连枚举点也只标记 `enumerated_sample_not_execution_authority`，`sound_envelope` 仍为 `unverified` |
| 输入互斥 | 探针明确拒绝同时选择 jump 与 sneak 两条分支，也拒绝把两者合成一条输入 | 只验证 S0 夹具门禁；生产互斥约束尚未实现 |
| 正式 Runtime 时序 | 南向两分支各 30 次，共 60/60；观察 `L=26` 后才提交所选分支，实际回执首输入均为 tick 27；单个 movement winner；episode、request、tick 和五个 movement 字段与所选输入一致；后缀执行后的终态位置、速度与 D120 重放一致，零伤害 | 使用 `CalculatorBackend → PlayerRuntimeV1` 和正式仲裁出口；未调用尚未实现的 S2／S3 联合准备／原子选择 |
| 固定重放成本 | 每次第二跳 20 步＋sneak 8 步；100 次均实数为 28 次 physics 调用。统一 LF 后的最终 Windows P95／P99／最大值为 `2.4613／2.5178／8.2357 ms` | 同步运行 D120 两个固定后缀，包含 trace 构建和调用计数开销；不含候选搜索、worker 交付或尚未实现的正式身份／依赖复核；墙钟不参与行为 |
| 后台结果准备余量 | `background_ready_margin_ticks=null`，状态为 `joint_worker_not_implemented` | 无法在 S0 诚实报告联合结果在 `L` 前准备完成的 tick 余量；必须在后续已有 worker 链中补测 |

联合证明、后继过期／真实依赖变化／worker 失联时的正式选择、fallback owner 持续保留和生产输入互斥均保存为不可变 typed gap，状态为 `unimplemented`。它们不是已通过的负例／mutation，后续实现仍需先写相应 RED。

检查过程先新增测试，探针尚不存在时导入失败；补齐探针后，首次组件检查因误用 `BodyState.on_ground` 字段失败，修正为现有 `is_on_ground`。独立审查随后发现首条回执的 movement 内容没有进入门禁；新增 identity／tick／五字段 mutation 后，最终定向 8/8 通过。以上修正只在新探针和测试内。未运行 Fabric、S4 扩大矩阵或公开快照。

本次命令：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_d121_momentum_handoff -v
.\.venv\python.exe scripts\d121_momentum_handoff_probe.py --output evidence\motion_navigation\d121-momentum-handoff-v1\s0\final.json --repetitions 100 --timing-repetitions 30
git diff --check
```

探针拒绝覆盖已有证据；复跑时必须选择新输出路径。

## 3. S1 证明结果

prepared fixed-proof 切片已完成：

- 新的共享值只描述候选的精确 `TickInput`、有界 tick 数和终态安全模式，不枚举 neutral／sneak／back 等动作名；
- 每条 `PreparedSafetyTail` 保留候选、owner、generation、route、action、输入开始位置、精确首效 tick／窗口、逐 tick 轨迹、依赖和预计伤害；
- 如果不可撤销输入已经让身体停稳，结果会明确记录候选尚未实际生效，而不是伪造一次输入；
- 候选自身出现 `MISSING_INFORMATION` 或 `UNSUPPORTED` 时，证明器继续尝试后续声明。全部失败后才返回信息量最高的 typed 结果；
- 普通旧请求在 request 构造边界显式补成 neutral，冻结的 fixed-proof 行为和 physics-step 计数不变；
- D120 sneak 尾段没有降低全局 `0.15` 支撑门槛。它使用 `SNEAK_EDGE_STABLE`：全尾段保持正支撑，终点着地、潜行、水平速度为零，并保存一拍 neutral release witness。witness 后水平位置不变、仍着地且正支撑；其依赖计入 proof；
- D120 的 neutral 和 back 仍为 typed `BLOCKED`。

mutation 覆盖：空候选、候选输入被换回 neutral、首效 tick／窗口错误、owner 错误、release witness 缺失或非 neutral、依赖变成 UNKNOWN、移除中途支撑、伤害超额。以上都被拒绝。候选局部 `MISSING_INFORMATION`／`UNSUPPORTED` 后，后续 sneak 仍能通过。

Windows 定向结果：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_d102_prepared_proof tests.motion_nav.test_d102_contracts tests.motion_nav.test_d121_momentum_handoff -q
# Ran 66 tests ... OK

.\.venv\python.exe -m unittest tests.motion_nav.test_d102_registry_lifecycle.RegistryTests.test_next_preparation_keeps_current_execution_ref tests.motion_nav.test_d116_handoff_timeline -q
# Ran 13 tests ... OK
```

相邻六模块首轮运行发现一个测试夹具仍只替换 stream，没有同步 proof 内新增的 fallback 身份；生产校验正确拒绝。夹具同步 owner／generation／route／action 后，相关测试通过。命令中误写了不存在的 `test_d117_prepared_handoff_closure` 模块；这不是产品失败，D117 的代表链由现有 `test_d116_handoff_timeline` 覆盖。

在线求解器也已完成同一套声明：

- `GapSolveRequest` 与 `AirTransitionSolveRequest` 可以声明最多三个 fallback；旧调用显式补成保持当前朝向的 neutral；
- 主分支、晚一 tick 分支、`start_variant()` 和重验证结果，都保存每个边界实际选中的完整 `FallbackTailProof`；
- 尾段必须从所属主轨迹的同一边界开始，不能把另一时刻的尾段改个序号后塞入；
- 普通 fallback 的终点必须着地且水平零速；潜行卡边还要满足逐 tick 正支撑和完整松键见证；
- D120 名义落地点的 sneak 尾段通过，neutral／back 保持拒绝；
- prepared 与在线旧 neutral 都保持每个 tick 当时的朝向。带转向出口的独立对照中，输入、完整 `PhysicsState`、physics-step 和 tail-step 与旧算法逐项一致。

两轮独立审查共发现并关闭：风险区间取错 witness、witness 契约漏字段、tail 未绑定主轨迹边界、普通尾段未检查停稳、旧 neutral 固定入口朝向、prepared 默认语义未同步，以及测试 fixture 没跟随 parent request。生产校验没有为适配测试而放宽。

Windows 定向结果：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_d102_prepared_proof tests.motion_nav.test_d102_contracts tests.motion_nav.test_d121_momentum_handoff -q
# Ran 79 tests ... OK
```

相邻求解器、连续性、worker、下降、交付和启动窗口共 `38 + 53` 项通过。最终 D121 单模块 `29/29`，`git diff --check` 与 `compileall` 通过。S1 没有修改 worker、协调器、Session、规划器、Java 或动作 profile。

S1 当时测得标准证明序列化体积约为：一格跨隙 92,392 bytes、上一格 61,565 bytes、下降 46,820 bytes。都远低于 2 MiB 上限，S1 不因此停止；当时要求 S2 报告联合结果体积和交付时间。修订后的 S2 改为逐条报告既有正式消息体积和后继交付时间，不能复用这些单动作数字代替新代表场景。完整尾段不扩展到长动作链。

## 4. 历史 S2 direct-copy 结果

历史 S2 的 direct-copy 替代方案因消息体积停止。S3 当时未运行。本节只保留该次替代方案的结果；修订方案见第 5 节。

第一次临时生产试做已全部撤回。它暴露了重复完整对象图的体积风险，但该次试做和临时数值不可复现，因此不进入下表，也不能作为“生产联合消息已经经过 worker”的证据。

下表只来自最终测试侧复现。它使用现有 `gap -> gap` 夹具的真实当前 `VerifiedMotionResult`、精确出口 `FallbackTailProof` 和后继 `PreparedMotionProof`，构造只存在于脚本中的 `DiagnosticJointEnvelope`。这个对象刻意模拟“直接复制完整图”的表达，不是生产 request、bundle 或 wire message。

| 项目 | 结果 |
|---|---:|
| 当前完整证明 canonical | `413,586` bytes |
| 当前绑定 canonical | `418,160` bytes |
| 后继 request canonical | `10,153` bytes |
| 带当前绑定的联合 request canonical | `428,348` bytes |
| 后继完整证明 canonical | `544,471` bytes |
| 再次内嵌两份证明的联合事实 canonical | `966,102` bytes |
| 测试侧 direct-copy 规范内容 | `1,941,363` bytes，约为 2 MiB 上限的 `92.57%` |
| 测试侧 direct-copy protocol-5 pickle | 约 `2,137,3xx` bytes；对象别名会带来几十字节差异，复跑均超过 `2,097,152` bytes 上限 |
| 注入的预算异常映射 | 在正常 `PreparedMotionJob` 的 `seal_bundle` 边界注入等价 `PreparedMessageBudgetExceeded`，得到 `BUDGET_EXHAUSTED -> STALE` |
| READY | handle 为 `0`；这里只证明异常映射不发布 READY |
| `L` 前准备余量 | `null`；联合结果没有 READY |

这次失败证明测试侧的 direct-copy 表达不适合现有 2 MiB 消息边界。预算异常检查只验证 worker／receiver 的有类型失败映射。它没有运行生产联合 payload，没有否定 S1 的 fallback 证明，也没有证明“摘要／引用”方案可行。临时生产实现已撤回，没有修改 Session、规划器、Java、动作 profile、worker 上限或 Runtime 输入出口。

复现命令：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_d121_joint_worker_stop -v
.\.venv\python.exe scripts\d121_joint_worker_stop_probe.py --output <新的输出路径>
```

冻结证据为 [`s2/stop.json`](../../../evidence/motion_navigation/d121-momentum-handoff-v1/s2/stop.json)。脚本拒绝覆盖已有证据。

## 5. 修订 S2—S4 的验收门槛

完整矩阵尚未运行，Task 2 的当前证明结果见第 5.1 节。最终表至少包含方向、后继交付、依赖变化、落地入口、选中路径、首个实际生效 tick、owner、伤害、终态和违规。

| 检查 | 必须满足 | 当前证据 |
|---|---|---|
| 1-1-4 正常后继 | 已准备且绑定／入口／依赖／批准有效，完成第二次跨越 | 待运行 |
| 合法当前证明 | 承诺后复用已证明主轨迹后缀至 `L`，再执行精确出口 fallback；引用不复制轨迹状态 | Task 2 明确许可的组件证明通过；D120 的 27 个边界都有 typed 尾段，15—25 引用 `L=26`；普通请求仍要求立即尾段 |
| 拒绝／过期／依赖变化／迟到 | 默认执行当前证明中精确出口的 sneak fallback，停在中间平台；后继依赖变化用例保留 fallback 依赖有效 | 待运行 |
| `L` 前未 READY | 到 authoritative landing `L` 仍未准备好就选择 fallback，首条输入在 `L+1` 生效 | Python 选择路径待验收 |
| 提交后传输迟到 | 后继只在 `[L+1,L+1]` 有效；即使已在 `L` 提交，也不得到 `L+2` 补跳 | RED／必过门槛；当前精确窗口不随 `ActionSnapshotV1` 到 Java／backend，只能事后检测 |
| 输入和责任 | 所有分支零伤害、每 tick 一个 movement winner、owner 无空档，fallback 完成前当前 owner 保留责任 | 待运行 |
| 消息与摘要 | 不新增联合证明消息；完整证明各自在既有消息中只出现一次；摘要在控制／`L` 帧外计算 | 待检查实现 |
| 消息体积 | 2 MiB 上限不变；代表场景每条正式请求／结果的规范编码与实际传输编码均 ≤50%，即 `1,048,576` bytes | 当前结果消息格式测量通过，见 5.1；后继请求／消息和实际交付待验收 |
| 发布回归 | Windows 受影响与相邻、完整源码树和独立公开快照矩阵按计划通过 | 未重跑完整矩阵 |

继续沿用 D117 prepared 接纳、apply、撤销和清理生命周期。不新增 Session 状态、owner、账本或 worker 管线；不授权规划器、生产四格 profile、Java 或 Fabric。支撑门槛、UNKNOWN 和伤害规则不放宽，D120 有限样本不构成连续包络。

原立即尾段限制已由 Task 2 的主后缀引用解决，具体边界和证据如下。传输迟到门禁仍待实现。若无 Java 修改的现有链无法稳定满足 `L -> L+1`，仍按原条件停止；事后报告迟到不能代替阻止 `L+2` 补跳。

### 5.1 Task 2：当前主后缀与精确出口尾段

先写 RED，使用 D120 原夹具的 26 条固定命令和 `[1,1]` 首效窗口。修正测试入口窗口后，旧生产代码通过主轨迹检查，但以 `start_window_release_recovery_not_safe / release_recovery_not_safe` 拒绝证明。实施后同一用例在明确许可下得到合法 `VerifiedMotionResult`，最终状态逐字段等于 D120 名义落地状态。

`FallbackTailProof` 现在保存原边界、恢复边界和主后缀摘要，只保存实际 fallback 段的输入与状态。即时尾段的两个边界相等。D120 的 15—25 都引用 `L=26`，随后才在 tick 27 输入 sneak；各引用共享出口尾段的同一组状态对象。父证明核对自己的命令投影、主输入、连续状态、精确恢复起点、出口尾段、依赖、资源和整条路线的伤害上限。

明确许可的组件证明中，恢复必须发生在父轨迹已着地且支撑比例至少为 `0.15` 的边界。空中即时 neutral 后来能停稳，也不能提前关闭已打开的风险区间；多个独立风险区间仍为 typed 拒绝。没有修改 UNKNOWN、unsupported、伤害或全局容差规则。边界、恢复边界、主命令／输入／状态、候选、fallback 输入、依赖、资源、伤害、witness 和终态的单项篡改均被拒绝。旧单格和转向 neutral 的即时行为继续通过。

首轮 Task 2 的独立审查发现两个阻塞问题：主后缀语义误用于普通请求，且 `release_safe_command_indices` 把延后边界列为立即安全。修复先建立 RED：起点 `(.5,64,1.1)`、向南 `2.0` 格/秒、目标只有一块方块的普通请求，首轮代码错误返回 `SOLVED`；D120 的 15—25 也被误列为 release-safe。修复后，`GapSolveRequest`、`AirTransitionSolveRequest` 和结果均以严格 `bool` 字段 `allow_committed_main_suffix=False` 默认关闭该语义。普通求解和固定重放继续要求每个命令边界有立即尾段；只有测试明确打开许可才能建立组件证明。普通接纳器对带许可结果返回 `REJECTED / committed_main_suffix_execution_not_enabled`，不交给旧 neutral 恢复执行器。

`release_safe_command_indices` 已恢复原义：只列 fallback 在本边界立即开始的位置。D120 只列 0—14，15—25 仍有完整延后尾段引用，但都不在 release-safe 集合内。结果与首效 variant 都核对此对应关系，篡改集合或移除明确许可会被拒绝。普通默认请求在上述反例中恢复为 `NO_SOLUTION_WITHIN_SEARCH / release_recovery_not_safe`；明确许可时可以建立组件结果，但仍不能取得普通执行许可。

现有模板不能原样表达 14 tick 起跑，本次只在测试侧包装 `_replay_verified_commands`，没有增加生产四格 profile。以下体积来自该精确重放构造的现有证明与 `GapMotionSolveResult`，不是 worker 实际交付结果：

| 值 | 规范编码 | protocol-5 pickle |
|---|---:|---:|
| 当前完整 `VerifiedMotionResult` | `299,807` bytes | `36,054` bytes |
| 现有 `GapMotionSolveResult` 结果消息 | `300,028` bytes | `36,216` bytes |

两种结果消息编码均低于 2 MiB 的 50%（`1,048,576` bytes）。主后缀不出现在尾段的输入或状态字段中，pickle 往返后父证明校验通过。未测后继请求、后继消息、交付时间或 `L` 前余量。

Windows 定向结果：

```powershell
D:\My_project\mc_ai\.venv\python.exe -m unittest tests.motion_nav.test_d121_momentum_handoff -v
# 修复后 40/40，包含 11 项主后缀／许可隔离／mutation／体积检查
D:\My_project\mc_ai\.venv\python.exe -m unittest tests.motion_nav.test_air_transition_solver tests.motion_nav.test_b10_gap_solver tests.motion_nav.test_d102_prepared_proof tests.motion_nav.test_d102_contracts -q
# 78/78
D:\My_project\mc_ai\.venv\python.exe -m unittest tests.motion_nav.test_navigation_design_metrics tests.motion_nav.test_motion_continuation_quality -q
# 45/45；设计量尺另单独 35/35
git diff --check
```

任务说明中的 `test_motion_solver_height_transitions` 在仓库中不存在；含该模块的首轮命令记录了 `ModuleNotFoundError`，其余测试执行通过。上面的正式高度转换回归改用现有 `test_air_transition_solver`，没有创建兼容模块。另一次 D121 首轮只有旧 mutation 夹具在更早的构造边界报错；同步原边界和恢复边界后，仍由父证明拒绝错位尾段。

Task 2 完成的是证明表达，不签署 S2、S3 或 D121 整体完成。后继绑定、worker 接入、落地二选一、传输迟到门禁、完整源码树／公开快照矩阵仍待验收；历史 `s2/stop.json` 与 D120 证据未修改。

修复时另完成普通候选／执行器回归 `test_b10_motion_candidate` 46/46。其一处测试原先只替换命令，现在用既有固定重放同步输入、状态和尾段，保持完整证明。精确单 tick 重验证暴露 `max(float, *empty)` 的既有调用错误；已把两处伤害聚合改为对 tuple 求最大值，多分支数值不变。上述明确许可、主后缀和 release-safe 检查通过，未实现旧执行器消费延后尾段。

`start_variant(earliest)` 仍会重建并校验 variant，D120 每次会重新计算 11 个后缀摘要。该方法已有接纳、启动和首输入检查调用；不能把摘要概括为只在后台计算。本轮没有新增 `L` 选择路径或新的 `L` 调用。后续集成必须使用已验证对象，避免把重建接到 `L` 或持续控制检查中；目前没有 `L` 帧性能结论。

### 5.2 Task 3：固定后继的准备范围

初版提交 `ec280f7` 的本地检查通过，但独立审查为 FAIL。审查发现完整尾段依赖、父依赖完整性和落地 anchor 外层身份缺口；本节原始数据保留，修复验收见 5.3。四方向分别用 D120 的固定首跳和 21 条固定后继命令生成可行性绑定。当前证明的主输入、尾段和依赖未被后继准备替换；普通接纳仍拒绝 committed suffix。完整后继由原 worker 和 receiver 计算、验证、登记，再经过原 `begin → apply → finish` 安装。四方向都只有 `ON_TIME` 分支，窗口从 stream、request 到 proof 均为 `[27,27]`。

| 方向 | 当前结果 canonical／pickle-5 | PREPARE job canonical／pickle-5／真实 Queue sender | bundle canonical／实际 protocol-5 payload |
|---|---:|---:|---:|
| 南 | 313,926／41,650 | 242,015／93,190／93,190 | 458,359／512,176 |
| 东 | 325,875／42,019 | 243,892／93,775／93,775 | 472,196／526,484 |
| 北 | 324,878／41,995 | 245,133／94,922／94,922 | 472,385／526,985 |
| 西 | 324,847／41,626 | 244,582／94,337／94,337 | 471,624／525,753 |

单位为 bytes。所有值均低于 `1,048,576`，原硬上限仍为 `2,097,152`。Queue sender 按实际 `ForkingPickler` 编码单列；返回 payload 是原 sender 生成的 bytes，没有再次 pickle 后冒充原 payload。当前结果只测格式，当前 D120 worker 没有运行。后继请求和 bundle 没有嵌套当前 `VerifiedMotionResult`，当前 binding 没有嵌套后继完整 proof、bundle、handle 或 sealed bytes。

每方向真实进程各传输一个请求和一个结果。固定后继可行性重放为 21 步，父轨迹及出口尾段／见证复核为 36 步；完整 PREPARE 为 253 physics steps，其中 stopping-tail 为 232，candidate 为 1。准备余量只能记为冻结组件时钟下的 26 tick；实际提交到首次可见约为 951—997 ms。worker 完成 tick 没有现成字段，记录为 `null`，不从 receiver 时间反推。

聚焦检查覆盖坏目标、步数不足、父请求／主摘要／出口 tail 摘要／入口／投影 mutation、prepared 窗口扩大和 fallback 替换、旧 revision／generation／wrong owner、伤害预留、完整 PREPARE 超预算、失联、无 READY 与迟到 2／5／10、UNKNOWN、stamp-only、wrong ledger、只有候选而无批准，以及查询／apply 零摘要与零证明重建。完整候选只提供准备资格；动作、tracker、owner 和账本没有推进，后继发送数为 0。失联结果没有被写成“已经物理执行 fallback”或“已经停稳”。

历史失败检查已改为原始 `s2/stop.json` 的 SHA-256 完整性和新正式消息门槛，不再要求当前代码永远超限。旧 S1 语义清单保留原字节；Task 3 新语义清单覆盖当前交付及未改动的 Task 2 接纳依赖。早期导出检查发现漏列该依赖，初版清单保存在新证据的 `history/`，没有覆盖旧历史。

初版的准备范围签署已被独立审查 FAIL 撤回，修复后的范围见下节。真实 committed suffix／fallback 执行、落地原子选择、1-1-4 物理交接、客户端阻止 `L+2` 补跳、负载和完整发布矩阵均未完成。没有修改 Session、planner、Java/protocol、生产 profile 或安全规则，也没有运行 Fabric。

### 5.3 Task 3 审查修复：完整依赖与落地身份

修复在生产改动前新增 12 项检查，得到 16 个失败（含子例）。随后补齐三处边界：父 proof 留存同源完整版本，binding 必须精确匹配；`L` 查询取得原 handle 后检查完整 proof／tails 的全部依赖；紧凑引用保存投影，查询核对 anchor 外层身份和 world.ruleset。未被消费的 `snapshot_digest` 已删除，后继依赖摘要仍按版本元组验证。

行为覆盖父版本删空、少格、多格、换版本、同时换 proof 与 binding 版本，父动作独有依赖在 prepare 前变化，以及完整尾段独有格的真实变化和 UNKNOWN。上述情况均拒绝。尾段独有格仅刷新 stamp 仍 READY；已 apply 的本地完整 proof 在 worker 随后死亡时仍维持本接口原语义。投影、ruleset、schema、session 和 world.ruleset 错配均拒绝。查询与 apply 没有新增摘要或 proof 重建。

修复证据追加到 `evidence/motion_navigation/d121-momentum-handoff-v1/task-3-preparation/repair/`。四方向各真实传输一个原 PREPARE 请求和结果，再登记、apply 为候选。主轨迹依赖各 32 格，完整 proof 依赖各 33 格；新增的尾段依赖通过原 handle 复核。

| 方向 | 当前结果 canonical／pickle-5 | PREPARE job canonical／pickle-5／真实 Queue sender | bundle canonical／实际 protocol-5 payload |
|---|---:|---:|---:|
| 南 | 316,908／41,588 | 242,123／93,217／93,217 | 458,521／512,365 |
| 东 | 328,664／41,957 | 242,331／93,367／93,367 | 471,051／525,365 |
| 北 | 327,774／41,933 | 244,407／94,732／94,732 | 471,894／526,521 |
| 西 | 327,818／41,564 | 244,690／94,364／94,364 | 471,786／525,942 |

单位为 bytes，最大 526,521，小于 1,048,576；2 MiB 硬上限不变。当前结果仍只测格式，没有提交当前 D120 搜索作业。组件时钟仍冻结于 tick 0，`L=26`；实际提交到首次可见约 1,023—1,153 ms，不构成完整 Runtime 时序保证。原初版和 S1 语义清单均保留，新语义清单记录它们的原字节哈希；历史 `s2/stop.json` 原字节不变。

Windows 最终定向检查共 394/394：聚焦／D121／D116／D117／candidate 为 147，D102 contracts／proof／receiver／registry／action handoff 为 185，历史消息门槛／design metrics／standalone export 为 62。均零失败、错误和跳过，`git diff --check` 通过。完整命令与时长见新 `checks.json`。

本次只签署修复后的 Task 3 准备范围。完整候选不构成执行许可；动作、tracker、owner 和伤害账本不推进。未实现实际 fallback、1-1-4 物理交接或 `L+2` 传输门禁，未运行 Fabric 或全量发布矩阵。

### 5.4 Task 4：及时输入组件执行

实现起点是 Task 3 独立 PASS 的 `0e6b0141`。完整链使用 `RuntimeNavigationDriver → NavigationSession → supervisor → route executor → coordinator → CalculatorBackend`。测试只注入手工合法路线、D120 固定当前证明和已知 TEST_ORACLE 世界；READY 来自原 producer／receiver／registry，批准和实际输入由正式组件生成。夹具在调用 `_activate_planning_route` 后，仅封装复用正式 ground-direct 成功分支的 pending 请求清理；这不构成规划器产品行为证据。

新执行入口明确绑定持有身体责任的当前执行器。主输入、committed suffix、fallback 和 release witness 都逐真实 ledger receipt 推进。普通默认接纳语义保持，compact binding 自身仍无执行许可。L 先产生精确 `[27,27]` 的默认 fallback，完整后继 approval 和 activation commit 全部通过才覆盖。prepare 前以及 prepare／accept／deliver／commit 的拒绝均在同次 Runtime 发出唯一 fallback。成功 commit 后的 backend 异常不补发旧动作，保留身体责任未解决结果。

| 四方向场景 | 场数 | 实际结果 |
|---|---:|---|
| 及时 READY、原进程 worker、stamp-only | 12 | tick27 后继首效，tick48 SUCCESS 后释放 |
| worker 晚2／5／10、never、dead | 20 | tick27 fallback，迟到结果不抢回 |
| 过期 dispatch／旧 generation／后继独有依赖变 | 12 | 当前 proof 保持有效，安全 fallback |
| cancel15／23／24／25／26／29、换目标 | 28 | 原 owner 完成已承诺后缀和尾段 |
| prepare／accept／deliver／commit 拒绝 | 16 | 单次发送 fallback、真实当前执行器登记 |
| 合计 | 88 | 零伤害，来源／registry／mailbox 归零 |

fallback 在 tick27 开始，实际发送并收到 tick36 的松键见证，核对零速、支撑、依赖、owner 和 ledger 后才结束责任。tiny-support 使用本次已观察见证，不重复 `.01` rollout，不降低全局支撑阈值。正常与失败分支都继续四个 free tick，没有旧来源或后继重新发送。

四个原进程 worker 都与每拍推进的 Runtime 同时运行，首次安装可见 tick14，距 L=26 有12 tick；没有冻结 movement clock 等待 worker。工作完成 tick 没有现成 wire 字段，不从墙钟反推。四方向当前结果、PREPARE job 和原返回 payload 的最大编码为530,709 bytes；规范和传输编码均小于1,048,576 bytes，2 MiB 硬上限未改。当前首跳仍是测试固定重放证明，没有把它称为普通生产搜索作业。

Windows 控制帧实测：L 的88个样本 P50／P95／P99／max为1.5077／5.7828／6.2512／6.2512 ms；tail的684个样本为1.2071／1.3601／1.5167／9.4263 ms。这是当前组件矩阵的墙钟记录，不代替30次空闲／同核负载或全量发布签署。

热路径的原始零 physics RED 保留。只读 `0e6b0141` 普通正式 L 帧和当前三个 L 分支都只有一次已有 residual 监视。当前整段 tail 也是每个真实观察最多一次；最后一次 driver.tick 先处理 tick36，再在 adopt 释放时处理新的 tick37，所以整次调用合计两步。新增 physics、solver／tail／proof重放、digest／canonical、pickle和variant重建均为0。Session和残差代码原字节不变。

新增证据只写入 `evidence/motion_navigation/d121-momentum-handoff-v1/task-4-execution/`。`matrix-directions-first-2.json` 和 `matrix-required-1.json` 是88场最终矩阵；早期 runner 错误、东向数值比较 RED、tiny-support、Runtime拒绝、registry完成清理、热路径及分类登记 RED 均保留，不回写历史失败。

最终Windows受影响与相邻检查411/411，474.345 s，零失败、错误和跳过，涵盖D121 Task2/3/4、D102 runtime activation／action handoff／registry／receiver、D116／D117、candidate／continuation、design metrics及standalone export。独立公开导出内聚焦23项＋metrics35项为58/58，66.249 s，文件verify通过。暂存diff与历史禁止文件检查通过；完整命令见`checks-final.json`。结论：Task4及时输入组件执行PASS。

真实 transport 在 L 提交、到 L+2 才采样的物理补跳检查仍是下一独立 RED／STOP 边界。本次没有保证客户端阻止补跳，没有运行 Fabric、完整源码／独立快照矩阵或30次空闲／负载签署。

### 5.5 Task 4 审查修复：reason 只用于报告

初版提交 `a0a4645a` 的独立审查为 FAIL：supervisor 用 `HandoffEvidence.reason` 的具体字符串决定是否消费已观察到的 fallback 释放见证。修复只删除该字符串条件。缓存的同一 `RouteControl`、committed executor、`QUIESCENT` 及原有 `retired_release_still_current()` 决定资格，后者继续严格核对 `RELEASED`、真实回执、精确 tick 和身体状态；owner、ledger、session 和依赖检查不变，没有增加状态或释放出口。

正式链变异测试在 tick36 真实见证完成、原 retire 成功后，只改缓存 evidence 的 reason。三个合法文本在修复前都转入被禁止的通用 rollout，得到三个 RED 子例；修复后 tick37 均释放来源并清空一次性缓存，零伤害。聚焦三项通过；Windows 同一受影响与相邻集合新增此项后为412/412，480.240 s，零失败、错误和跳过。独立导出内 Task4 与 metrics 为59/59，71.762 s，2699文件verify通过。

新证据只写入 `task-4-execution/review-repair/`。新语义清单绑定旧 Task4 清单的原始哈希；旧清单及全部历史证据不改。完整命令见该目录 `checks.json`。初版88场矩阵没有重跑或覆盖，真实transport L+2、Fabric和完整发布矩阵等边界不变。结论：Task4及时输入组件执行PASS。

### 5.6 Task 5：真实 transport L+2 RED

Task 5 在 Windows 组件链运行三个对照。三场都由 Task 4 的正式 `RuntimeNavigationDriver → Session → supervisor → route executor／coordinator → PlayerRuntimeV1 → backend` 产生 successor，并走 Runtime 唯一输入出口。没有延迟 worker、伪造 READY、事后改 receipt 或由 runner 直接发 jump。

| transport 条件 | Python 提交 | 实际客户端采样 | 账本结果 | 业务结果 |
|---|---|---|---|---|
| 及时 | L=26，sequence 26，窗口 `[27,27]` | tick27，sprint+forward+jump | `APPLIED`，tick27 | 路线成功并释放 |
| L+2 | 与及时场逐字段相同 | tick28，仍为 sprint+forward+jump | 物理后才成为 `APPLIED_OUTSIDE_WINDOW`，tick28 | 过期补跳，随后身体责任未解决 |
| 永不采样 | 与及时场逐字段相同 | 无 | sequence 26 后来被更新输入 supersede，零 applied tick | 身体凭余速离开平台，责任未解决 |

L+2 场在 tick27 的 decision 仍是 jump，winner 为1，窗口为 `[27,27]`；该拍 receipt 只包含 sequence 25 的 `lease_exhausted`。tick28 的 Runtime decision 已进入停止并提交 neutral，但客户端采样的是先前排队的 sequence 26：`forward=1, jump=true, sprint=true`。应用后的身体 y 为64.42、竖直速度为0.3332、`on_ground=false`。这不是 receipt 分类误差，jump 已经进入计算器物理。

现有 wire 没有应用前门禁。`ActionIntentV1` 的 `movement_tick_window` 没进入 `ActionSnapshotV1`，编码后的客户端 JSON也没有该字段或 prepared generation。sequence、wall deadline、单 tick lease和后续cancel都不能表达“只允许movement tick27首效”。账本得到应用事实后能正确标记错拍，但那时物理已经发生。

判定：**FAILED／STOPPED**。Task 4 的及时输入结论保持；D121 不签署完整 S3／S4。修复需要修改当前禁止的 Python action contract、wire 和 Java 客户端门禁，或者另行决定等价的客户端排程协议。没有修改生产代码，没有运行 Fabric、完整 Windows 或独立公开快照矩阵。

证据目录：`evidence/motion_navigation/d121-momentum-handoff-v1/task-5-transport-late/`。初次 `matrix-final.json`、`summary.json`、`protocol-audit.json` 和5项诊断记录原样保留。最终 `matrix-final-audited.json` 进一步保存 backend 收到的完整 action、采样前后的队列、物理前后状态和真实 winner 来源；三场完整 action 与 Python 窗口逐字段相同。`protocol-source-review.json` 标明 Python backend 接口、Java decoder、request gate 和 Input.tick 的源码位置与哈希，Java 结论来自源码审计，没有运行 Java 或 Fabric。

late 注入把 sequence26 留在 tick27 的队列中，并让 tick28 新发的 neutral 排在它后面。这模拟一条按提交顺序传输的链路；neutral 尚未被客户端采用时，旧 jump 已到达物理采样点。它不声称所有 transport 调度都会补跳，只证明现协议不能排除这条调度。never 的零伤害也不能算安全：夹具在落地前到达已知世界边界，仍有身体责任未解决。

Windows受影响与相邻检查126/126通过，耗时222.177 s，包含最初6项诊断、Task4执行、D102 activation、design metrics与standalone export。随后仅收紧汇总门槛，新增两项反例：未采样不能冒充应用前拒绝，late身体责任未解决不能签署。最终诊断8/8通过。测试通过只说明探针准确识别 RED，不代表安全门槛通过。最终独立导出内诊断与metrics为43/43；完整命令、耗时和verify文件数见 `checks-final-gates.json`、`export-tests-final-gates.json` 和 `export-verify-final-gates.json`。design metrics无未登记分支。

独立审查确认核心transport RED成立，同时指出“proof／world完全相同”超出了已有比较。修正后逐项计算12组实际内容的语义指纹：路线、入口、轨迹、命令、完整当前证明及fallback、依赖格、快照事实、规则／范围、计算器世界、实际prepared请求／证明／输入。L帧另比较`MovementV1`、单一winner和来源类别`ordered`。三场这些语义内容相同；WorldView owner的epoch、依赖版本、stream/request ID、job token和相关摘要各自独立，不能称为同一个proof或message。生产校验没有排除这些身份字段，只有测试对照把身份值与内容分开记录。

新证据追加在`task-5-transport-late/review-repair/`，旧矩阵与清单不改。源码和本轮新增文本统一LF；`.gitattributes`沿用已有evidence规则，显式保留Task5历史CRLF原始记录，因此可以使用默认`git diff --check`，无需命令级例外。修正后的检查和语义比较记录在新目录中；Task5仍为FAILED／STOPPED，D121仍为PARTIAL／STOPPED。

## 6. 停止与签署

当前状态为 `PARTIAL／STOPPED`。Task 4 及时组件执行保持 PASS；Task 5 transport 门槛为 `FAILED／STOPPED`，完整 D121 不签署。历史失败证据保留；修改 action contract／wire／Java 的后续工作须另行决定，当前没有继续实现。
