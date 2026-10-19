# AGENTS.md

2026-10-19，[D121](docs/motion_navigation/decisions/0121-prove-fallback-backed-momentum-handoff.md) 在同一阶段获授权继续修订，状态为 **AUTHORIZED REVISION／IN PROGRESS**。S0、S1 保持完成；历史 S2 direct-copy 超过 2 MiB 的失败和 `s2/stop.json` 保留。修订方案不用联合证明消息：当前证明保留精确出口 fallback，只增加紧凑后继可行性绑定，完整后继证明继续走 D117 prepared 消息和生命周期。`L` 决定 `L+1`，默认 sneak fallback；后继只允许在 `L+1` 首次生效，错过后不得 `L+2` 补跳。2 MiB 上限不变，代表场景每条正式消息须不超过其 50%。修订 S2—S4 与当前完整发布矩阵尚未完成，下面 D120 的全绿结果仅属于历史基线。见[计划](docs/motion_navigation/stages/d121-fallback-backed-momentum-handoff-plan.md)和[验收](docs/motion_navigation/acceptance/d121-fallback-backed-momentum-handoff.md)。本轮不授权规划、生产四格 profile、Session 状态、Java、Fabric、新 owner／账本／worker 管线或放宽安全规则。

2026-10-19，[D120](docs/motion_navigation/decisions/0120-resume-momentum-entry-calibration.md) 已完成有限入口与实机校准：四方向 972 个联合入口、2916 个后缀通过，Windows Fabric south 3/3、full 36/36，1476 条 full 命令身份对应、离散物理差异 0、真实健康零下降、来源释放和关闭尾迹通过。JDK 预检、reset 健康观察误拒和北向 ±π 角差误拒的历史失败不回写。Windows 源码树正逆各 197 模块／2534 项，独立公开快照正逆各 196 模块／2510 项全绿，失败、错误、超时、跳过和清理失败均为 0；Java 各 3/3，矩阵快照 verify 2634 文件。连续 `sound_envelope` 仍 unverified，D118 历史 FAILED／STOPPED 不改，生产接入和 D118-1—D118-5 未授权。见[计划](docs/motion_navigation/stages/d120-momentum-entry-calibration-plan.md)、[验收](docs/motion_navigation/acceptance/d120-momentum-entry-calibration.md)和 `evidence/motion_navigation/d120-momentum-entry-v1/`。

2026-10-19，[D119](docs/motion_navigation/decisions/0119-repair-zero-height-step-candidate.md) 已完成。`physics_1_21` 现在按原版 Java `float` 规则处理 0 高度 step 候选。D118 raw 只读重放的 7 个 trial、273 个 tick 离散差异为 0。Windows 源码树正逆各 196 模块、2507 项，独立公开快照正逆各 195 模块、2483 项，全部通过且零跳过。D118-PHYSICS-01 已关闭；D118-0 仍保持历史 FAILED／STOPPED，full matrix、连续两跳和后续阶段仍未授权。见[计划](docs/motion_navigation/stages/d119-zero-height-step-candidate-repair-plan.md)和[验收](docs/motion_navigation/acceptance/d119-zero-height-step-candidate-repair.md)。

2026-10-18，[D118](docs/motion_navigation/decisions/0118-calibrate-delay-tolerant-momentum-gap-chain.md) 已执行到 D118-0 并按物理门槛停止，状态为 **FAILED／STOPPED**；D118-1—D118-5 未授权。最新 Windows 朝南 smoke 的 Fabric probe 通过，273／273 条命令身份逐项对应，7 个 trial 中 6 个与计算器一致。`fallback-back-south-0` 在 movement tick 246 暴露计算器遗漏原版 0 高度 step 候选，着地和碰撞结果与实机不同。潜行 fallback 在 `L+1` 安全生效，但余量只有 `0.000551` 格；第二跳入口包络仍未验证。四方向矩阵和生产接入均未运行。见[计划](docs/motion_navigation/stages/d118-momentum-gap-calibration-plan.md)、[验收](docs/motion_navigation/acceptance/d118-momentum-gap-calibration.md)和 `evidence/motion_navigation/d118-momentum-calibration-v1/stop.json`。

