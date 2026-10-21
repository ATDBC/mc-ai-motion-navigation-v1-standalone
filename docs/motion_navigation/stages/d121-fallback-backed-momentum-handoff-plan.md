# D121 带安全尾段的动量交接计划

日期：2026-10-19，Task 4 与 Task 5 记录补充于2026-10-20。状态：**PARTIAL／STOPPED。Task 4 及时输入组件执行 PASS；Task 5 证明当前 transport 会在 L+2 物理执行已经过期的后继输入，修复需要修改 ActionSnapshot／wire／Java，触发停止条件。** Task 2、Task 3 修复基线和 S2 历史停止证据保留，不运行完整发布矩阵或 Fabric。对应 [D121](../decisions/0121-prove-fallback-backed-momentum-handoff.md) 和[验收表](../acceptance/d121-fallback-backed-momentum-handoff.md)。

## 1. 本次交付

本阶段只在组件链中交付一条通用能力：当前严格动作落地时，后继证明仍有效就继续；否则执行当前动作已经携带的安全尾段。

不接规划器，不新增四格动作 profile，不运行 Fabric。D120 的 972 个入口只作回归输入。

修订后的安全依据是 S1 已证明的精确出口 fallback。Task 2 已建立 D120 固定第一跳的合法组件证明，只有显式打开默认关闭的 `allow_committed_main_suffix` 才允许引用主后缀到 `L`；普通生产请求仍要求每个边界都有立即安全尾段。Task 3 已实现后继可行性绑定；Task 4 仅通过显式绑定的接纳入口执行，普通接纳仍拒绝。完整后继证明仍走 D117 prepared 消息和生命周期。不新增联合证明消息；两份完整证明各自在原有消息中只出现一次，摘要在控制帧和 `L` 帧之外计算。历史 `s2/stop.json` 原样保留，不作为新方案已实现或不可实现的结论。

## 2. 预计文件边界

新增：

- `mc2p/motion_nav/motion_chain.py`：通用后继、fallback 和落地选择值；
- `tests/motion_nav/test_d121_momentum_handoff.py`：RED、mutation、端到端组件矩阵；
- `scripts/d121_momentum_handoff_probe.py`：确定性组件证据与成本汇总。

按需要最小修改：

- `motion_solver.py`：固定输入的有界重放和 typed fallback 证明；
- `prepared_motion.py`、`prepared_motion_proof.py`：保留候选声明的 fallback，不再假定只能 neutral；
- `motion_worker.py`：沿用现有 worker 生命周期交付 prepared 后继，不新增联合消息；
- `motion_candidate.py`：只执行已经绑定的后继或 fallback；
- `motion_coordination.py`：核对紧凑绑定和既有 prepared 后继，在落地边界作唯一选择；
- `action_route_executor.py`、必要时 `execution_supervisor.py`：复用 D117 原子交接和 owner 责任。

明确不改：

- `navigation_session.py`；
- `known_map_planner.py`；
- `route_admission.py`；
- 世界模型、伤害规则和全局容差；
- D120 脚本、清单和历史证据；
- Java 与生产动作 profile。

## 3. S0：先冻结 RED、时序和成本

使用手工构造的“一格间隙—一格平台—四格间隙”和 D120 固定输入，不依赖规划器。

先建立这些失败：

1. 当前独立求解会把第一跳刹到不能进入第二跳；
2. 后继缺失时，neutral 或 back 会离开平台；
3. D120 外接框内、但没有实际证明的状态不能直接获批；
4. jump 和 sneak 同一 tick 同时获胜必须失败；
5. 后继过期、依赖变化或 worker 失联时，不能继续 RUN 或使用未经证明的 neutral；
6. 当前 Runtime 必须稳定实现 `L` 决定、`L+1` 生效。

同时测量：

- 从真实 `L` 状态重放第二跳和 fallback 的物理步数；
- 控制线程只做固定重放时的 P95、P99 和最大耗时；
- 后台结果在 `L` 前准备完成的 tick 余量。

S0 不修改生产行为。若 `L -> L+1` 时序或固定重放成本不成立，立即停止。

