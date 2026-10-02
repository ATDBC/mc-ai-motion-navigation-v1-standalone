# AGENTS.md

2026-10-02：D052 的验证期限、后台重验证与连续入口已实施。限定跨隙四方向实机 4/4，运动导航 914/914；真实条件不匹配仍不能起跳，未获选与取消保留身体责任。当前结果见 [R28 验收第 12.6 节](docs/motion_navigation/acceptance/navigation-coordination-convergence.md#126-本轮实施与限定试点结果)。完整升降组合、长期控制期限、R28 共享协调及跟随未关闭，旧基线和失败保持原记录。

本仓库是由主项目生成的只读源码快照。先读最新的 `docs/motion_navigation/stages/` 和对应的 `acceptance/`，再读取相关 `architecture/` 与 `decisions/`。

D051 的进入修正已移除正式筛选和失败改名，保留末段直连及证据工具；886 项检查通过。v4 原八层为 1,589/1,600，玩家站位为 114/400；比 v3 少完成三场，非退步比较未通过。800 个严格任务逐项不变，代表性 Fabric 绕墙 4/4。旧 R28-C-01 注入混用两份地形，修正后的 12 个变体有界退出但不代表恢复成功。R28-C-02、共享协调、终点接触和跟随尚未关闭；结果见验收第 11 节，原 v1—v3 保留。

历史 v3 结果与失败保存在[R28 验收第 10.7 节](docs/motion_navigation/acceptance/navigation-coordination-convergence.md#107-批次-5-实现与-v3-结果)，不作为筛选可用的结论。原四个未覆盖函数已有正式路径场景，16 组历史扰动交付差异单列。R28-1、持续预算、跟随和完整路线平滑尚未实施。现行 R27 和 M3 结论保持原范围。

当前导航协调 S0 至 S5 已实施。2026-10-01，R27 复审整改 A0—A5 完成，S5 按本轮冻结范围重新关闭。停止请求保留业务终态和身体责任，信息通知携带完整身份，四个模拟关闭入口执行共同证据检查；测试走 Runtime 和正式 driver，不替实现生成身份。当前范围见 [R27](docs/motion_navigation/stages/navigation-async-work-r27-root-fix-plan.md)，结果和限制见[验收第 20 节](docs/motion_navigation/acceptance/navigation-coordination-refactor.md#20-r27-复审整改结果)与[缺陷台账](docs/motion_navigation/acceptance/defect-ledger.md)。连续高度 M3 的历史 Fabric 结论保留。

项目方只通过默认分支 `main` 发布由固定导出清单生成、校验通过的公开快照。不得把主项目的完整开发分支或提交历史直接推入本仓库。三方审查分支可以保留，但只保存审查者自己的报告和复现材料，不代表正式发布状态。

## 工程边界

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
python -m unittest tests.test_c1_melee_evidence tests.test_c1_moving_melee_evidence tests.test_c1_external_motion_evidence tests.test_c1_fixed_melee_runtime tests.test_c1_moving_melee_runtime tests.test_c1_external_motion_runtime -v
python -m unittest tests.test_action_arbiter_v1 tests.test_action_receipt tests.test_player_runtime_v1 tests.test_runtime_failure_disposition tests.test_engagement_memory tests.test_fixed_melee tests.test_fixed_melee_driver tests.test_melee_strike_driver tests.test_moving_melee tests.test_moving_melee_driver tests.test_external_motion_recovery_driver tests.test_c1_navigation_session -v
python -m unittest tests.test_b10_runtime_probe tests.test_b11_world_change_runtime tests.test_b12_attack_evidence_runtime tests.test_b12a_fabric_runtime tests.test_b12a_runtime_injection_acceptance tests.test_b12b_partial_combat_runtime tests.test_b12b_runtime_injection_acceptance tests.test_fabric_deployment_probe -v
python scripts/export_motion_navigation_standalone.py check-java
python -m unittest tests.test_standalone_java_gates -v
python scripts/public_runtime_evidence.py verify --root evidence/motion_navigation/representative-v1
```

正式实机结论以 `docs/motion_navigation/acceptance/` 中的运行 ID、样本范围和限制为准。组件测试不能替代 Fabric 实机验收。
公开真实运行样本只用于核对冻结的历史批次。修复后的重放结果不能覆盖样本原有的失败分类。