2026-10-17，[D116](docs/motion_navigation/decisions/0116-attribute-prepared-handoff-before-changing-order.md) 已执行到 D116-1 并按门槛停止，状态为 **PARTIAL／STOPPED**。D116-0 的正式链时间线更正了 D115 的顺序判断：执行器先发布边界，Session 随后申请批准；最初的拒绝来自完整 `CellFact` 把纯观察时间戳刷新误判为地形变化。测试夹具已支持 `PreparedMotionJob` 正式身份，冻结值校验已支持 `frozenset[T]`。D116-1 改用 `WorldModel.dependency_changes()` 的语义版本：纯 stamp 刷新可继续，owner 缺失、历史不完整及真实方块、流体或碰撞变化仍拒绝。代表链在 tick 20 获批，tick 21 实际应用第二跳输入，单一 movement winner、owner 无空档、零伤害；随后路线 tracker 身份仍停在上一动作，prepared admission 也缺少异步生命周期 `apply` 事件，因此连续两跳仍未完成。未运行扩大矩阵、完整 Windows、公开快照、性能签署或 Fabric。见[计划](docs/motion_navigation/stages/d116-semantic-dependency-repair-plan.md)、[验收](docs/motion_navigation/acceptance/d116-prepared-handoff-attribution.md)和 `evidence/motion_navigation/d116-handoff-timeline-v1/`。

2026-10-17，[D115](docs/motion_navigation/decisions/0115-close-d114-and-enable-bounded-two-jump-continuity.md) 的以下顺序归因为当时记录，已由上文 D116 更正；该阶段已按范围执行并在连续两跳代表闭环止损，状态为 **PARTIAL／STOPPED**。D114 夹具收尾已完成组件验证：传送必须先有命令生效迹象，再满足最少 3 tick 和连续 2 帧稳定；同位姿自然漂移不能冒充传送完成；组件与 Fabric 共用 trial 组装和“本帧没有移动意图”的等待口径。交付估计改为新的动作主体（subject）用 SOLVE 重建，同一主体的求解重试和 REVALIDATE 只上调。正式规划器生成的 `Walk -> JumpGap -> JumpGap` 代表链暴露同帧交接顺序缺口：身体在 tick20 精确命中第二跳入口，但 Session 的批准早于执行器发布边界；下一帧身体已离开入口，证明随后过期。局部自动准备改动已撤回，未运行扩大矩阵、完整门禁或 Fabric。见[计划](docs/motion_navigation/stages/d115-two-jump-continuity-plan.md)、[验收](docs/motion_navigation/acceptance/d115-two-jump-continuity.md)和 `evidence/motion_navigation/d115-two-jump-v1/stop.json`。

2026-10-17，[D114](docs/motion_navigation/decisions/0114-run-b10-through-the-formal-navigation-driver.md) 已完成并签署。B10 协调器矩阵现在每帧都经过正式 `RuntimeNavigationDriver` 的 prepare／Runtime control／adopt，等待 worker 时不再复用即将过期的旧租约；旧 manual／Session 意图不会重新获胜。唯一一次 Fabric 命令完整运行：validation 40／40、turn validation 40／40、R4 60／60、typed negative 24；协调器 baseline 20／20、2／5-tick 16／16、never-return 4／4。原始命令因量尺把传送复位后坐标不变的一帧 `on_ground=false` 当成离开支撑而记录 failed，10-tick 写成 5／8；原始失败保持不变。限定修正只改量尺，没有重跑 Fabric；对哈希不变的原始 trial 和 trace 只读重算后，三项误报关闭，10-tick 为 8／8 安全有界。全部场景零伤害、来源显式释放。生产控制准备 1211 帧 P95／P99／最大为 2.9600／5.4988／5.8878 ms。D113 仍为 FAILED／STOPPED，D110-SOLVE-01 仍打开；连续两跳、零等待交接和新动作仍未授权。见[计划](docs/motion_navigation/stages/d114-formal-driver-harness-repair-plan.md)、[验收](docs/motion_navigation/acceptance/d114-formal-driver-harness-repair.md)和 `evidence/motion_navigation/d114-formal-driver-v1/`。

