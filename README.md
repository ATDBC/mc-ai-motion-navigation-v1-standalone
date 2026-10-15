# Minecraft 机器人运动、导航与战斗执行底座

这个仓库是主项目按固定清单生成的源码快照。它包含当前正式实现、共享契约、配置、测试、Fabric 客户端代码和四类现行设计文档。`EXPORT-METADATA.json` 记录来源提交，`SHA256SUMS.txt` 覆盖导出的每个文件。

2026-10-15，D109 已完成 D0—D8 的已授权实现和 Windows 验收。600 秒 B10 完成 40／40 validation、40／40 turn validation、10／10 默认协调链、60／60 R4 和 24 个 typed negative，零伤害；ordered source 显式释放。异步 trace 写入 22409／22409 条，丢弃和不确定记录均为 0，worker 与 cleanup 正常结束。Windows 最终矩阵为主源码正逆各 186 个模块、2331 项，公开快照正逆各 185 个模块、2307 项；Java 门禁各 3／3。D109 整体仍按规则 **FAILED／STOPPED**：求解最大值 41.0339 ms 超过 30 ms 门槛，time attribution 以 `NoneType` 错误结束；Linux zombie、双 fork 和 POSIX 文件描述符矩阵仍待三方补充复核。连续两跳、零等待交接和新动作仍未授权。当前入口为 [D109](docs/motion_navigation/decisions/0109-retest-b10-budget-and-bound-lifecycle-cleanup.md)、[计划](docs/motion_navigation/stages/d109-b10-budget-and-lifecycle-repair-plan.md)、[验收](docs/motion_navigation/acceptance/d109-b10-budget-and-lifecycle-repair.md)和 `evidence/motion_navigation/d109-b10-lifecycle-v1/`。

2026-10-15，D108 的 A1—A5-R3 已完成，但 B10 不退步门槛未关闭。观察更新与不可变 section 物化已经分开；借用只处理相关范围。分片器能有界终止登记的进程树。B10 的六类 JSONL、Runtime trace 和 F-TICK 已移出控制 tick 的同步写盘路径；receiver 诊断默认关闭。现有协议测量“receiver 观察 → 注册发布 → 控制消费”，不能测 worker 实际完成时刻。A5-R3 让 receiver 去重集合只登记真正进入有界缓冲的记录，并把旧 A5 的含混 ready 字段标为 legacy。最近一次完整结果仍是 A5-R2：Windows 主源码正逆各 186／186 个模块、2307 项，本公开快照正逆各 185／185 个模块、2283 项。Linux 动态生命周期矩阵仍待三方复核。去掉同步文件 I/O 后，B10 仍在约 3200 个游戏 tick 后触发旧 `deadline_exceeded`，清理又覆盖原始错误并留下 ordered source；原始失败已保留，本阶段没有修改运动逻辑。当前入口为 [D108](docs/motion_navigation/decisions/0108-separate-world-invalidation-from-snapshot-materialization.md)、[计划](docs/motion_navigation/stages/d108-snapshot-and-bounded-verification-hardening-plan.md)、[验收](docs/motion_navigation/acceptance/d108-snapshot-and-bounded-verification-hardening.md)和 `evidence/motion_navigation/d108-snapshot-hardening-v1/`。

2026-10-14，D107 已完成。D103 留下的观察热路径和异步测试回归已经关闭：F1 缩短场景由约 182 ms/tick 降至 14.57 ms/tick；同语义观察只更新证据，不再重建约 3,400 格的不可变 section；几何变化仍立即重建并使旧依赖失效。计划登记的 11 个回归模块全部恢复。Windows 正序、逆序各运行 184／184 个独立模块、2264 项检查，零失败、错误、超时和跳过。D106 的 R1／R4 停止结论保持不变，现有 trace 不能证明 D103 性能回归导致 B10 跨 tick。当前入口为 [D107](docs/motion_navigation/decisions/0107-repair-d103-regressions-before-new-motion-work.md)、[阶段](docs/motion_navigation/stages/d103-regression-recovery-plan.md)、[验收](docs/motion_navigation/acceptance/d103-regression-recovery.md)和 `evidence/motion_navigation/d107-regression-recovery-v1/`。

