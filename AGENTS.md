# AGENTS.md

2026-10-09，公开实现从 F2-R 按 [D090](docs/motion_navigation/decisions/0090-rebuild-main-from-f2r-and-recover-only-validated-parts.md) 恢复到已签署的 R1。R0 基线和 R1 完成区域几何已签署；R1 的 Windows 完整正序、逆序各 1661/1661，失败、错误、跳过均为 0。区域几何正式链完成 40/64；柱顶 `column_top` 24 项仍为 Walk→JumpUp 交接 RED，保持有界结束、零伤害、零安全违规及来源释放，尚未交付柱顶能力。范围调整见 [D091](docs/motion_navigation/decisions/0091-sign-region-geometry-and-defer-column-top-handoff.md)。

R2 最小选面候选没有冻结旧成功退步，纯选面 P95 为 1.1873 ms；正式杂乱目标修订＋准备 P95 为 8.8542 ms，仍超过 8 ms。两轮限定优化后已止损，R2 未签署。公开 main 的生产代码保持已签署 R1，未签署的 R2 候选代码只保存在主项目本地归档，不进入公开快照。R3、R4 未开始，本次公开整理不代表恢复全阶段已经通过。当前入口为 [F2REC 阶段](docs/motion_navigation/stages/F2REC-rebuild-from-f2r.md)、[验收](docs/motion_navigation/acceptance/F2REC-rebuild-from-f2r.md)及 `evidence/motion_navigation/F2REC-recovery-v1/`。以下较早结果保留各自历史范围。

2026-10-07，F2 通用非中心地面路线执行与终点接近已按冻结范围验收通过。普通开阔路线保留快速路径，已知碰撞或支撑边界每帧最多复核三个候选；潜行只由路线区间授权，完成区域保持在原 `GoalState` 内。正式 Windows 完整正逆各 1627/1627，五组通过，产品 1998/2000，旧成功退步 0；玩家站位 187/400 → 400/400。Fabric 冻结清单 93/93 符合各自判定，80 正例完成、40 次首条晚一 tick 实际应用，三组路线／持续输入用时比为 1.000／1.235／1.074，连续 10 tick 非中性停滞为 0。首个静止普通 Walk 补一 tick 中性等价证明，命令租约与 Session／Runtime 生命周期未改。

范围见 [F2 阶段](docs/motion_navigation/stages/F2-non-center-ground-route-execution.md)、[架构](docs/motion_navigation/architecture/continuous-ground-route-execution-v1.md)和 [D074](docs/motion_navigation/decisions/0074-generalize-ground-route-execution-before-terminal-approach.md)，结果与限制见 [F2 验收](docs/motion_navigation/acceptance/F2-non-center-ground-route-execution.md)。紧凑证据位于 `evidence/motion_navigation/F2-ground-route-v1/final/`，原失败、逐项索引、来源和哈希均保留。外力仅按冻结试次判断；移动活塞观察、动态避障、路线优化器、疾跑路线和新动作尚未交付。

Windows 是正式开发、自动验收和 Fabric 实机平台；Linux 仅用于补充可移植性复核。公开快照不包含本地 `.tmp`、`artifacts`、凭据、世界存档或原始大型轨迹。F2 的详细轨迹比较和 Fabric 原始流审计仍需主项目保管的原始证据，紧凑摘要不能代替完整观察。`EXPORT-METADATA.json` 的来源提交标识本次公开整理；历史验收采集提交和当时的 dirty 状态仍按原记录保存。

历史 M0 通过，M1 结构止损，未通过；动作接口迁移保留，后续接口加固和 Driver 枚举生命周期整改已经完成。原 S1—S4 不再实施，M2—M5 未授权。原结果分别见 [M0／M1 验收](docs/motion_navigation/acceptance/motion-navigation-middle-layer-M0-M1.md)、[加固验收](docs/motion_navigation/acceptance/action-spec-hardening-driver-lifecycle.md)和 [D073](docs/motion_navigation/decisions/0073-accept-action-spec-and-retire-size-gates.md)，历史失败不回写。

历史 F1 按“功能通过、结构未通过”保存；R28 按“时间盒结束，部分交付通过”保存。历史 S0-R、v1—v7、R27 和连续高度 M3 的结果与边界均保留在各自验收及 `evidence/motion_navigation/` 中，不用 F2 的新结果覆盖。历史入口见 [F1 验收](docs/motion_navigation/acceptance/F1-known-world-following.md)、[R28 验收](docs/motion_navigation/acceptance/navigation-coordination-convergence.md)和 [S0-R 验收](docs/motion_navigation/acceptance/post-F1-navigation-structure-cleanup.md)。

本仓库是主项目生成的源码快照。先读 F2REC 阶段和对应验收，再按任务读取相关架构与决定。

项目方只通过默认分支 `main` 发布由固定导出清单生成、校验通过的公开快照。不得把主项目的完整开发分支或提交历史直接推入本仓库。三方审查分支可以保留，但只保存审查者自己的报告和复现材料，不代表正式发布状态。

## 工程边界

- 正式开发、自动验收和 Fabric 实机环境是 Windows。除非后续决定明确要求支持 Linux，否则 Linux 结果不阻塞阶段关闭。
- 正式机器人只使用结构化观察。未知空间不能当作空气。
- 只有 Runtime 仲裁后的唯一输入出口可以写玩家控制。
- 动作发出不等于成功，必须读取客户端实际应用回执和后续观察。
- 每类长期状态只有一个拥有者。不要复制地图、目标、路线、动作许可或输入账本。
- 不为尚未进入当前阶段的能力建立空框架。
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
python -m unittest tests.motion_nav.test_f2_ground_route_gates tests.motion_nav.test_f2_non_center_ground_route tests.motion_nav.test_f2_ground_route_edge_guard tests.motion_nav.test_f2_ground_completion_region tests.motion_nav.test_f2_ground_start_window -q
python -m unittest tests.test_c1_melee_evidence tests.test_c1_moving_melee_evidence tests.test_c1_external_motion_evidence tests.test_c1_fixed_melee_runtime tests.test_c1_moving_melee_runtime tests.test_c1_external_motion_runtime -v
python -m unittest tests.test_action_arbiter_v1 tests.test_action_receipt tests.test_player_runtime_v1 tests.test_runtime_failure_disposition tests.test_engagement_memory tests.test_fixed_melee tests.test_fixed_melee_driver tests.test_melee_strike_driver tests.test_moving_melee tests.test_moving_melee_driver tests.test_external_motion_recovery_driver tests.test_c1_navigation_session -v
python -m unittest tests.test_b10_runtime_probe tests.test_b11_world_change_runtime tests.test_b12_attack_evidence_runtime tests.test_b12a_fabric_runtime tests.test_b12a_runtime_injection_acceptance tests.test_b12b_partial_combat_runtime tests.test_b12b_runtime_injection_acceptance tests.test_fabric_deployment_probe -v
python scripts/export_motion_navigation_standalone.py check-java
python -m unittest tests.test_standalone_java_gates -v
python scripts/public_runtime_evidence.py verify --root evidence/motion_navigation/representative-v1
```

正式实机结论以 `docs/motion_navigation/acceptance/` 中的运行 ID、样本范围和限制为准。组件测试不能替代 Fabric 实机验收。
公开真实运行样本只用于核对冻结的历史批次。修复后的重放结果不能覆盖样本原有的失败分类。