2026-10-16，[D113](docs/motion_navigation/decisions/0113-unify-control-visible-delivery-and-repair-jitter.md) 已完成授权实现、本地门禁和唯一正式 Fabric，结论为 **FAILED／STOPPED**。交付估计只增不减，纯中性准备允许已证明的着地停稳侧碰，准备上限仍为 5 tick；首次控制可见与取走结果分别记录。固定 33 场和 180 场抖动通过；Windows 源树正逆各 191 模块、2387 项，独立公开快照正逆各 190 模块、2363 项，全零失败、错误、超时和跳过；Java 源树 13／13、快照 3／3，矩阵前后 verify 各 2386 文件。唯一初始定向的两个作业实际首次可见均为 5 tick，任务完成、零伤害、来源显式释放。唯一正式批次完成 baseline 20／20、2-tick 8／8、5-tick 8／8，late1 八项明确不支持；首个 10-tick 场景在观察 2243 以 `TimeoutError／DEADLINE_EXCEEDED` 结束，实际首次可见缺失、注入 `incomplete`，后续 10-tick、never-return 与 R4 未运行。最后观察原位着地、health 20、输入中性，但不能替代安全终态；来源只在 Runtime close 时清除，清理失败为 0。已记录 755 个生产帧 P95／P99／最大为 4.1105／4.4411／4.6834 ms，关联问题为 0；53／54 identity 链完整，唯一缺端点为失败场景。D110-SOLVE-01 保持打开，D112 历史不回写；连续两跳、零等待交接和新动作仍未授权。见[计划](docs/motion_navigation/stages/d113-control-visible-delivery-remediation-plan.md)、[验收](docs/motion_navigation/acceptance/d113-control-visible-delivery-remediation.md)和 `evidence/motion_navigation/d113-control-visible-v1/`。

2026-10-16，D112 已完成授权实现和正式 Fabric，结论为 **FAILED／STOPPED**。控制线程内联动作求解入口已封住，33／33 组件场景通过。两次夹具无效运行分别保留字段错误和未分类客户端异常；期限传递缺口已修，但不是已证实根因。最终正式批次取得 baseline 20／20、2-tick worker 延迟 8／8；5-tick 第一场安全失败，违反必须完成门槛。28 个完成动作证据链完整、窗口错过 0、零伤害。控制准备 P99 为 22.4488 ms，也超过 15 ms；校准前后稳定。10-tick 和 never-return Fabric 场景未运行。D110-SOLVE-01 保持打开，连续两跳、零等待交接和新动作仍未授权。先读 [D112](docs/motion_navigation/decisions/0112-separate-reference-calibration-from-runtime-delivery.md)、[阶段](docs/motion_navigation/stages/d112-worker-delivery-and-degradation-plan.md)、[验收](docs/motion_navigation/acceptance/d112-worker-delivery-and-degradation.md)和 `evidence/motion_navigation/d112-worker-delivery-v1/`。

2026-10-15，[D111](docs/motion_navigation/decisions/0111-remove-b10-observer-gc-distortion.md) 已完成授权实现和唯一一次 600 秒 B10，结论为 **FAILED／STOPPED**。证据缓冲和局部诊断只保留规范 JSON 字符串；全程 GC 的 656 个事件完整配成 328 个区间，零丢弃、未配对或测量错误。142 个样本的 raw wall P95／P99／最大值为 31.8873／33.4408／39.9605 ms；扣除已记录 GC 停顿后的诊断值为 31.4963／32.1705／33.1761 ms，仍超限。原始 `< 30 ms` 门槛不变，不重跑 B10。功能为 40／40 validation、40／40 turn validation、10／10 默认协调链、60／60 R4 和 24 个 typed negative，零伤害；来源显式释放，trace 21805／21805 写入，零丢弃与不确定记录，cleanup failure 为 0，时间归因通过。实现提交的 Windows 源树正逆各 186／186 模块、2334 项，独立快照正逆各 185／185 模块、2310 项，Java 各 3／3，前置 verify 2322 文件通过；结果文档和公开证据在矩阵后补齐。三方 Linux 复核已关闭 D109-N1／N2／N4 与 D110-LINUX-01；两项只改测试的时序修正已采纳，D110 历史 16 文件语义哈希已补齐。连续两跳、零等待交接与新动作仍未授权。见[计划](docs/motion_navigation/stages/d111-b10-gc-attribution-and-buffer-plan.md)、[验收](docs/motion_navigation/acceptance/d111-b10-gc-attribution-and-buffer.md)与 `evidence/motion_navigation/d111-b10-gc-attribution-v1/`。

2026-10-15，D110 已完成已授权实现并停止。调用线程在后端关闭前抓取 close 诊断，诊断与同一条 trace 记录一起接纳和写出；唯一一次 600 秒 B10 的时间归因通过。功能、安全、来源释放和 trace 封存通过，Windows 源树与公开快照正逆矩阵、Java 门禁和快照校验全绿。D110 仍为 **FAILED／STOPPED**：求解墙钟最大值 39.2484 ms 超过 30 ms；它与唯一一次二代垃圾回收重叠，但现有 CPU 计时粒度不足以证明因果，门槛保持不变。Linux settle 与 fd 的最终字节动态复核在 D110 结束时仍待补，后由 D111 关闭。先读 [D110](docs/motion_navigation/decisions/0110-repair-close-diagnostics-and-measure-solver-tail.md)、[阶段](docs/motion_navigation/stages/d110-evidence-and-lifecycle-repair-plan.md)、[验收](docs/motion_navigation/acceptance/d110-evidence-and-lifecycle-repair.md)和 `evidence/motion_navigation/d110-evidence-lifecycle-v1/`。