2026-10-14，D106 已部分完成并停止。T1-R 去掉重复证明摘要后保持功能、安全和摘要一致，但 Windows M0 P99 为 16.5732 ms，仍未达到 15 ms，R1 **FAILED／STOPPED**，方案 B 未实施。R2 已把 G2 的调用计数与无 profile 性能计时拆开并通过。R3 已证明现有 UNKNOWN 与依赖复核足以让局部快照保守失败，没有修改生产代码。R4 的第二轮 Fabric 取得 40／40 `NEXT_MOVEMENT_TICK`，但随后旧 B10 移动入口夹具在一次观察跨两个 movement tick 时没有进入 `(0.5, 1.0)` 速度带，因此 R4 **FAILED／STOPPED**。该失败发生在动作求解前，身体全程安全。连续两跳、零等待交接、T2-3 和新动作仍未授权。现行入口为 [D106](docs/motion_navigation/decisions/0106-repair-d105-cost-measurement-and-tick-diagnostics.md)、[阶段](docs/motion_navigation/stages/d105-cost-measurement-and-tick-remediation-plan.md)、[验收](docs/motion_navigation/acceptance/d105-cost-measurement-and-tick-remediation.md)和 `evidence/motion_navigation/sound-motion-proof-d106-v1/`。

2026-10-13，D105 已按三条独立轨道止损。T1 的证明瘦身未达到 Windows 性能门槛；T2 的 receiver 精简通过，但局部快照阶段当时因门槛定义问题停止；T3 的首次 Fabric 批次只有 22 个完整时间线，随后 B10 中止。D105 的原始失败保持不变，由 D106 分别复核和修正量尺。见[架构](docs/motion_navigation/architecture/sound-sparse-motion-proof-v1.md)、[阶段](docs/motion_navigation/stages/sound-motion-proof-and-independent-tracks-plan.md)、[验收](docs/motion_navigation/acceptance/sound-motion-proof-and-independent-tracks.md)和 `evidence/motion_navigation/sound-motion-proof-d105-v1/`。

2026-10-13，D104 止于 K1。K0 的 18 个旧扫描样本可重复；关键边界候选在这些样本上减少了物理计算，但独立审查证明它可能漏掉两个采样点之间短暂出现的风险。最小反例中，旧扫描器得到风险区间 `(4,5,6)`，候选返回零风险并接纳。候选已从活动 D101 路径撤回；随后产生的性能数据只作诊断，不构成 K2。K2—K7、receiver／快照简化、F-TICK、连续两跳和 Fabric 均未运行。现行入口为 [D104](docs/motion_navigation/decisions/0104-slim-motion-proofs-before-continuity.md)、[阶段](docs/motion_navigation/stages/key-boundary-motion-proof-and-continuity-plan.md)、[验收](docs/motion_navigation/acceptance/key-boundary-motion-proof-and-continuity.md)和[失败索引](evidence/motion_navigation/key-boundary-motion-proof-d104-v1/failure-index.json)。

2026-10-13，D103 G2-F2 已按预登记止损。Task A 已统一执行窗口转换，修复 11／12 tick 窗口在量尺适配处丢失；Task B 已把观察请求容量编码移出控制路径，同时保留公共校验与真实 backend 编码。Task C 的真实 spawn 前置试验确认，legacy 第一段没有 registry handle 或登记安全尾迹，late delivery、fallback 和正常 closeout 仍会在控制线程现场运行 `verified_ground_rollout`。因此 Task C 没有实现，Task D、唯一正式 F2、Task 10、G3 和 Fabric 均未运行。G2 保持 **FAILED／STOPPED**，不能作为后续阶段的通过依据。现行入口为 [D103](docs/motion_navigation/decisions/0103-repair-g2-window-and-control-hot-path.md)、[G2-F2 计划及结果](docs/motion_navigation/stages/prepared-motion-proof-g2-f2-remediation-plan.md)和[D102 正式验收](docs/motion_navigation/acceptance/prepared-motion-proof-pipeline-d102.md)。

2026-10-11，D101 最小原型止于 M0。按时和整体晚一 tick 各 100／100 安全落地，400 次关键节点只比较，没有新增完整证明或安全违规；但完整决定 P99／最大值为 48.6079／57.5597 ms，超过 15／30 ms 门槛。M0 整体未通过，后续任务、正式链接入和 Fabric 均未授权。证据位于 `evidence/motion_navigation/continuous-motion-d101-v1/`。

D102 的公开证据只包含紧凑失败索引、D102／D103 前置试验和可独立运行的代码检查。`test_d102_evidence_contract.py` 依赖约 46 MiB 的本地 G0／G1 原始目录，按公开包不上传大型原始证据的规则不进入快照；缺少这些原始字节时不能声称重新核验了 G0／G1。公开树仍可核对 G2 的失败边界，但不能把失败指针当作完整正式运行。

