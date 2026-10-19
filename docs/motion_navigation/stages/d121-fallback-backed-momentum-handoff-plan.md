# D121 带安全尾段的动量交接计划

日期：2026-10-19。状态：**PARTIAL／STOPPED。S0、S1 已完成；S2 因测试侧 direct-copy 联合包超过现有 2 MiB 上限而止损；S3、S4 未运行。** 对应 [D121](../decisions/0121-prove-fallback-backed-momentum-handoff.md) 和[验收表](../acceptance/d121-fallback-backed-momentum-handoff.md)。

## 1. 本次交付

本阶段只在组件链中交付一条通用能力：当前严格动作落地时，后继证明仍有效就继续；否则执行当前动作已经携带的安全尾段。

不接规划器，不新增四格动作 profile，不运行 Fabric。D120 的 972 个入口只作回归输入。

## 2. 预计文件边界

新增：

- `mc2p/motion_nav/motion_chain.py`：通用后继、fallback 和落地选择值；
- `tests/motion_nav/test_d121_momentum_handoff.py`：RED、mutation、端到端组件矩阵；
- `scripts/d121_momentum_handoff_probe.py`：确定性组件证据与成本汇总。

按需要最小修改：

- `motion_solver.py`：固定输入的有界重放和 typed fallback 证明；
- `prepared_motion.py`、`prepared_motion_proof.py`：保留候选声明的 fallback，不再假定只能 neutral；
- `motion_worker.py`：沿用现有 worker 生命周期处理联合证明；
- `motion_candidate.py`：只执行已经绑定的后继或 fallback；
- `motion_coordination.py`：准备联合结果，在落地边界作唯一选择；
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

当前缺口已冻结为诊断事实：联合证明、后继过期／依赖变化／失联时的正式选择、fallback owner 保留和生产互斥检查均未实现。S0 的互斥门禁只属于探针。后台联合结果尚不存在，因此 `L` 前准备余量为 `null`，不能把直接提交的时序结果当作 S2／S3 已通过；该余量必须由后续现有 worker 链实际测量。D120 有限点、外接框和未枚举中间点均不构成执行许可。

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

## 5. S2：在现有 worker 内联合准备

对第一动作的每个候选出口执行两项固定工作：

1. 重放后继的固定输入；
2. 证明 fallback。

两项都成立，第一动作候选才能越过最后可放弃点。联合结果使用现有异步身份、snapshot lease、预算和 registry，不增加第二条 worker 管线。

准备结果保留精确入口。D120 采样点和外接框不能替代入口证明。快照 `BUSY`、容量不足、内容无效、旧 generation 和超期沿用 D117 的有类型处理。

2026-10-19 S2 结果：第一次临时生产试做把当前 `VerifiedMotionResult` 和后继 `PreparedMotionProof` 重复放进联合对象图，触发预登记的消息体积止损。该实现及其临时数值已经撤回，不作为可复现证据，也不能据此声称生产联合消息走过 worker。

最终冻结的是测试侧只读复现。它用真实的当前证明和后继证明构造 `DiagnosticJointEnvelope`，专门测量“直接复制两份完整图”这一种表达。固定快照和证明下，代表 `gap -> gap` 的规范内容为 `1,941,363` bytes，占现有 2 MiB 上限约 `92.57%`；protocol-5 pickle 约为 `2,137,3xx` bytes，对象别名会带来几十字节差异，但复跑都超过上限。另一个独立检查向正常 `PreparedMotionJob` 的 `seal_bundle` 边界注入等价 `PreparedMessageBudgetExceeded`，只验证现有映射会得到 `BUDGET_EXHAUSTED -> STALE` 且不发布 READY。它不表示生产联合 payload 已经提交。

阶段按预登记规则停止，没有提高消息上限，也没有增加第二条 worker 管线、owner 或账本。仓库只保留测试侧诊断、RED 和紧凑失败证据。证据见 `evidence/motion_navigation/d121-momentum-handoff-v1/s2/stop.json`。

S2 没有证明联合 READY，也没有取得 `L` 前准备余量。若以后继续，需要先另行决定一种不重复完整图的引用／摘要表达，并重新冻结防伪和消息体积门槛；本计划不把该方向记作已经授权。

## 6. S3：落地时原子二选一

在 authoritative landed post-state `L`：

- 若后继的入口、依赖、时序和批准仍有效，复用 D117 的动作完成事务，提交后继；
- 否则保留当前 owner，提交 proof 中的 fallback；
- 选择完成后才能释放另一条未使用路径；
- fallback 完成并稳定着地后，才释放当前 owner 或进入既有恢复。

协调层只处理 typed “后继／fallback”选择，不判断具体动作类型。

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
2. 不可用后继全部由已证明 fallback 有界结束，零伤害；
3. 同一 tick 只有一个 movement winner，owner 无空档；
4. I18—I23 零违规；
5. D116／D117 和 D120 哈希、行为结论不变；
6. Windows 受影响与相邻检查全绿；
7. 独立公开快照全绿。

完成 tick 和耗时分布要报告，但不能用墙钟改变行为。

## 8. 提交和止损

S0—S4 分开提交。每个生产切片都先写 RED，再做最小实现和定向回归。每完成一项就复核预计文件边界，防止逻辑扩散。

出现 D121 决定第 6 节任一条件立即停止。停止后保留 D117 和 D120 已签结果；prepared 自动准备继续默认关闭，规划器和 Fabric 不扩大。