2026-10-15，D109 的 D0—D8 已完成已授权实现和 Windows 验收。最终 B10 的功能、安全、来源释放和 trace 封存通过：40／40 validation、40／40 turn validation、10／10 默认协调链、60／60 R4、24 个 typed negative，零伤害；trace 写入 22409／22409，零丢弃和不确定记录。D109 仍为 **FAILED／STOPPED**，因为求解最大值 41.0339 ms 超过 30 ms 门槛，time attribution 以 `NoneType` 错误结束。Linux zombie、双 fork 和 POSIX 文件描述符矩阵在 D109 结束时待三方补充复核，后由 D111 关闭；连续两跳、零等待交接和新动作未授权。先读 [D109](docs/motion_navigation/decisions/0109-retest-b10-budget-and-bound-lifecycle-cleanup.md)、[阶段](docs/motion_navigation/stages/d109-b10-budget-and-lifecycle-repair-plan.md)、[验收](docs/motion_navigation/acceptance/d109-b10-budget-and-lifecycle-repair.md)和 `evidence/motion_navigation/d109-b10-lifecycle-v1/`。

2026-10-14，D107 已完成。D103 留下的观察热路径和异步测试回归已经关闭：F1 缩短场景由约 182 ms/tick 降至 14.57 ms/tick；同语义观察只更新证据，不再重建约 3,400 格的不可变 section；几何变化仍立即使旧依赖失效。计划登记的 11 个回归模块全部恢复。Windows 正序、逆序各 184／184 个独立模块、2264 项检查通过，零失败、错误、超时和跳过。D106 的停止结论保持不变。先读 [D107](docs/motion_navigation/decisions/0107-repair-d103-regressions-before-new-motion-work.md)、[阶段](docs/motion_navigation/stages/d103-regression-recovery-plan.md)、[验收](docs/motion_navigation/acceptance/d103-regression-recovery.md)和 `evidence/motion_navigation/d107-regression-recovery-v1/`。

2026-10-14，D106 已部分完成并停止。R1 去掉重复证明摘要后仍未达到 Windows M0 的 P99 15 ms 门槛；R2 的调用计数与性能计时已经分开；R3 已证明现有 UNKNOWN 与依赖复核足以让局部快照保守失败。R4 的第二轮 Fabric 取得 40／40 `NEXT_MOVEMENT_TICK`，但旧 B10 移动入口夹具随后因一次观察跨两个 movement tick、没有进入 `(0.5, 1.0)` 速度带而失败。该失败发生在动作求解前，身体全程安全。连续两跳、零等待交接、T2-3 和新动作仍未授权。先读 [D106](docs/motion_navigation/decisions/0106-repair-d105-cost-measurement-and-tick-diagnostics.md)、[阶段](docs/motion_navigation/stages/d105-cost-measurement-and-tick-remediation-plan.md)、[验收](docs/motion_navigation/acceptance/d105-cost-measurement-and-tick-remediation.md)和 `evidence/motion_navigation/sound-motion-proof-d106-v1/`。

2026-10-13，D105 已按三条独立轨道止损，原失败保持不变。D106 只在各自轨道内修正量尺、复核局部快照和诊断 B10 时间线，不把 D105 的失败改写成通过。历史入口为[架构](docs/motion_navigation/architecture/sound-sparse-motion-proof-v1.md)、[阶段](docs/motion_navigation/stages/sound-motion-proof-and-independent-tracks-plan.md)、[验收](docs/motion_navigation/acceptance/sound-motion-proof-and-independent-tracks.md)和 `evidence/motion_navigation/sound-motion-proof-d105-v1/`。

