# R28-1 剩余协调迁移实施计划

> 实施时按任务顺序推进。每个任务先补失败检查，再修改生产代码；专项稳定后才扩大到完整矩阵。

**状态：已完成。** A—E 五项迁移、独立复审、完整模拟对照和受影响 Fabric 均已通过。结果见本文第 10 节和[验收第 17 节](../acceptance/navigation-coordination-convergence.md#17-r28-1-剩余协调迁移完成)。

**目标：** 删除 Session 中剩余的停止后去向副本，把探边、路线和规划失败统一接入现有交接协调器，并让 `propose()` 只保留调用顺序、事件路由和报告组装。

**依据：** [R28 总计划](navigation-coordination-convergence-r28-plan.md)、[协调架构第 17 节](../architecture/navigation-coordination-v1.md#17-r28-协调收敛目标)、[验收第 16 节](../acceptance/navigation-coordination-convergence.md#16-r28-1-共同恢复前两个切片)。

## 1. 共同约束

- 不新增生命周期状态、流程布尔或按 `reason` 字符串选择流程的分支。
- 任务结果、身体责任和已经派发的效果继续分别保存。
- `ExecutionSupervisor` 继续判断身体能否交接；交接协调器只决定安全退场后去哪里。
- 正常目标修订、连续接管和仲裁失选不消费恢复额度。真正恢复仍按稳定事件身份只收费一次。
- 只有 Runtime 实际选中的输入才能完成身体接管。路线准备好、动作求解完成和取得观察许可都不等于接管。
- 空中动作、在途输入、风险结算、落地与坑边退回保持现有安全证明。
- D054／D055 的后台延迟、启动余量和 v7 确定性交付模型不在本轮修改。
- 本轮不做终点贴墙、正式跟随、长期速率预算、异步世代简化或动作质量优化。

## 2. 最容易出错的五类组合

1. 旧路线在空中，目标再次修订：旧控制者必须落地，最后一个已接受目标随后从当前身体重锚。
2. 替代规划已经失败，停止期间又收到新目标或取消：旧目标的否定结果不能结束新目标，正式结束请求不能被覆盖。
3. 探边自然完成或超时，同时存在暂停路线、候选路线或结束请求：每项身体责任都要安全退出，去向只能决定一次。
4. 候选首条输入失选或晚到：旧路线不能因为候选已准备好而提前退役。
5. 信息查询重复返回、部分可见或超出范围：不能刷新其他事实的期限，也不能把缺信息改成空气或通行许可。

## 3. 任务 A：用有类型交接请求删除重启布尔

**生产文件：**

- `mc2p/motion_nav/navigation_handoff.py`
- `mc2p/motion_nav/navigation_session.py`
- 必要时只调整 `mc2p/motion_nav/planning_coordinator.py` 的重锚入口

**测试文件：**

- `tests/motion_nav/test_navigation_handoff.py`
- `tests/motion_nav/test_r28_shared_recovery.py`
- `tests/motion_nav/test_navigation_supervised_interruptions.py`

- [x] 先补正式链检查：空中重复修订、探边停止期间修订、当前支撑缺事实、错误世界或旧观察的释放证据，以及正常修订不增加恢复次数。
- [x] 扩展现有 `NavigationStopRequest`／`NavigationHandoffResolution`，明确释放后的重锚目的和是否已经获得恢复许可。
- [x] 把 `_stage_goal_revision_for_body_release`、`_wait_for_active_terminal`、替代路线安全退出的去向写入 `NavigationHandoffCoordinator`。
- [x] 删除 `_restart_after_active_terminal` 的字段、四处写入和独立消费分支；动作协调器只读取当前有类型交接请求。
- [x] 保留规划协调器创建新请求的职责；交接协调器不能直接寻路或读取第二份世界。
- [x] 运行任务 A 专项，确认停止后确实产生新的规划请求和 worker 提交。

任务 A 完成于 `c7822a5` 和 `e0393fb`。首轮审查发现免费重锚抢先吞掉输入失联，以及已收费恢复在目标修订后丢失实际许可身份；修复后定向 14/14、任务 A 专项 127/127，复审通过。

## 4. 任务 B：迁移替代规划失败

**生产文件：**

- `mc2p/motion_nav/navigation_handoff.py`
- `mc2p/motion_nav/navigation_session.py`
- `mc2p/motion_nav/planning_coordinator.py`
- `mc2p/motion_nav/navigation_owners.py`

**测试文件：**

- `tests/motion_nav/test_planning_coordinator.py`
- `tests/motion_nav/test_navigation_supervised_interruptions.py`
- `tests/motion_nav/test_r28_shared_recovery.py`

- [x] 冻结四种现行结果：空中替代规划失败、失败后修订、失败后取消，以及失败与旧路线终端发生在同一观察。
- [x] 让规划协调器只产生带请求身份的失败事实；让交接协调器保存尚未发布的停止后去向。
- [x] 新目标可以替换旧目标尚未发布的否定事实；已经接受的取消、关闭或正式失败不能被覆盖。
- [x] 删除 `PlanningPipelineState.replacement_failure`、`ReplacementPlanningFailure` 及 preserve／consume 接口。
- [x] 验证重复通知不重复扣账，也不会把旧请求结果用于新请求。

任务 B 完成于 `8e85d81` 和 `cadefa2`。首轮审查发现相同请求下的旧 attempt 失败仍能结束当前任务；修复后只接纳 PlanningCoordinator 已退役的当前失败事实，定向 32/32、任务 A/B 直接套件 141/141，复审通过。

## 5. 任务 C：统一探边自然完成、停止和超时

**生产文件：**

- `mc2p/motion_nav/navigation_handoff.py`
- `mc2p/motion_nav/navigation_session.py`
- `mc2p/motion_nav/execution_supervisor.py`
- `mc2p/motion_nav/probe_body_controller.py`
- `mc2p/motion_nav/navigation_owners.py`
- 只有缺少领域结果时才修改 `mc2p/motion_nav/landing_edge_probe.py`

**测试文件：**

- `tests/motion_nav/test_navigation_supervised_interruptions.py`
- `tests/motion_nav/test_navigation_route_handoff.py`

- [x] 分别覆盖探边自然完成、取得许可、超时、容量耗尽、取消、失败和目标修订。
- [x] Probe/controller 只报告完成、超时或停止事实；交接协调器决定重规划、等待信息或结束。
- [x] 保留 `TRANSFERABLE` 连续接管：探边完成后不强制先停稳再起步。
- [x] 删除 Session 中探边安全退场后的六路去向树和 `_probe_mode_exit_pending` 状态。
- [x] 锚点暂缺时继续保留身体；长期无法确认时沿用现行有界 `recovery_unresolved`，不能伪造 `QUIESCENT`。

任务 C 完成于 `2373499` 和 `8470bf0`。首轮审查发现 Runtime 失败旁路会释放输入源却留下仍 owned 的 probe；修复后当前 Q 会先由监督者退役 probe、清理等待并重新核验剩余路线，新增正式链 3/3、任务 A—C 直接套件 163/163，复审通过。

## 6. 任务 D：把单帧身体推进交给 RouteControl 和监督者

**生产文件：**

- `mc2p/motion_nav/route_body_controller.py`
- `mc2p/motion_nav/execution_supervisor.py`
- `mc2p/motion_nav/navigation_session.py`

**测试文件：**

- `tests/motion_nav/test_navigation_route_handoff.py`
- `tests/motion_nav/test_runtime_navigation_verified_handoff.py`
- `tests/motion_nav/test_action_continuity_formal.py`

- [x] 让 `RouteControl` 统一推进 executor 或 motion coordinator，并返回本帧决定和风险所需事实。
- [x] 让监督者处理 incumbent、pending route 与 probe 的单帧选择和接管证据。
- [x] Session 只传入当前 frame、anchor、输入账本和已获胜视角，再消费一个身体决定。
- [x] 删除 `propose()` 中分别推进候选路线和旧路线的重复分支。
- [x] 验证候选失选、晚到或入口变化时旧路线仍承担合法前缀，直到真实接管或安全释放。

任务 D 已由提交 `cfba119c` 实施，并由 `b9df8404` 修正风险拒绝后的重复推进旁路。独立复审确认 Session 已无 executor／coordinator 的逐帧 `decide()` 调用；候选拒绝先保留原重规划顺序，停止保护不重复轮询、推进动作索引或消费下一帧状态。Task D 直接检查 92／92，A—C 回归 164／164，执行器回归 49／49。完整矩阵留到任务 E 后统一运行。

## 7. 任务 E：拆出信息推进和规划结果消费

**生产文件：**

- `mc2p/motion_nav/navigation_owners.py`
- `mc2p/motion_nav/planning_coordinator.py`
- `mc2p/motion_nav/navigation_session.py`

**测试文件：**

- `tests/motion_nav/test_navigation_session.py`
- `tests/motion_nav/test_b11_world_change_navigation.py`
- `tests/motion_nav/test_goal_reach_policy.py`

- [x] 让现有信息 owner 推进查询分类、观察视角和取得事实；不在本轮修改等待预算。
- [x] 让规划协调器继续产生 `RUNNING／NEEDS_INFORMATION／REQUIRES_INTERACTION／FAILED／ROUTE_READY` 事实，Session 只路由。
- [x] 将 `propose()` 缩为固定顺序：吸收观察、推进交接、推进信息、推进规划、推进身体、组装提案。
- [x] 删除已经迁移的决定分支，不把原 700 多行整体搬到另一个大方法。
- [x] 检查持续目标满足期间不会创建新查询或恢复；B11 的效果确认责任不会因导航终态丢失。

任务 E 已由提交 `2e9eb8cc` 实施，并由 `f6ab92de` 修正完整协调对照发现的停止中 probe 优先级。信息 owner 现在形成查询分类、低部可见要求、观察视角、事实取得和原等待结果；五类规划结果携带本次请求、缺失格和落点探边事实。`propose()` 从 533 行缩为 105 行，只保留生命周期、风险、身体交接和提案组装的薄路由。最终 A—E 直接回归 262／262；完整矩阵、v7 和受影响实机结果见第 10 节。

## 8. 验收顺序

每个任务先运行其直接测试。出现失败就停止扩大范围，保留最短反例并检查共同规则。

任务 A—C 稳定后运行：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_navigation_handoff tests.motion_nav.test_r28_shared_recovery tests.motion_nav.test_navigation_supervised_interruptions tests.motion_nav.test_r28_migration_faults tests.motion_nav.test_navigation_session tests.motion_nav.test_planning_coordinator -q
```

任务 D—E 稳定后加入：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_navigation_route_handoff tests.motion_nav.test_runtime_navigation_verified_handoff tests.motion_nav.test_b11_world_change_navigation tests.motion_nav.test_goal_reach_policy tests.motion_nav.test_motion_start_delivery tests.motion_nav.test_action_continuity_formal tests.test_c1_navigation_session -q
```

最后才运行完整运动导航、1,448 项协调集合和固定 v7 对照。只有实际输入链发生变化时，才复跑 B11 终态停止、空中取消和空中改目标的代表性 Fabric 场景。

## 9. 完成条件

- `_restart_after_active_terminal`、`_probe_mode_exit_pending` 和替代规划失败缓存从生产代码删除。
- Session 不再分别决定探边、路线和规划失败退场后的目的地。
- `propose()` 不再直接推进两套路线控制者或维护跨 owner 的去向树。
- 任务结果、原因、worker 提交、输入、身体轨迹与当前冻结范围一致；明确修复项单列差异。
- 完整运动导航、协调集合、v7 对照和受影响 Fabric 全部达到各自既有门槛。
- 记录协调代码净变化、删除的旧分支和仍属于 R28-4／R28-3 的边界；不以文件变短代替行为证据。

## 10. 实施结果

任务 A—E 均按“失败检查、实施、独立复审、修正、再次复审”完成。最后一次直接回归为 262／262。

结构结果：

- `_restart_after_active_terminal`、替代规划失败缓存和 `_probe_mode_exit_pending` 已删除；
- Session 不再直接调用路线 executor 或 motion coordinator 的逐帧 `decide()`；
- `propose()` 从 533 行缩为 105 行；
- Session 相对开始点净减少 248 行。七个相关生产文件合计净增加 480 行，主要是有类型结果、owner 接口和停止保护；因此本轮只能说明决定归属更清楚，不能用总行数宣称复杂度已经全面下降。

第一次完整协调对照发现 2／1,448 项逐帧差异。规划失败当帧，仍为 `RETAIN` 的探边保护被路线中性取消提案覆盖，产生一帧错误松潜行。修复后，监督者优先选择仍持有身体且处于 `STOPPING` 的 probe；两项轨迹恢复原样。

最终门槛：

- 完整运动导航 1,058／1,058；
- 协调集合 1,448／1,448，与冻结集合 1,448 对无差异；
- 四个补充故障入口 4／4；
- v7 为 1,705／2,000，与前一切片 2,000 对无差异；
- B11 Fabric：60／60 正例、24／24 反例、20／20 父层终态停止；
- review-20 Fabric：空中改目标／取消矩阵 24／24。

精简证据位于 `evidence/motion_navigation/r28-1-completion-v1/`。R28-1 冻结范围关闭。下一阶段按 R28 总计划进入 R28-4；R28-3、室内终点接近、正式跟随和 R28-5 仍打开。
