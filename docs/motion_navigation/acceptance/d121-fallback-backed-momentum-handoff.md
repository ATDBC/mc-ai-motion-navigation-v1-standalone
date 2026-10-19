# D121 带安全尾段的动量交接验收

日期：2026-10-19。状态：**PARTIAL／STOPPED。S0 DIAGNOSTIC COMPLETE；S1 COMPLETE；S2 STOPPED；S3、S4 NOT RUN。**

关联：[决定](../decisions/0121-prove-fallback-backed-momentum-handoff.md)、[实施计划](../stages/d121-fallback-backed-momentum-handoff-plan.md)。

## 1. 证据边界

- 正式平台是 Windows；
- 本阶段只验收组件链，不运行 Fabric；
- D120 的 972 个入口只作有限样本回归，`sound_envelope` 继续为 `unverified`；
- 规划组合边、生产四格 profile 和正式能力保持未授权。

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

当前标准证明序列化体积约为：一格跨隙 92,392 bytes、上一格 61,565 bytes、下降 46,820 bytes。都远低于 2 MiB 上限，S1 不因此停止；S2 必须继续报告联合结果的消息体积和实际 worker 交付时间。完整尾段仍会重复保存，暂不把它扩展到长动作链。

## 4. S2／S3 联合交接结果

S2 已运行到预登记消息体积门槛并停止。S3 未运行。

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

## 5. S4 矩阵

尚未运行。最终表至少包含方向、后继交付、依赖变化、落地入口、选中路径、首个生效 tick、owner、伤害、终态和违规。

## 6. 停止与签署

未满足全部门槛时保持 `PARTIAL／STOPPED` 或 `FAILED／STOPPED`，并保留全部失败证据。只有组件范围全部通过，才能签署 D121；这仍不等于授权规划器或 Fabric 产品能力。