2026-10-13，D104 止于 K1。K0 冻结的 18 个旧扫描样本可重复；关键边界候选虽在这些样本上减少了物理计算，但独立反例证明它可能漏掉两个采样点之间短暂的 `UNSAFE→SAFE` 风险。候选已从活动 D101 路径撤回，后续性能运行只作诊断，不构成 K2。K2—K7、receiver／快照简化、F-TICK、连续两跳和 Fabric 均未运行。先读 [D104](docs/motion_navigation/decisions/0104-slim-motion-proofs-before-continuity.md)、[阶段](docs/motion_navigation/stages/key-boundary-motion-proof-and-continuity-plan.md)、[验收](docs/motion_navigation/acceptance/key-boundary-motion-proof-and-continuity.md)和[失败索引](evidence/motion_navigation/key-boundary-motion-proof-d104-v1/failure-index.json)。

2026-10-13，D103 G2-F2 止于 Task C。Task A 已统一执行窗口转换并关闭 11／12 tick 漏传；Task B 已把观察请求容量编码移出控制路径，同时保留公共校验和真实 backend 编码。Task C 的真实 spawn 前置试验证明 legacy 第一段没有 registry handle 或登记安全尾迹，late delivery、fallback 与正常 closeout 仍需在控制线程现场 `verified_ground_rollout`。该结果触发预登记止损：Task C 未实现，Task D、唯一正式 F2、Task 10、G3 和 Fabric 均未运行；G2 继续 **FAILED／STOPPED**。先读 [D103](docs/motion_navigation/decisions/0103-repair-g2-window-and-control-hot-path.md)、[G2-F2 计划及结果](docs/motion_navigation/stages/prepared-motion-proof-g2-f2-remediation-plan.md)和[D102 正式验收](docs/motion_navigation/acceptance/prepared-motion-proof-pipeline-d102.md)。

2026-10-11，D101 最小原型止于 M0。安全结果通过，但完整决定 P99 和最大值超过冻结门槛；M0 整体未通过，正式链接入和 Fabric 未授权。现行证据入口为 `evidence/motion_navigation/continuous-motion-d101-v1/`。

2026-10-11，D100 已冻结连续偏离恢复的设计：宽阔、已知且可停车的安全区域默认保持运动；位置、速度和落点偏差优先在运动中局部接回原路线。后段失效但当前前段仍安全时，继续安全前段并后台重规划；被外力推向坑边等危险时，先让短期运动远离危险；越过起跳、离边或下落承诺点后，由原 owner 负责安全落地，再从真实位置继续任务。偏离按当前风险和承诺阶段处理，不为动作组合或偏离原因增加专用状态机。D100 目前只冻结设计，尚未授权实现、正式链接入、P4 或新 Fabric；D098 的 P3 失败与原证据保持不变。现行入口为 [D100](docs/motion_navigation/decisions/0100-use-risk-aware-continuous-deviation-recovery.md) 和[按风险连续运动架构](docs/motion_navigation/architecture/risk-aware-continuous-motion-v1.md)。

2026-10-11，D099 已确定后续连续运动的设计方向：优先利用 Minecraft 稳定、确定且可直接读取事件与事实的特点，采用“短开环输入包＋逐 tick 廉价监视＋事件触发重算＋关键节点严格检查”。普通安全场景不默认每 tick 完整重证；高危任务可以由后续上层策略拒绝、换路、先处理威胁或改变地形。该决定尚未授权实现、P4、正式链接入或新 Fabric；D098 的 P3 失败与证据保持不变。现行入口为 [D099](docs/motion_navigation/decisions/0099-adopt-game-first-risk-graded-short-open-loop-control.md)。

2026-10-11，D098 的隔离实验停止于 P3。Windows 原样复测与三方 D097-A 的 1,423,766 条非计时样本逐项一致；项目自有控制器、冻结表、可信轨迹、增量伤害、主轨迹复用和 24-step 分摊已经完成。P3 的 37×3 场景产生 1,338 个完整决定，P50／P95／P99／最大值为 0.2521／15.8503／19.8099／20.0919 ms。1,227 次后端推进均有 owner、实验账本和许可来源，但承诺后恢复为 0／27，并有 54 次身体责任未解决和 6 次伤害额度超出。P3 未通过，P4 与正式链适配未授权；`mc2p` 未修改，没有新的 Fabric 结论。现行入口为 [D098](docs/motion_navigation/decisions/0098-validate-and-optimize-incremental-motion-proof-on-windows.md)、[阶段](docs/motion_navigation/stages/continuous-motion-proof-performance-r98-plan.md)、[验收](docs/motion_navigation/acceptance/continuous-motion-proof-performance-r98.md)及 `evidence/motion_navigation/incremental-motion-r98-v1/`。