2026-10-19 S0 结果：新增的最小探针和 8 项检查通过。Windows 南向后继和 sneak 分支各 30 次，全部由 Runtime 在看到 `L=26` 后提交，并在 tick 27 实际应用；首条回执的 episode、request、tick 和五个 movement 字段与所选输入一致；后缀终态位置、速度与 D120 固定重放一致，零伤害。固定后继与 sneak 重放各 20、8 步；最终重跑的耗时以验收文档和 `s0/final.json` 为准。证据见[验收](../acceptance/d121-fallback-backed-momentum-handoff.md#2-s0-前置结果)。

S0 当时的诊断缺口包括联合证明、后继过期／依赖变化／失联时的正式选择、fallback owner 保留和生产互斥检查。S0 的互斥门禁只属于探针，`L` 前准备余量为 `null`，不能把直接提交的时序结果当作 S2／S3 已通过。本次不再建设联合结果，改由现有 prepared worker 链测量后继 READY 的实际准备余量。D120 有限点、外接框和未枚举中间点均不构成执行许可。

## 4. S1：把安全尾段变成正式证明

新增不可变 typed 值，至少包含：

- fallback 的输入、轨迹和起止 tick；
- 适用的边界、generation、owner 和动作身份；
- 世界依赖及其版本；
- 预计伤害和剩余额度；
- 首条输入允许生效的唯一窗口。

证明逻辑按有界候选顺序尝试 neutral 和调用方明确声明的 fallback。它必须给每个需要收尾的边界一个已证明结果；没有结果就拒绝候选。

neutral 只是一个普通 fallback 候选，不再由代码写死为“安全”的同义词。UNKNOWN、未建模材料和伤害超额继续拒绝。

RED／mutation：

- 删除 fallback、改错 tick、owner 或 generation，候选必须拒绝；
- 删除依赖或把支撑改 UNKNOWN，必须拒绝；
- 只检查最终位置、漏掉中途离开平台，必须失败；
- 把 neutral 恢复成硬编码，D120 动量落地用例必须失败。

2026-10-19 实施结果：prepared fixed-proof 不再把 neutral 写死为唯一尾段。调用方现在声明最多三个带精确 `TickInput` 的候选；proof 为每个边界保留实际选择、输入、逐 tick 轨迹、owner／generation／route／action、精确首效 tick 和窗口、依赖、伤害。旧调用没有新字段时，会在请求边界显式补成与入口 yaw 一致的 neutral，旧证明行为和计数不变。

D120 的 sneak 使用通用 `SNEAK_EDGE_STABLE` 终态：全尾段每 tick 保持正支撑；终点必须着地、保持潜行且水平速度为零；随后还要额外推演一拍 neutral，证明水平位置不变、仍着地且保有正支撑。该 witness 的输入、状态和依赖全部进入 proof。全局 `0.15` 标准支撑门槛没有降低；普通 neutral 继续使用原门槛。

在线 `motion_solver` 也已经使用同一份候选声明。主分支、晚一 tick 分支和重验证结果，都保留每个边界实际选中的 fallback 尾段。旧调用显式使用“保持当前朝向”的 neutral；D121 的 sneak／back 仍绑定精确输入。这样既保留旧转向行为，也不允许 worker 或协调层自行推断 fallback。

独立审查发现并关闭了三类契约缺口：风险区间一度取错恢复边界的松键见证；在线尾段一度没有绑定所属主轨迹边界；松开潜行见证一度漏查 Y、潜行解除和水平速度。最终契约还要求普通 fallback 的终点着地且水平零速。详见验收第 3 节。

## 5. S2：沿用 prepared 后继消息，只增加紧凑绑定

修订后的实现按以下边界推进：

1. 当前证明继续携带 S1 已证明的精确出口 fallback，承诺前必须有效；
2. 在后台核对预计出口的固定后继可行性，在当前证明内只记录紧凑绑定；
3. 完整后继证明通过原有 D117 `PreparedMotionJob` 消息交付，沿用原有 registry 和 apply 生命周期。

绑定必须对应同一请求／generation、精确出口、后继输入身份／摘要和精确出口 fallback 身份。它不能代替后继完整证明或产生执行许可。当前证明和后继证明各自在现有消息中只完整出现一次，关联处只保留紧凑身份与摘要。摘要在后台或准备阶段计算，不能放到控制帧或 `L` 帧。

两项证明要求成立，第一动作候选才能越过最后可放弃点；后继交付不及时仍由已证明 fallback 收尾。现有异步身份、snapshot lease、预算和 registry 继续负责交付，不增加 owner、账本、Session 状态或第二条 worker 管线。

准备结果保留精确入口。D120 采样点和外接框不能替代入口证明。快照 `BUSY`、容量不足、内容无效、旧 generation 和超期沿用 D117 的有类型处理。

代表 1-1-4 场景中逐条测量正式请求与结果消息。2 MiB 上限保持不变，每条消息的规范编码和实际传输编码均须不超过上限的 50%（`1,048,576` bytes）。同时测量后继在 `L` 前 READY 的 tick 余量。

Task 2 已先建立合法当前证明的 RED：D120 冻结的 26 条命令通过主轨迹检查，但旧在线证明在立即收尾处返回 `release_recovery_not_safe`。实现后的 `FallbackTailProof` 分别记录原边界与恢复边界，只保存恢复边界之后实际执行的 fallback 输入和状态。主后缀只保存摘要，父证明核对自己的输入、状态和命令投影，不复制主轨迹状态。D120 的边界 15—25 共用 `L=26` 的精确 sneak 尾段；0—14 保持立即尾段。

明确许可的组件证明沿用 prepared 的单风险区间规则：空中 neutral 即使能在稍后停稳，也不能关闭已打开的风险区间；必须等父轨迹着地且支撑比例达到原有 `0.15` 门槛。UNKNOWN、unsupported、伤害和资源检查继续拒绝不完整或超额结果，两段独立风险区间仍拒绝。`release_safe_command_indices` 只列立即尾段的边界；D120 为 0—14，15—25 不在该集合中。默认请求与默认结果没有这项许可，继续保留旧立即尾段门槛，普通接纳器拒绝带许可的组件结果。

本次只使用测试侧的 `_replay_verified_commands` 包装和单一首效 tick `[1,1]`，精确重放 D120 的 `14 RUN + JUMP + 11 RUN`。现有模板的起跑上限为 6 tick，不能原样表达这条固定输入，因此没有修改生产模板或 profile。补入明确许可和真实 release-safe 集合后，现有 `GapMotionSolveResult` 承载该结果时，规范编码 `300,028` bytes、protocol-5 pickle `36,216` bytes，均低于 `1,048,576` bytes；结果只是现有消息格式测量，没有运行 worker 交付。完整结果见验收第 5.1 节。后继绑定、S3 落地选择与当前完整发布矩阵仍未完成，Task 2 不构成 S2 或 S3 签署。

### 历史 direct-copy 替代方案及停止证据

2026-10-19 历史 S2 结果：第一次临时生产试做把当前 `VerifiedMotionResult` 和后继 `PreparedMotionProof` 重复放进联合对象图，因消息体积停止。该实现及其临时数值已经撤回，不作为可复现证据，也不能据此声称生产联合消息走过 worker。

最终冻结的是测试侧只读复现。它用真实的当前证明和后继证明构造 `DiagnosticJointEnvelope`，专门测量“直接复制两份完整图”这一种表达。固定快照和证明下，代表 `gap -> gap` 的规范内容为 `1,941,363` bytes，占现有 2 MiB 上限约 `92.57%`；protocol-5 pickle 约为 `2,137,3xx` bytes，对象别名会带来几十字节差异，但复跑都超过上限。另一个独立检查向正常 `PreparedMotionJob` 的 `seal_bundle` 边界注入等价 `PreparedMessageBudgetExceeded`，只验证现有映射会得到 `BUDGET_EXHAUSTED -> STALE` 且不发布 READY。它不表示生产联合 payload 已经提交。

当时停止了 direct-copy 表达，没有提高消息上限，也没有增加第二条 worker 管线、owner 或账本。测试侧诊断、RED 和紧凑失败证据保留。证据见 `evidence/motion_navigation/d121-momentum-handoff-v1/s2/stop.json`，本次不改写。

历史 S2 没有证明联合 READY，也没有取得 `L` 前准备余量。本次已授权上面的紧凑绑定和既有 prepared 消息方案，替代联合消息路线；历史失败不能被改记为通过，修订方案也仍须独立验收。

## 6. S3：落地时原子二选一

在 authoritative landed post-state `L`：

- 默认保留当前 owner，提交当前 proof 中精确出口的 sneak fallback，在 `L+1` 生效；
- 只有后继的绑定、完整证明、入口、依赖、时序和批准仍有效，才复用 D117 的动作完成事务选择后继；
- 选择完成后才能释放另一条未使用路径；
- fallback 完成并稳定着地后，才释放当前 owner 或进入既有恢复。

协调层只处理 typed “后继／fallback”选择，不判断具体动作类型。

后继首条输入窗口严格为 `[L+1,L+1]`。未在 `L+1` 生效即 stale，不得在 `L+2` 补跳。测试必须核对真实应用回执，不能只看提交时刻。

先分别建立两个 RED：一是到 `L` 仍未 READY，Python 必须选择 fallback；二是在 `L` 提交后继、物理上却到 `L+2` 才应用，必须证明它不会补跳。第二项尚无保证：`CandidateExecutionWindow` 没有传入 `ActionSnapshotV1` 或 Java／backend，当前只能事后检测。保持无 Java 修改范围；若无法在现有链内稳定做到 `L -> L+1`，立即按原停止条件收尾，不能只报告“检测到迟到”就验收通过。

若真实入口与后台预计入口不同，只允许有界固定重放。不能在线搜索，也不能用 D120 外接框直接放行。

## 7. S4：组件矩阵和发布门槛

至少覆盖：

- 四方向；
- 后继正常、晚 2／5／10 tick、never-return、过期和旧 generation；
- 中间平台依赖改变、UNKNOWN 和无变化 stamp 刷新；
- 真实落地状态精确命中、D117 浮点等价值、D120 有限样本和未证明中间点；
- fallback 正常、输入错 tick、输入丢失和 owner 取消；
- 空闲与受控同核负载各 30 次。

门槛：

1. 正常后继全部连续完成；
2. 1-1-4 组件课程中，后继被拒绝、过期、依赖变化或迟到时，全部使用已证明 sneak fallback 并停在中间平台，零伤害；后继依赖变化场景保持 fallback 自身依赖有效，UNKNOWN／失效支撑不能冒充安全；
3. 同一 tick 只有一个 movement winner，owner 无空档；
4. I18—I23 零违规；
5. D116／D117 和 D120 哈希、行为结论不变；
6. Windows 受影响与相邻检查全绿；
7. 独立公开快照全绿；
8. 代表场景每条正式消息均不超过现有 2 MiB 上限的 50%，完整证明不重复传输，`L` 帧不计算摘要；
9. 后继只允许在 `L+1` 首次生效，错过后不允许 `L+2` 补跳。

完成 tick 和耗时分布要报告，但不能用墙钟改变行为。

D120 的完整全绿矩阵是历史基线，不能当作当前 D121 修订源码的结果。本次完整 Windows 与独立公开快照矩阵须在实现稳定后重跑，运行前均标记未验证。

## 8. 提交和止损

### Task 3 准备切片（2026-10-19）

初版提交 `ec280f7` 实现了紧凑后继可行性绑定和原 D117 准备链的显式固定输入入口，但独立审查为 FAIL：完整尾段依赖、父依赖完整性和落地 anchor 外层身份尚有缺口。下面的初版消息与时序数据保留为历史，修复结果另列。当前证明仍受 Task 2 的 opt-in 和普通接纳拒绝保护。它没有进入旧执行器。

固定后继只重放调用方事先选定的 21 条输入，不搜索其他方案。可行性绑定保存当前主轨迹和精确出口 fallback 的摘要、出口／入口、唯一 `[27,27]` 窗口、后继固定输入、同源语义版本、伤害余额和预算。后继依赖与当前证明依赖分别保留。完整后继继续经过 `PreparedActionPreparation → receiver snapshot → PreparedMotionRequest → PreparedMotionJob → PreparedMotionBundle → registry → async apply`；stream 只携带紧凑引用。

四方向已各用真实 `MotionSolverWorker` 完成一次请求和结果传输，并安装为候选。最大正式消息为 `526,985` bytes，低于 `1,048,576` bytes。当前结果只测既有 `GapMotionSolveResult` 格式；D120 首跳仍由固定重放组件包装，没有提交当前搜索作业。固定后继重放为 21 步，同源父轨迹／出口尾段／松键见证复核为 36 步，完整 PREPARE 为 253 步。

到 `L` 仍未 READY、晚 2／5／10 tick 或失联的结果不能恢复本次资格。查询和 apply 只比较已保存的对象、版本和摘要，不重建完整证明。安装候选不推进 action index，不创建执行许可，不启动后继控制器，也不更改身体 owner 或伤害账本。

准备余量来自冻结的组件 movement clock：首次可见 tick 为 0，`L=26`，余量为 26 tick。四方向提交到首次可见的实测墙钟约为 951—997 ms。这不构成完整导航帧的时间保证；prepared wire 没有 worker 完成 tick，证据明确留空。

初版证据在 `evidence/motion_navigation/d121-momentum-handoff-v1/task-3-preparation/`，历史 `s2/stop.json` 不变。下一切片仍须实现 committed suffix／精确出口 fallback 的真实执行、`L` 二选一和传输迟到门禁。Task 3 只签署准备范围，不签署 S2—S4 或 D121 完成；完整源码树和独立公开快照矩阵仍未运行。

### Task 3 审查修复（2026-10-19）

绑定后的父 proof 单独保存同源快照的完整语义版本。binding 的父依赖位置必须与 `world_dependencies` 精确相等，版本必须与 proof 中保存的来源版本精确相等；既有主轨迹摘要同时绑定这份版本。普通 proof 保留原行为，不要求新增版本。

`L` 查询先比较父动作和固定后继主轨迹依赖，再取得原 registry handle，复核完整 proof 及全部安全尾段的依赖。只用于完整尾段的方块变化或 UNKNOWN 都会撤销候选。纯观察 stamp 刷新仍可用；已登记并 apply 的完整 proof 不因 worker 随后死亡而在此查询中失效，既有 D117 撤销语义不变。

stream 紧凑引用现在保留输入投影版本。`L` 查询还核对 anchor 的 session、ruleset、schema、投影和 world.ruleset，并保留原精确身体检查。删除没有独立验证边界的 `snapshot_digest`；安全依据为同源精确依赖版本和完整 handle 依赖，后继 `dependency_digest` 继续对应自己的版本元组。

修复先新增 12 项行为检查，在 `ec280f7` 上得到 16 个失败（含子例），再做最小实现。Windows 最终聚焦／D121／D116／D117／candidate 147 项、D102 185 项、历史消息门槛／design metrics／standalone export 62 项全部通过，共 394/394，零失败、错误和跳过。`git diff --check` 通过。

新证据追加在 `task-3-preparation/repair/`，不覆盖初版；四方向仍各走一次真实 worker／receiver／registry／apply，最大正式消息为 `526,521` bytes，小于 1 MiB。修复只关闭 Task 3 准备范围内的三类审查问题，未扩大到实际执行或 Task 4。

### Task 4 热路径检查口径修正（2026-10-19）

Task 4 最初把完整 L 控制帧内的全部 physics 调用都设为禁止。三条代表分支的 RED 均命中 Session 原有 `motion_residual`：它用上一拍输入预测下一拍，再与真实观察比较。只读基线 `0e6b0141` 的普通正式 L 帧也执行同样的一步。按 D099/D100 允许逐 tick 廉价监视的边界，整帧零 physics 门槛撤回。

修正后的要求是：Task 4 不新增 physics；每个真实观察最多执行一次原 residual step。一次 driver.tick 若同时处理提交前与 backend 推进后的两个不同 movement tick，可以分别监视一次。solver、tail/proof 重放、digest/canonical、pickle 和 variant 重建仍全部禁止。新证据保存原 RED、基线和当前调用归因，Session 与 residual 代码保持不变；最终三分支和完整 tail 检查通过。

### Task 4 及时输入组件执行（2026-10-19）

Task 4 从独立 PASS 的 `0e6b0141` 开始。当前执行器通过显式绑定入口接纳主后缀，并独占 main、tail、witness、pending receipt 和取消状态。普通接纳继续拒绝 committed proof。真实 L 的默认动作来自当前 proof 的精确 fallback，只有完整后继、批准和 activation commit 都成功才覆盖。

Runtime 的可选本地端口借用当前 supervisor 和执行器，取得本次尚未发送的 fallback。拒绝后将本次真实 sequence/window 登记给当前执行器，再由同一 Runtime 出口发送。成功 commit 后不回滚。尾段实际完成松键见证后，复用 supervisor 既有 handoff 缓存清理来源，不降低通用支撑门槛。

两处既有生命周期边界随直接 RED 修复：已 apply/finish 的固定候选不再受等待 worker 的 20 tick 准备期限误清；普通 D117 和新入口在真实安全终态共用现有 release_usage，释放当前已完成 prepared controller 的 executing/safe-tail 引用。registry 的入口比较仅复用既有 D117 数值等价规则。design metrics 只补登记同一执行器内部的 `CommittedExecutionPhase` 和既有 `ReferenceRejectReason`，没有增加动作或控制器选择。

必要组件矩阵为 88/88：四方向各 22 场，包含及时 READY、原进程 worker、2/5/10 tick 迟回、never、worker dead、过期窗口、旧 generation、后继独有依赖改变、stamp-only、六个取消边界、换目标和四个 activation 拒绝点。正常路线 tick48 成功并释放；fallback tick27 首效、tick36 实际见证、tick37 来源释放。全部零伤害，来源／registry／mailbox 归零，释放后四个 free tick 无旧输入复活。最大正式消息 530,709 bytes，小于 1 MiB。正式回归与调用记录见验收和新 Task 4 证据，不使用历史 D120 数字替代。

本切片仍使用手工合法 route 和 TEST_ORACLE；真实进程 worker 四方向都与正常 Runtime 帧同时推进，首次安装可见 tick14，距 L 还有12 tick。真实输入拖到 L+2、全量 Windows／独立快照矩阵、30次空闲／负载签署及 Fabric 均未运行。

最终Windows受影响与相邻检查411/411，474.345 s，零失败、错误和跳过。独立公开导出内聚焦23项和design metrics35项为58/58，66.249 s；导出文件校验通过。Task4及时输入组件执行PASS，不签署完整D121或生产能力。

### Task 4 审查修复（2026-10-20）

初版提交 `a0a4645a` 的独立审查发现一个P1：诊断 reason 字符串参与了释放见证的消费判断。先用正式链完成 tick36 真实见证，再只改 reason，三个合法文本均在旧代码触发通用 rollout 的 RED。生产修复只删除这一条件，继续由缓存 controller 身份、typed disposition 和原有 `RELEASED`／真实回执检查决定释放。

聚焦三项通过；同范围Windows受影响与相邻集合412/412通过（480.240 s），独立导出内Task4和metrics59/59通过（71.762 s），2699文件verify通过。新证据及新语义清单在 `task-4-execution/review-repair/`，旧Task4清单与历史证据原字节保留。未重跑88场矩阵，未扩大授权边界。结论：Task4及时输入组件执行PASS。

S0—S4 分开提交。每个生产切片都先写 RED，再做最小实现和定向回归。每完成一项就复核预计文件边界，防止逻辑扩散。

出现 D121 决定第 6 节任一条件立即停止。停止后保留 D117 和 D120 已签结果；prepared 自动准备继续默认关闭，规划器和 Fabric 不扩大。

### Task 5 真实 transport L+2 RED 与停止（2026-10-20）

Task 5 只改测试、证据和文档。它复用 Task 4 的完整正式链、真实 `ActionSnapshotV1`、Runtime sequence、input ledger、`ClientBehaviorReceiptV3` 和计算器物理。三场的路线、证明、世界事实、prepared 后继和 L 帧 winner 的语义内容相同；运行身份、依赖版本和由这些值计算的摘要按场不同。L=26 提交的完整 successor action 与窗口相同。对照改变的是 backend 到客户端采样点的 transport 时序，不要求不同运行共用同一 proof、WorldView owner 或 prepared message。

- `timely_l1`：sequence 26 的 sprint+forward+jump 在唯一窗口 `[27,27]` 内于 tick27 采样，账本为 `APPLIED`，路线完成并释放；
- `late_l2`：tick27 只观察到上一条租约耗尽；同一个 sequence 26 在 tick28 真实采样 jump，身体从 `(0.5,64.0,2.309556379...)` 变为 `(0.5,64.42,2.778545544...)` 并离地。账本在物理之后记为 `APPLIED_OUTSIDE_WINDOW`；
- `never_applied`：sequence 26 从未物理采样；交接已经提交，后续停止输入不能把仍有动量的身体留在一格平台上，最终身体责任未解决。它与 L+2 补跳分开记录。

接口审计确认：`movement_tick_window` 只在 `ActionIntentV1` 和 Python 仲裁／账本中；`ActionSnapshotV1` 与 `encode_behavior_action()` 的客户端 JSON 都没有 exact movement tick window 或 prepared generation。request sequence 只约束顺序；wall-clock deadline 不是 movement tick；`valid_for_ticks=1` 只限制接受后的采样次数；cancel 是后续 neutral 请求，原命令没有自带 tick 过期条件。因此当前协议只能根据 receipt 事后分类，不能在物理应用前阻止 L+2 jump。

这命中计划第 6、8 节的停止条件。要阻止补跳，需要修改 `ActionSnapshotV1`／wire／Java 门禁，或另行设计客户端预排程和撤销协议；这些都不在 D121 当前授权内。D121 因此记录为 `PARTIAL／STOPPED`。Task 4 及时组件执行仍为 PASS，但不能扩写成完整时序保证。S4、完整 Windows／独立快照矩阵和 Fabric 均未运行。

证据：`evidence/motion_navigation/d121-momentum-handoff-v1/task-5-transport-late/`。初次矩阵、接口审计和5项测试记录原样保留。最终 `matrix-final-audited.json` 补齐 backend 收到的 action、队列、物理前后状态和 winner 来源；`protocol-source-review.json` 保存 Python 与 Java 的字段和采样源码依据。受影响与相邻126/126通过（222.177 s）；随后只补汇总门槛反例，最终诊断8/8、独立导出内诊断及metrics43/43通过，verify通过。测试通过表示探针准确识别并保存 RED，不表示 transport 门槛通过。late 场模拟 FIFO：旧 jump 在 tick28 采样，新 neutral 排在后面；这是一条当前协议不能排除的调度。never 的身体在落地前到达夹具已知世界边界，零伤害不构成安全签署。最终命令见 `checks-final-gates.json`、`export-tests-final-gates.json` 和 `export-verify-final-gates.json`。

Task5独立审查修正追加在 `task-5-transport-late/review-repair/`。新矩阵比较12组实际语义字段和 L 帧 winner 类型／来源类别；运行身份、版本及摘要另列。三场语义内容一致，字面对象不相等。修改实际位置、方块、命令、路线内容的反例会改变语义指纹；最终 action 相同也不能替代世界／证明比较。源码和本轮新增文本统一LF；历史CRLF证据按现有evidence属性规则保留原字节，默认 `git diff --check` 通过。Task5 RED和D121停止结论不变。

## 发布修复 Task 4：诊断与入口更正（2026-10-20）

新的 Task 5 诊断使用 v2 schema，分别记录 `applied_in_window`、`applied_outside_window` 和 `not_delivered`。及时和未送达都没有执行“协议在物理前拒绝过期输入”的检查，因此 `expired_application_prevention=not_applicable`，兼容字段 `protocol_prevented_expired_application=null`。null 不表示成功；晚到后仍应用则明确为 `not_prevented` 和 false。

原 Task 5 证据不回写。新结果追加到 `evidence/motion_navigation/d121-momentum-handoff-v1/release-repair/task-4/`；三场的物理、输入和停止结论保持不变，聚合仍为 `FAILED_STOPPED`。公开入口统一为 `PARTIAL／STOPPED`；D122 尚未开始，必须先完成发布修复的全量 Windows 与独立快照门禁。