2026-10-11，[D100](docs/motion_navigation/decisions/0100-use-risk-aware-continuous-deviation-recovery.md) 已冻结连续偏离恢复的设计。宽阔、已知且可停车的安全区域默认保持运动；位置、速度和落点出现偏差时，优先在运动中局部接回路线。后段失效但当前前段仍安全时，继续安全前段并在后台重规划；被外力推向坑边等危险时，先让短期运动远离危险；越过起跳、离边或下落承诺点后，由原 owner 负责安全落地，再从真实位置继续任务。偏离按当前风险和承诺阶段处理，不为每种动作组合或偏离原因增加专用状态机。D100 目前只冻结设计，尚未授权实现、正式链接入、P4 或新 Fabric；D098 的失败证据保持原样。架构说明见[按风险连续运动架构](docs/motion_navigation/architecture/risk-aware-continuous-motion-v1.md)。

2026-10-11，[D099](docs/motion_navigation/decisions/0099-adopt-game-first-risk-graded-short-open-loop-control.md) 已记录后续连续运动方向：利用 Minecraft 稳定、确定且可直接读取事件与事实的特点，采用“短开环输入包＋逐 tick 廉价监视＋事件触发重算＋关键节点严格检查”。普通安全场景不默认每 tick 完整重证；高危任务可以由后续上层策略拒绝、换路、先处理威胁或改变地形。D099 只是设计决定，尚未授权实现、P4、正式链接入或新 Fabric，D098 的失败证据保持原样。

2026-10-11，D098 的隔离实验已经停止于 P3。Windows 原样复测与三方 D097-A 的 1,423,766 条非计时样本逐项一致；项目自有控制器、冻结表、可信轨迹、增量伤害、主轨迹复用和 24-step 分摊已经完成。P3 滚动闭环最终运行 37 个场景各 3 轮，共 1,338 个完整决定；P50／P95／P99／最大值为 0.2521／15.8503／19.8099／20.0919 ms。所有 1,227 次后端推进均有 owner、实验账本和许可来源，但承诺后恢复为 0／27，并有 54 次身体责任未解决和 6 次伤害额度超出。因此 P3 未通过，P4 和正式链适配未授权；`mc2p` 没有修改，也没有新的 Fabric 结论。

当前先读 D108、对应计划与验收，再按需追溯 D107—D103 的历史止损。需要追溯连续运动设计时，再读 [D101](docs/motion_navigation/decisions/0101-clarify-short-open-loop-timing-and-lifecycle.md)、[D100](docs/motion_navigation/decisions/0100-use-risk-aware-continuous-deviation-recovery.md)、[按风险连续运动架构](docs/motion_navigation/architecture/risk-aware-continuous-motion-v1.md)、[D099](docs/motion_navigation/decisions/0099-adopt-game-first-risk-graded-short-open-loop-control.md)和 [D098](docs/motion_navigation/decisions/0098-validate-and-optimize-incremental-motion-proof-on-windows.md)。P1—P3 的历史隔离实验检查可以运行：

```text
python -m unittest tests.motion_nav.test_incremental_motion_controller tests.motion_nav.test_incremental_motion_table tests.motion_nav.test_incremental_motion_proof tests.motion_nav.test_incremental_motion_performance tests.motion_nav.test_incremental_motion_closed_loop tests.motion_nav.test_incremental_motion_closed_loop_measurement -q
```

2026-10-10，D097 的连续动作新路线在 M0 成本门槛停止。正式 Windows 探针保持 22 个旧正例、6 个负例和新增混合动作元反例的分类正确；混合反例三轮都固定使用 10,056 次物理计算，三轮正例最大耗时分别为 587.9058、591.1611 和 736.8842 ms，超过 400 ms 门槛。负例最大 744.7585 ms；单边界增量证明和已知输入 rollout 仍只是从完整扫描拆出的 2.7390／5.7393 ms 成本估算，不是执行许可。M0 未通过，M1—M4 未开始，`mc2p` 没有修改，也没有运行新的 Fabric 实机批次。

第三方可阅读 [D097](docs/motion_navigation/decisions/0097-use-controller-table-and-incremental-safety-proof.md)、[增量证明架构](docs/motion_navigation/architecture/incremental-motion-proof-v1.md)、[阶段方案](docs/motion_navigation/stages/continuous-motion-incremental-proof-plan.md)、[验收](docs/motion_navigation/acceptance/continuous-motion-incremental-proof.md)和 [M0 证据](evidence/motion_navigation/incremental-motion-m0-v1/README.md)。只读探针可从仓库根目录运行：

```text
python -m unittest tests.motion_nav.test_trajectory_proto_m0_probe -q
```