2026-10-10，D097 的连续动作控制器、离线表和增量证明路线在 M0 停止。正式 Windows 只读探针保持 22 个旧正例、6 个负例和混合动作元反例分类正确；混合反例固定使用 10,056 次物理计算，三轮正例最大耗时均超过 400 ms 门槛。M0 未通过，M1—M4 未开始，`mc2p` 未修改，也没有新的 Fabric 结论。单边界增量证明与已知输入 rollout 数字只是从完整扫描拆出的成本估算，不能作为执行许可。见 [D097](docs/motion_navigation/decisions/0097-use-controller-table-and-incremental-safety-proof.md)、[阶段](docs/motion_navigation/stages/continuous-motion-incremental-proof-plan.md)、[验收](docs/motion_navigation/acceptance/continuous-motion-incremental-proof.md)和 [M0 证据](evidence/motion_navigation/incremental-motion-m0-v1/README.md)。

从仓库根目录运行 M0 只读探针：

```text
python -m unittest tests.motion_nav.test_trajectory_proto_m0_probe -q
```

2026-10-10，TP-0 正式对照已通过并冻结为 `da56bfa297cec6e9c0861bb976e608b462237cd2`，生产字节与 `c73c66ed96ce37760661265ae8228f5fca7fb135` 相同。恢复原因、终结不可改写和空中依赖撤销的正式检查通过；Windows 正序、逆序各1713／1713。正序只有直接 unittest 会话摘要，没有逐项结构化记录和场景哈希；逆序保留正式脚本报告且前后场景哈希相同。四方向入口前普通 Walk 均真实晚一 tick，任务4／4完成、零伤害、额外显式期限违规和来源泄漏；原始账本窗口外状态保留，普通延迟单列，不授予新窗口。D093旧失败不改写。见[阶段](docs/motion_navigation/stages/TP-continuous-short-trajectory-prototype-plan.md)、[验收](docs/motion_navigation/acceptance/TP-continuous-short-trajectory-prototype.md)和 `evidence/motion_navigation/trajectory-proto-tp0-v1/`。

Task 3／4 的原型组件检查保留。Task 5A 的 `343d10de` 只保留为旧候选。D096 的 5A-R 中，R1 通过；R2 候选 `cfd36acd` 遗漏独立的地面、起跳和空中输入组合。限定修正关闭该反例后，三个冻结正例都耗尽原 65,536 次物理计算预算，因此 R2 未通过，R3 未开始，P0 未通过，不进入 5B。失败修正只保留为 diff 和结构化证据，候选源码恢复到 `cfd36acd`。公开快照包含隔离原型源码、测试、方案、报告与失败证据；正式 actor、Runtime 和脚本入口不得导入原型，也不能把旧矩阵 94／94 写成 R2 通过。见[5A-R 计划](docs/motion_navigation/stages/TP-task-5A-R-plan.md)、[D096](docs/motion_navigation/decisions/0096-repair-trajectory-proof-and-use-stable-motion-primitives.md)和[失败证据](evidence/motion_navigation/trajectory-proto-5ar-v1/README.md)。

从仓库根目录运行四个原型组件测试：

```text
python -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment tests.motion_nav.test_trajectory_proto_reference_search tests.motion_nav.test_trajectory_proto_primitive_matrix -q
```

2026-10-10，D094 的安全落地后补走已完成。末次严格输入失联但安全落地、且目标仍未满足时，正式链通过现有 `NEEDS_REPLAN` 从当前观察补走；正常目标不满足和其他不安全条件仍拒绝。六族 600 项恢复 122 项，旧成功退步 0；Windows 正序、逆序各 1705／1705；Fabric 四方向 4／4，四次严格空中命令均真实晚一 tick 生效并记录为 `applied_outside_window`，落地后保留 typed `INPUT_LOST` 并补走完成，零伤害、额外期限违规、来源泄漏和二次起跳。见[阶段](docs/motion_navigation/stages/post-input-loss-landing-recovery.md)、[验收](docs/motion_navigation/acceptance/post-input-loss-landing-recovery.md)和 `evidence/motion_navigation/post-input-loss-landing-v1/`。

2026-10-09，D092 的动作入口交接 H0—H3 已完成。正式链 80 项从修复前 0／80 变为 80／80；本轮新增的 Walk 朝向准备分支读取下一段通用入口朝向，同帧地面输入按最终获胜视角计算。Windows 完整正序、逆序各 1670／1670；v7、F2 528、v8 的旧成功退步为 0。Fabric 四方向 normal／首条严格输入晚一 tick 共 8／8，四次晚到都在证明窗口内实际生效，起跳 yaw 误差 0°，零伤害、期限错过和来源泄漏。见[阶段](docs/motion_navigation/stages/action-entry-handoff-repair.md)、[验收](docs/motion_navigation/acceptance/action-entry-handoff-repair.md)和 `evidence/motion_navigation/action-entry-handoff-v1/`。