2026-10-10，TP 连续短轨迹原型已结束。TP-0 正式对照通过；Task 5A 未签署，`343d10de` 只是旧候选。D096 授权的 5A-R 中，R1 证明修正通过；R2 候选 `cfd36acd` 在旧矩阵 94／94 时仍把地面、起跳和空中控制绑定在一起，遗漏合法混合动作元。唯一限定修正让审查反例转绿，但 `turn_90`、`jump_up_after_turn`、`jump_up_after_turn_continue` 三项冻结正例都精确耗尽 65,536 次物理计算预算。R2 未通过，R3 未开始，P0 未通过，不进入 5B。失败修正只保留为 diff 和结构化证据，没有进入候选源码。正式 actor、Runtime 和脚本入口均不导入原型。

第三方可阅读[TP 阶段](docs/motion_navigation/stages/TP-continuous-short-trajectory-prototype-plan.md)、[5A-R 计划](docs/motion_navigation/stages/TP-task-5A-R-plan.md)、[验收](docs/motion_navigation/acceptance/TP-continuous-short-trajectory-prototype.md)、[D096](docs/motion_navigation/decisions/0096-repair-trajectory-proof-and-use-stable-motion-primitives.md)和[失败证据](evidence/motion_navigation/trajectory-proto-5ar-v1/README.md)，并从仓库根目录运行四个原型组件测试：

```text
python -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment tests.motion_nav.test_trajectory_proto_reference_search tests.motion_nav.test_trajectory_proto_primitive_matrix -q
```

2026-10-10，D094 的安全落地后补走已完成。末次严格输入失联但安全落地、且目标仍未满足时，正式链从当前观察重新规划并补走。六族 600 项恢复 122 项，旧成功退步 0；Windows 正序、逆序各 1705／1705；Fabric 四方向 4／4，四次严格空中命令均真实晚一 tick 生效并记录为 `applied_outside_window`，落地后保留 typed `INPUT_LOST`，零伤害、额外期限违规、来源泄漏和二次起跳。结果见[阶段](docs/motion_navigation/stages/post-input-loss-landing-recovery.md)、[验收](docs/motion_navigation/acceptance/post-input-loss-landing-recovery.md)和 `evidence/motion_navigation/post-input-loss-landing-v1/`。

默认分支 `main` 是项目方唯一的公开发布线，只接收固定导出脚本生成并校验通过的整理后快照。主项目的完整开发分支和提交历史不会直接推入本仓库。三方审查分支可以单独存在，用于保存审查报告和复现材料；它们不改变 `main` 的正式状态。

2026-10-09，D092 的动作入口交接 H0—H3 已完成。正式链 80 项从修复前 0／80 变为 80／80；本轮新增的 Walk 朝向准备分支读取下一段通用入口朝向，同帧地面输入按最终获胜视角计算。Windows 完整正序、逆序各 1670／1670；v7、F2 528、v8 的旧成功退步为 0。Fabric 四方向 normal／首条严格输入晚一 tick 共 8／8，四次晚到均在证明窗口内实际生效，起跳 yaw 误差 0°，零伤害、期限错过和来源泄漏。结果见[阶段](docs/motion_navigation/stages/action-entry-handoff-repair.md)、[验收](docs/motion_navigation/acceptance/action-entry-handoff-repair.md)和 `evidence/motion_navigation/action-entry-handoff-v1/`。

同日，按D090—D092完成F2-R主线恢复，R0—R4已签署。Windows源树和独立公开快照正序、逆序各1661／1661，零失败／错误／跳过；43组Fabric、46个实际试次全部通过，20次首条晚1由真实应用确认，旧成功退步0。区域几何40／64；柱顶24项在 R4 签署时仍按D091保留为安全有界的Walk→JumpUp能力RED，现已由上面的独立交接阶段关闭。修订8 ms目标与worker资源债未关闭。正式结果见 [F2REC阶段](docs/motion_navigation/stages/F2REC-rebuild-from-f2r.md)和[验收](docs/motion_navigation/acceptance/F2REC-rebuild-from-f2r.md)。

R2按D092同机222帧配对签署；修订8ms仍是性能债。R3已删除没有正式生产者的潜行防坠状态机，探边、Crouch和原版潜行物理保留。Runtime关闭后的Session worker资源债仍保留。原停止、夹具失败与历史能力结果不回写。当前入口为 [F2REC 阶段](docs/motion_navigation/stages/F2REC-rebuild-from-f2r.md)、[验收](docs/motion_navigation/acceptance/F2REC-rebuild-from-f2r.md)及 `evidence/motion_navigation/F2REC-recovery-v1/`。以下较早结果保留各自历史范围。