同日，按D090—D092完成F2-R主线恢复，R0—R4已签署。Windows源树和独立公开快照正序、逆序各1661／1661，零失败／错误／跳过；43组Fabric、46个实际试次全部通过，20次首条晚1由真实应用确认，旧成功退步0。区域几何40／64；柱顶24项在 R4 签署时仍按D091保留为安全有界的Walk→JumpUp能力RED，现已由上面的独立交接阶段关闭。修订8 ms目标与worker资源债未关闭。正式结果见 [F2REC阶段](docs/motion_navigation/stages/F2REC-rebuild-from-f2r.md)和[验收](docs/motion_navigation/acceptance/F2REC-rebuild-from-f2r.md)。

R2按D092同机各222修订帧的P95/P99非退步门槛签署，修订8ms和压力族超限仍为债。R3删除了无正式生产者的防坠能力；Runtime关闭后的Session worker资源债未修复。历史停止、失败和归档保留；柱顶交接只按独立 H0—H3 的新证据签署，不改写 R4 历史。

2026-10-07，F2 通用非中心地面路线执行与终点接近已按冻结范围验收通过。普通开阔路线保留快速路径，已知碰撞或支撑边界每帧最多复核三个候选；潜行只由路线区间授权，完成区域保持在原 `GoalState` 内。正式 Windows 完整正逆各 1627/1627，五组通过，产品 1998/2000，旧成功退步 0；玩家站位 187/400 → 400/400。Fabric 冻结清单 93/93 符合各自判定，80 正例完成、40 次首条晚一 tick 实际应用，三组路线／持续输入用时比为 1.000／1.235／1.074，连续 10 tick 非中性停滞为 0。首个静止普通 Walk 补一 tick 中性等价证明，命令租约与 Session／Runtime 生命周期未改。

范围见 [F2 阶段](docs/motion_navigation/stages/F2-non-center-ground-route-execution.md)、[架构](docs/motion_navigation/architecture/continuous-ground-route-execution-v1.md)和 [D074](docs/motion_navigation/decisions/0074-generalize-ground-route-execution-before-terminal-approach.md)，结果与限制见 [F2 验收](docs/motion_navigation/acceptance/F2-non-center-ground-route-execution.md)。紧凑证据位于 `evidence/motion_navigation/F2-ground-route-v1/final/`，原失败、逐项索引、来源和哈希均保留。外力仅按冻结试次判断；移动活塞观察、动态避障、路线优化器、疾跑路线和新动作尚未交付。

Windows 是正式开发、自动验收和 Fabric 实机平台；Linux 仅用于补充可移植性复核。公开快照不包含本地 `.tmp`、`artifacts`、凭据、世界存档或原始大型轨迹。F2 的详细轨迹比较和 Fabric 原始流审计仍需主项目保管的原始证据，紧凑摘要不能代替完整观察。`EXPORT-METADATA.json` 的来源提交标识本次公开整理；历史验收采集提交和当时的 dirty 状态仍按原记录保存。

历史 M0 通过，M1 结构止损，未通过；动作接口迁移保留，后续接口加固和 Driver 枚举生命周期整改已经完成。原 S1—S4 不再实施，M2—M5 未授权。原结果分别见 [M0／M1 验收](docs/motion_navigation/acceptance/motion-navigation-middle-layer-M0-M1.md)、[加固验收](docs/motion_navigation/acceptance/action-spec-hardening-driver-lifecycle.md)和 [D073](docs/motion_navigation/decisions/0073-accept-action-spec-and-retire-size-gates.md)，历史失败不回写。

历史 F1 按“功能通过、结构未通过”保存；R28 按“时间盒结束，部分交付通过”保存。历史 S0-R、v1—v7、R27 和连续高度 M3 的结果与边界均保留在各自验收及 `evidence/motion_navigation/` 中，不用 F2 的新结果覆盖。历史入口见 [F1 验收](docs/motion_navigation/acceptance/F1-known-world-following.md)、[R28 验收](docs/motion_navigation/acceptance/navigation-coordination-convergence.md)和 [S0-R 验收](docs/motion_navigation/acceptance/post-F1-navigation-structure-cleanup.md)。

本仓库是主项目生成的源码快照。先读修订后的 D121 阶段和对应验收，再按任务读取相关架构与决定；D120、F2REC 及后续正式修复作为各自范围的历史依据保留。