撤出的 D076—D088、F2-S 至 F2-RH 阶段与证据保留在公开历史提交 [2bad9bf](https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/tree/2bad9bf)。该提交保存归档材料，不代表当前 main 的正式能力。

2026-10-08，F2-R 已按冻结范围通过。外角、对角柱和杂乱地形中的完成区域，从原目标内的安全精确矩形中选择面积最大的一块；不使用外接矩形，不扩大原 `GoalState`。UNKNOWN 按整格最坏障碍裁剪，只允许完全已知的安全子区域。Windows 完整正逆各 1647/1647；v7 为 1998/2000，旧成功退步 0；v8 为 3817/3904，87 项失败全部保留。Fabric 两族四方向正常／首条晚1共 16/16，8 次晚1实际应用，零安全与期限违规，来源全部注销。

共享模拟器等价短路后，单次正式 D061 完整路径最大 40.1764 ms、deadline miss 为 0；原超限记录保留，夹具性能修复不记作机器人能力提升。`SNEAK_EDGE_GUARD` 仍只有组件证明，正式路线没有生产者。下一阶段为路线优化器，必须成为第一个正式区间生产者，并补必须潜行的实机证明；若更换阶段，先删除不可达状态机。现行范围与结果见 [F2-R 阶段](docs/motion_navigation/stages/F2R-piecewise-completion-and-v8.md)、[F2-R 验收](docs/motion_navigation/acceptance/F2R-piecewise-completion-and-v8.md)及 [D075](docs/motion_navigation/decisions/0075-repair-piecewise-completion-and-bound-edge-guard.md)，紧凑证据位于 `evidence/motion_navigation/F2R-piecewise-completion-v1/`。以下 F2 及更早记录保留各自的历史范围。

2026-10-07，F2 通用非中心地面路线执行与终点接近已按冻结范围验收通过。普通开阔路线保留快速路径，已知碰撞或支撑边界每帧最多复核三个候选；潜行只由路线区间授权，完成区域保持在原 `GoalState` 内。正式 Windows 完整正逆各 1627/1627，五组通过，产品 1998/2000，旧成功退步 0；玩家站位 187/400 → 400/400。Fabric 冻结清单 93/93 符合各自判定，80 正例完成、40 次首条晚一 tick 实际应用，三组路线／持续输入用时比为 1.000／1.235／1.074，连续 10 tick 非中性停滞为 0。首个静止普通 Walk 补一 tick 中性等价证明，命令租约与 Session／Runtime 生命周期未改。

范围见 [F2 阶段](docs/motion_navigation/stages/F2-non-center-ground-route-execution.md)、[架构](docs/motion_navigation/architecture/continuous-ground-route-execution-v1.md)和 [D074](docs/motion_navigation/decisions/0074-generalize-ground-route-execution-before-terminal-approach.md)，结果与限制见 [F2 验收](docs/motion_navigation/acceptance/F2-non-center-ground-route-execution.md)。紧凑证据位于 `evidence/motion_navigation/F2-ground-route-v1/final/`，原失败、逐项索引、来源和哈希均保留。外力仅按冻结试次判断；移动活塞观察、动态避障、路线优化器、疾跑路线和新动作尚未交付。

Windows 是正式开发、自动验收和 Fabric 实机平台；Linux 仅用于补充可移植性复核。公开快照不包含本地 `.tmp`、`artifacts`、凭据、世界存档或原始大型轨迹。F2 的详细轨迹比较和 Fabric 原始流审计仍需主项目保管的原始证据，紧凑摘要不能代替完整观察。`EXPORT-METADATA.json` 的来源提交标识本次公开整理；历史验收采集提交和当时的 dirty 状态仍按原记录保存。

历史 M0 通过，M1 结构止损，未通过；动作接口迁移保留，后续接口加固和 Driver 枚举生命周期整改已经完成。原 S1—S4 不再实施，M2—M5 未授权。原结果分别见 [M0／M1 验收](docs/motion_navigation/acceptance/motion-navigation-middle-layer-M0-M1.md)、[加固验收](docs/motion_navigation/acceptance/action-spec-hardening-driver-lifecycle.md)和 [D073](docs/motion_navigation/decisions/0073-accept-action-spec-and-retire-size-gates.md)，历史失败不回写。

历史 F1 按“功能通过、结构未通过”保存；R28 按“时间盒结束，部分交付通过”保存。历史 S0-R、v1—v7、R27 和连续高度 M3 的结果与边界均保留在各自验收及 `evidence/motion_navigation/` 中，不用 F2 的新结果覆盖。历史入口见 [F1 验收](docs/motion_navigation/acceptance/F1-known-world-following.md)、[R28 验收](docs/motion_navigation/acceptance/navigation-coordination-convergence.md)和 [S0-R 验收](docs/motion_navigation/acceptance/post-F1-navigation-structure-cleanup.md)。

当前已完成 B01 至 B10、C1-A 至 C1-C、C1-R 的 R0 至 R6、B11 固定放置与有限搭桥，以及 B12-A、B12-B。B12-A 固定了每次攻击的证据和分类重试，并接入 Minecraft 1.21 伤害来源事实。B12-B 让战斗先确定本帧视角，导航再按最终视角计算普通地面移动；追逐阶段会持续给出目标视角，导航同时记录每帧的移动决定原因。活动目标和真实墙体场景补齐了持续瞄准、遮挡后的导航、补看和权限撤销。动作证明的生效窗口修正和客户端额外推进一个 tick 时的局部安全恢复也已包含。正式 Fabric 结果与适用边界写在 `docs/motion_navigation/acceptance/`。

导航协调的 S0 至 S5 已实施。2026-10-01，R27 复审整改 A0—A5 完成，S5 按本轮冻结范围重新关闭。规划、motion 和放置复用同一生命周期入口；业务终态后仍可请求身体停止；旧信息通知自行携带身份；四个模拟工具共同拒绝缺证据。完整运动导航 847/847，原 1000 序列及独立新增 256 个异步组合符合各自冻结检查，8 个旧错误副本均被行为断言发现。完整父层 B11 实机停止及正常对照 20/20、原正例 60/60、反例 24/24。当前分组、失败记录和实机边界写在协调验收第 20 节，原检查和独立复审保留在第 17、18 节。

`evidence/motion_navigation/r27-v1/` 提供本轮结构化汇总，以及一份完整 B11 通过归档和一份尾迹超时失败归档，两份压缩数据合计约 220 KiB。原有 `representative-v1` 归档保持不变。新归档保留试次、控制帧、实际应用采样、释放依据及尾迹；测试注入和源码冻结边界分别说明，便于外部核对。

连续高度 M3 已关闭。完整 Fabric 矩阵覆盖 800 个唯一试次：正常输入 400/400 完成；20% 随机晚一帧为 384/400 完成，另外 16 场均按冻结口径有界失败；伤害额度超出和不变量违规均为 0。半砖、楼梯、土径、地毯和不超过半格的积雪统一作为普通步行候选，再由同一个 1.21 运动计算器验证真实高度、净空和扫掠结果。

2026-09-26 起，正式 Fabric 方块视觉只接受 profile 4 表面深度。旧 profile 3 稀疏射线只能读取历史记录，不能启动正式会话，也不能形成新的验收结论。`evidence/motion_navigation/representative-v1` 保留九份带有 `historical_legacy_ray_profile3` 标签的旧归档，并加入一份 `current_surface_depth_profile4` 的 B12-B 当前代表批次。具体边界见 `docs/motion_navigation/decisions/0035-formal-surface-depth-only.md`。

主仓库仍保留 CraftGround、`legacy_ray_profile3` 和旧轨迹读取代码，以便复现历史结果。它们的采集器、运行入口、导航审计和测试不会进入这个当前实现仓库。V3 仍复用少量早期版本中已经冻结的通用数据类型和 JSON 校验函数；正式后端随后强制检查 profile 4，不能因此启动旧射线。正式 Fabric 构建门禁还会检查旧采集器没有进入客户端 JAR。

## 先读什么

1. `AGENTS.md`
2. [D108 计划](docs/motion_navigation/stages/d108-snapshot-and-bounded-verification-hardening-plan.md)
3. [D108 验收](docs/motion_navigation/acceptance/d108-snapshot-and-bounded-verification-hardening.md)和 [D108 决定](docs/motion_navigation/decisions/0108-separate-world-invalidation-from-snapshot-materialization.md)
4. 需要追溯产品能力时再读 F2REC、连续地面路线和对应历史验收。
5. `docs/motion_navigation/architecture/navigation-coordination-v1.md`（第 17 节包含当前 R28 目标和已实施的持续目标语义）
6. 当前实现和证据读 R27 阶段文档、`navigation-coordination-refactor.md` 第 20 节与 `defect-ledger.md`。
7. 连续高度与物理范围读 M3、B09-R、B10 的对应 architecture 和 acceptance。
8. 需要检查战斗或部分观察时，再读对应的 B12、C1 文档。

## 环境

- Python 3.11
- NumPy 2.4.6
- OpenJDK 21
- Minecraft 1.21
- Fabric Loader 0.15.11
- Fabric API 0.100.6+1.21