项目方只通过默认分支 `main` 发布由固定导出清单生成、校验通过的公开快照。不得把主项目的完整开发分支或提交历史直接推入本仓库。三方审查分支可以保留，但只保存审查者自己的报告和复现材料，不代表正式发布状态。

## 工程边界

- 正式开发、自动验收和 Fabric 实机环境是 Windows。除非后续决定明确要求支持 Linux，否则 Linux 结果不阻塞阶段关闭。
- 正式机器人只使用结构化观察。未知空间不能当作空气。
- 只有 Runtime 仲裁后的唯一输入出口可以写玩家控制。
- 动作发出不等于成功，必须读取客户端实际应用回执和后续观察。
- 每类长期状态只有一个拥有者。不要复制地图、目标、路线、动作许可或输入账本。
- 不为尚未进入当前阶段的能力建立空框架。
- 新增防御逻辑必须对应现行能力、真实失败、可信复现或冻结验收。假想极端风险先记录边界，不继续堆叠状态、等待、重试和恢复分支。
- 高危任务可以由上层拒绝、换路、先消除威胁或改变地形；底层应报告风险和能力不足，不为强行通过自动放宽安全条件。
- 当前仓库只包含独立 Fabric 正式路径。CraftGround 和旧射线兼容代码只在主仓库作历史参照。
- 采用 fail fast（尽早发现失败）：先用低成本检查验证正式调用链和最容易暴露问题的代表性边界场景；专项检查稳定后，再扩大到完整矩阵和实机验收。
- 出现未预期失败时，先停止扩大验收，保留失败证据，定位根因并检查共同规则；修复后先复跑直接相关场景，不带着已知问题继续批量运行。
- 小范围接口或诊断修改按实际影响复查，并完成阶段要求的检查；没有新变化、失败或未决疑点时，复用仍有效的证据，不自动重跑全部。额外问题先登记并判断是否阻塞当前交付，避免无关扩展当前批次。
- 快速发现失败不得降低验收门槛、删除失败样本或把有界失败计为成功；机器人已经承担的安全收尾责任必须继续履行。

## 文档

- `architecture/`：模块职责、接口、状态归属和不变量；
- `stages/`：当前范围、完成事项和下一阶段条件；
- `acceptance/`：场景、指标、结果和证据边界；
- `decisions/`：设计和验收口径改变的原因。

文档与代码或测试冲突时，先查清事实，再修正文档。不能为了符合说明而删除失败证据。

## 检查

```text
python scripts/export_motion_navigation_standalone.py verify --root .
python -m unittest discover -s tests/motion_nav -p "test_*.py" -v
python -m unittest tests.motion_nav.test_f2_ground_route_gates tests.motion_nav.test_f2_non_center_ground_route tests.motion_nav.test_f2rec_r3_cleanup tests.motion_nav.test_f2_ground_completion_region tests.motion_nav.test_f2_ground_start_window -q
python -m unittest tests.test_c1_melee_evidence tests.test_c1_moving_melee_evidence tests.test_c1_external_motion_evidence tests.test_c1_fixed_melee_runtime tests.test_c1_moving_melee_runtime tests.test_c1_external_motion_runtime -v
python -m unittest tests.test_action_arbiter_v1 tests.test_action_receipt tests.test_player_runtime_v1 tests.test_runtime_failure_disposition tests.test_engagement_memory tests.test_fixed_melee tests.test_fixed_melee_driver tests.test_melee_strike_driver tests.test_moving_melee tests.test_moving_melee_driver tests.test_external_motion_recovery_driver tests.test_c1_navigation_session -v
python -m unittest tests.test_b10_runtime_probe tests.test_b11_world_change_runtime tests.test_b12_attack_evidence_runtime tests.test_b12a_fabric_runtime tests.test_b12a_runtime_injection_acceptance tests.test_b12b_partial_combat_runtime tests.test_b12b_runtime_injection_acceptance tests.test_fabric_deployment_probe -v
python scripts/export_motion_navigation_standalone.py check-java
python -m unittest tests.test_standalone_java_gates -v
python scripts/public_runtime_evidence.py verify --root evidence/motion_navigation/representative-v1
```

正式实机结论以 `docs/motion_navigation/acceptance/` 中的运行 ID、样本范围和限制为准。组件测试不能替代 Fabric 实机验收。
公开真实运行样本只用于核对冻结的历史批次。修复后的重放结果不能覆盖样本原有的失败分类。