创建 Python 3.11 虚拟环境后安装：

```text
python -m pip install -r requirements.txt
```

Java 可以由 `JAVA_HOME` 指向 JDK 21，也可以把 JDK 21 的 `java` 与 `javac` 放入 `PATH`。检查命令不会依赖原项目的 `.venv/Library` 路径：

```text
python scripts/export_motion_navigation_standalone.py check-java
```

正式 Fabric 客户端还需要 CMake、C++ 构建器和 JDK 21。下面的命令会先从仓库内固定源码构建表面深度原生库，执行正式 JNI 冒烟检查，再离线构建 Fabric 模组。当前阶段只验收 Windows。离线 Fabric 构建还要求仓库根目录已经有固定的 Gradle 8.8 与 Minecraft 依赖缓存；公开快照不上传这部分缓存。没有缓存时仍可运行下面的源码、Python 和纯 Java 检查，但不能据此声称已经重建 Fabric 模组。

```text
python scripts/build_fabric_deployment_probe.py
```

## 可重复检查

先验证文件集合和哈希：

```text
python scripts/export_motion_navigation_standalone.py verify --root .
```

运行动作、几何、规划与执行检查：

```text
python -m unittest discover -s tests/motion_nav -p "test_*.py" -v
```

快速检查 F2 的路线、潜行区间、原目标完成区域和正式启动窗口：

```text
python -m unittest tests.motion_nav.test_f2_ground_route_gates tests.motion_nav.test_f2_non_center_ground_route tests.motion_nav.test_f2rec_r3_cleanup tests.motion_nav.test_f2_ground_completion_region tests.motion_nav.test_f2_ground_start_window -q
```

重新生成 F2 的组件与玩家站位量尺时，必须使用新的空输出目录：

```text
python scripts/f2_ground_route_evidence.py --output output/f2-ground-route --workers 4
```

F2 的完整历史命令与采集环境保存在 `final/README.md`。其中 `reproduce/run_windows_checks.py` 沿用主项目 `.venv/python.exe` 路径；公开环境可按上述命令直接运行源码检查。历史详细对比与实机审计需要未公开的原始流，不属于仅凭公开快照可重跑的检查。

快速检查 D055 的真实准备、后台复核、启动交付及首条晚到：

```text
python -m unittest tests.motion_nav.test_motion_start_delivery tests.motion_nav.test_action_continuity_formal tests.motion_nav.test_async_gap_revalidation_delivery -q
```

v6 原始归档的完整对比与 Fabric 命令维护在验收第 14.7 节。Fabric 仍需本地固定依赖和独立测试服务器，公开快照不含游戏世界或启动凭据。

重新生成连续高度正式调用链组件矩阵和规划代价对照时，必须使用新的空输出目录：

```text
python scripts/run_continuous_height_matrix.py --output output/continuous-height-matrix --condition both --scope all
python scripts/run_continuous_height_planning_matrix.py --output output/continuous-height-planning-matrix
```

运行固定的导航随机事件序列时，也必须使用新的空输出目录：

```text
python -m tests.sim.run_navigation_event_sequences --manifest tests/sim/manifests/navigation-coordination-event-sequences.json --output-root output/navigation-event-sequences
```

运行 R27 新增的异步组合和验证门禁检查：

```text
python -m unittest tests.motion_nav.test_async_work_verification -v
python -m tests.sim.run_async_work_sequences --manifest tests/sim/manifests/navigation-async-work-r27-hardening.json --output-root output/navigation-async-work-sequences
python -m tests.sim.run_async_admission_mutations --output output/navigation-async-admission-mutations
```

任务结果和证据结论分别报告。任务已经完成但缺少应有异步记录时，仍不得通过验收；没有异步工作的场景必须预先声明，并由实际调用记录核对。

运行 C1 正式证据读取和离线重放检查：

```text
python -m unittest tests.test_c1_melee_evidence tests.test_c1_moving_melee_evidence tests.test_c1_external_motion_evidence tests.test_c1_fixed_melee_runtime tests.test_c1_moving_melee_runtime tests.test_c1_external_motion_runtime -v
```

运行公共控制帧、仲裁、失败处置、交战记忆和战斗驱动检查：

```text
python -m unittest tests.test_action_arbiter_v1 tests.test_action_receipt tests.test_player_runtime_v1 tests.test_runtime_failure_disposition tests.test_engagement_memory tests.test_fixed_melee tests.test_fixed_melee_driver tests.test_melee_strike_driver tests.test_moving_melee tests.test_moving_melee_driver tests.test_external_motion_recovery_driver tests.test_c1_navigation_session -v
```

运行 B10、B11、B12 的正式探针契约与部署入口检查：

```text
python -m unittest tests.test_b10_runtime_probe tests.test_b11_world_change_runtime tests.test_b12_attack_evidence_runtime tests.test_b12a_fabric_runtime tests.test_b12a_runtime_injection_acceptance tests.test_b12b_partial_combat_runtime tests.test_b12b_runtime_injection_acceptance tests.test_fabric_deployment_probe -v
```

运行不依赖 Minecraft 类库的 Java 控制门禁：

```text
python -m unittest tests.test_standalone_java_gates -v
```

不依赖 Minecraft／Gradle 缓存的表面 Java、协议和空气候选门禁：

```text
python -m unittest tests.test_surface_depth_tiles tests.test_surface_observation_v3 tests.test_visual_air_runtime -v
```

当前阶段的原生表面核心只在 Windows 验收。先构建固定源码，再运行容量剔除、遮挡和 500 个随机世界空气零误判检查：

```text
python -c "from scripts.surface_depth_probe.build import build_native; print(build_native())"
python -m unittest tests.test_surface_depth_cache -v
```

`tests.test_client_block_observation_v3` 会编译接入 Minecraft 1.21 类型的观察类，因此和完整 Fabric 构建一样，需要仓库根目录已有固定 Gradle 与 Minecraft 依赖缓存；具备缓存时把它作为正式门禁一并运行。缺少缓存时可以运行上面的源码、协议和原生核心检查，但不能声称已经重建 Fabric 观察入口。

这些检查直接覆盖当前 profile 4 实现。旧射线采集器和退役诊断入口不在公开清单中；公开包中的第一方 Python 模块还会由 `tests/motion_nav/test_standalone_export.py` 逐个导入，避免留下缺少依赖的失效入口。

核对十个代表性真实运行批次：

```text
python scripts/public_runtime_evidence.py verify --root evidence/motion_navigation/representative-v1
```

前六个批次分别覆盖 B10-C 跨隙、C1-B 移动近战和 C1-C 外力恢复，每类各有一个完整通过批次和一个完整失败批次。第七至第九个批次分别保存 B11、B12-A 和旧 B12-B 的历史结果。第十个批次是当前 profile 4 的 B12-B 完整通过记录，保留 34 个场景、两种活动目标试次、导航决定原因、真实墙体遮挡、控制事件和分段 Runtime 轨迹。验证器会检查归档哈希、安全边界、视觉版本、完整试次数量、持续追击指标、汇总、时延门槛和失败分类。压缩归档合计不超过 30 MiB。

## 能说明什么

- 已知地形中的地面路线、支撑面、台阶、有限空中动作和动作接续有组件与实机证据；
- Runtime 是唯一逐帧推进者，导航、视角和攻击通过同一控制帧仲裁；
- 状态锚点、真实输入应用账本、后台求解、候选接纳和逐 tick 执行使用同一身份链；
- 带速度的一格同高跨隙已在三个入口速度带和四个正方向完成冻结验收；
- C1 战斗纵切片已覆盖固定目标、移动目标和真实受击后的恢复；
- B11 已覆盖单块放置、一至三格直桥和十二类边界与反例；
- B12-A 已覆盖单次攻击证据、分类重试和伤害来源，B12-B 已覆盖按最终战斗视角计算的普通地面移动、活动目标持续瞄准和部分观察边界；
- 导航协调层的身体责任、前置条件、重试与伤害额度已经有正式路径闭环门禁；
- 连续高度组件矩阵已覆盖已声明的小高差与整格动作，并保留随机迟到造成的有界安全失败；
- 公开仓库附带十个结构化真实运行批次，可以重新统计历史结果，并核对 B12-B 当前 profile 4 结果。

## 不能说明什么

这个快照不包含世界存档、完整普通日志、画面、Gradle 缓存、Minecraft 依赖 JAR 或构建产物，因此不能只靠本仓库重跑游戏内正式实验。公开的十个真实批次是经过筛选的结构化轨迹，可以核对历史结果、当前 B12-B 结果和读取链，但不能替代新的 Fabric 实机运行。其他原始证据仍由主项目保管。

未知区域探索、攀爬、游泳、主动 Crawl、特殊地面、任意宽度跨隙和尚未声明的动作接续仍未完成。当前独立仓库只包含正式 Fabric 路径；CraftGround 适配和旧射线兼容代码只在主仓库保留。

公开快照能够复现其声明的源码检查，不代表项目已经没有剩余问题。
