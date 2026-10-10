# Task 5A：有界参考搜索核心

来源基线为 `9bd97677`，正式环境为项目 Windows `.venv\python.exe`。本任务完成三个代表参考搜索及直接边界检查。聚焦提交为 `343d10de97f22854e4eb44fa6a55d585cdb7a82d`。生产 `mc2p` 未修改，未新增 runner、worker、Runtime 接入，也未扩到完整 P0 矩阵。P0 尚未签署，两周时间盒不重置。

实施前完整读取了 Task 5A brief、D095、TP 架构／阶段／验收、总实施计划 Task 5、Task 3／4 的实现与报告、独立审查和 `timing-branch-audit.md`，以及正式 `step()`、`query_support()`、`GoalState.accepts()`。使用测试驱动开发、执行计划与完成前验证技能；没有再派生子代理。

## 1. 三族按顺序 RED／GREEN

首先只写平地测试，原型搜索模块不存在。命令：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_reference_search -v
```

RED 为 `ModuleNotFoundError: No module named 'experiments.motion_navigation.trajectory_proto.reference_search'`，`Ran 1 test / FAILED (errors=1)`。随后实现通用逐 tick 搜索、平地可执行 fixture 和共享 scanner counter，同一命令为 `Ran 1 test in 0.039s / OK`。

接着只加入 JumpUp 测试。当时尚无可执行上跳 fixture：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_reference_search.TrajectoryProtoReferenceSearchTests.test_one_block_up_is_found_without_action_solver -v
```

RED 为 `ValueError: Task 5A has not frozen this executable representative`，`Ran 1 test / FAILED (errors=1)`。冻结真实一格高平台、净空与 Walk／Jump／Neutral 输入字母表后，原搜索器无需动作分支即可找到，`Ran 1 test in 0.060s / OK`。

最后加入一格跨隙测试：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_reference_search.TrajectoryProtoReferenceSearchTests.test_one_gap_uses_same_candidate_after_real_delay -v
```

RED 同样为可执行 fixture API 尚未覆盖该族，`Ran 1 test / FAILED (errors=1)`。加入真实空格与已知落点后，运行直接模块得到 `Ran 3 tests in 0.218s / OK`。上跳与跨隙 GREEN 都只补 fixture，搜索核心没有加入动作名称或 Walk→Jump 组合特判。

这三项初始 RED 分别证明搜索 API、上跳 fixture API 和跨隙 fixture API 缺失；没有把 fixture 缺失写成“已有搜索器搜不到”。之后另有两个真实行为 RED，见下文。

## 2. 候选必须自身停止

新增目标末速放宽至 100 格／秒的反例，运行：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_reference_search.TrajectoryProtoReferenceSearchTests.test_permissive_goal_speed_still_requires_stopped_candidate -v
```

首版行为 RED：`Ran 1 test in 0.026s / FAILED (failures=1)`，末态 `vz=0.11473642444619774`，测试要求候选已经停止。首版只保证 GoalState 末速与安全停车尾迹，确实可能在还滑行时返回 FOUND。

最小修正要求 nominal 候选进入扫描前水平速度已在停止阈值内；扫描后每支目标检查也要求停止。阈值复用 scanner 的 `ZERO_SPEED_EPSILON`，不复制运动公式。即便 GoalState 放宽末速，候选仍包含完整实际停止输入。随后四项直接检查为 `Ran 4 tests in 0.258s / OK`。

## 3. 5A 集成发现的承诺证明 P1

初步 15 项直接检查与 69 项组合已经通过。逐项读取 Gap1 搜索生成的证明后，发现新的承诺边界问题：

- boundary3／4 的中性停车会掉落，尾迹为 UNSAFE；
- boundary5 位于 `(0.5,1.0,1.332290931566656)`；
- 该状态的 `on_ground=true`，公开 `query_support()` 为 BLOCKED，支撑比例为0；
- boundary5 的不可撤回输入为下一条 Jump，包含该输入的完整尾迹可以安全落地；
- 旧 `_risks()` 因“tail 安全＋on_ground”在 boundary5 关闭风险；身体实际在 boundary17 才重新落地。

这是 5A 搜索接入后发现的 P1，旧 Task 4 报告、审查和原结果保持不变。父任务明确授权本轮最小修正，同时要求再发现同类交付阻塞时按 D089 停止 5A。

先新增 `test_gap_risk_recovers_only_on_real_landing_support` 并运行：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_reference_search.TrajectoryProtoReferenceSearchTests.test_gap_risk_recovers_only_on_real_landing_support -v
```

真实 RED：`Ran 1 test in 0.122s / FAILED (failures=1)`，`QueryStatus.BLOCKED is not QueryStatus.FEASIBLE`。

最小修正仅在 `_risks()`：关闭风险区间前，沿用 scanner 的公开支撑查询确认当前边界有已知支撑。该查询消费共同节点预算，其读取依赖进入 proof；不能用在途 Jump 最后能落地来冒充当前已恢复着地。UNKNOWN 沿原 typed 信息路径返回，不改成候选碰撞或预算失败。

修正后的 Gap1 两支均在 boundary17 关闭风险，最后可放弃／承诺／恢复边界为 `2／3／17`。准时绝对边界为 `12／13／27`，晚支为 `13／14／28`，承诺命令生效 tick 分别14／15。测试独立调用公共查询验证恢复支撑，并确认依赖已绑定。公共支撑查询返回 UNKNOWN 的注入检查只证明该返回路径，不冒充真实世界 UNKNOWN 重放。

删除真实支撑检查的内存变异产生 `Ran 1 test / FAILED (failures=1)`，恢复边界错误回到5。没有改写源码文件做变异。旧多风险、真实晚支、累计伤害与各预算组件都在最终组合中通过。本轮没有继续扩充边界模型。

## 4. 接口与预算

`reference_search.py` 新增不可变 `ReferenceSearchOutcome` 和逐支 `BranchGoalCheck`。结果保存五类请求状态及 typed reason、同一输入、实际 `CommitmentProof`、末态目标检查、总计数、frontier 展开数、候选长、逐支总时长、固定输入顺序、coverage 和稳定 SHA-256。FOUND 之外不保存 proof；FOUND 要求所有声明分支的目标检查通过。

搜索是确定性的 greedy best-first：每次展开一 tick，排序只使用到目标区域的距离和当前速度。它不推算或复制物理公式，不调用已有动作求解器，不按动作名字构造组合。输入按请求 tuple 顺序展开；堆平局使用递增序号。去重只允许完整 `PhysicsState` 严格相等，包括位置、速度、tick、姿态、碰撞、冷却和资源。水平碰撞及 unsupported／invalid step 只淘汰该候选。不存在位置或速度量化。队列和 seen 集合受固定节点／step 上限约束。

搜索源码不读墙钟。`route_guidance` 保留在不可变请求与 proof，首版排序只使用当前目标区域；未声明全局最优或完整性。

`scan_commitment(..., counter=...)` 接受搜索创建的同一个 `CountedPhysics`。省略该参数时保留组件的独立扫描行为；传入时必须是匹配原请求总预算的计数器。每个候选不会重新领完整预算。

节点覆盖 frontier 展开、分支重放、每个边界停车尝试、风险恢复支撑和目标支撑确认。全部物理 step，包括共享前缀、已知 prelude、双支候选和全部尾迹，共用总 step 额度。目标使用现有 `GoalState.accepts()` 和公开支撑查询，查询事实合入 proof 并重新计算 proof hash。

统一预算反例直接统计真实 physics 调用：平地搜索展开先用28 step，完整双支扫描还要199 step；请求总额度设为199时，在第199次调用停止，返回 `NO_TRAJECTORY_IN_BUDGET/PHYSICS_STEP_BUDGET`，计数与实际调用数均为199。节点与轨迹长度反例分别保留 NODE_BUDGET／TRAJECTORY_TICK_BUDGET；两支上限不足仍保留 TIMING_BRANCH_BUDGET。

`max_trajectory_ticks` 限制同一候选长度。晚支一 tick 等待另外计入 `branch_total_ticks`，不隐藏在候选长中。输入账本把每个边界的下一条候选输入声明为不可撤回的条件预测，绑定绝对 tick 与共同控制序号，没有伪造实际客户端回执。

固定候选反例保留两类差异：Neutral 等待通过；真实 Walk 等待可以使晚支偏出窄目标，或在边缘仍有支撑却不能安全停车。任一分支不成立都不给共享候选 proof。伪造晚支冷却字段，即使 tick 正确也不能绕过真实等待重放。

## 5. 代表量尺

三个 fixture 都是完全已知的 detached 小世界、Java 1.21 ruleset、朝向0、水平静止、零剩余伤害额度。目标姿态 standing、支撑 SOLID、末速0。late entry 在 fixture 准备时由真实 step 构造，scanner 再用共同请求额度从 anchor 重放并比对完整状态。

| 代表 | 状态 | frontier 展开 | 总节点 | 总 step | 停车 step | 候选 tick | 准时／晚支总 tick |
|---|---|---:|---:|---:|---:|---:|---|
| flat_walk | FOUND | 15 | 49 | 227 | 170 | 14 | 14／15 |
| jump_up_straight | FOUND | 18 | 58 | 368 | 282 | 17 | 17／18 |
| jump_gap_1 | FOUND | 193 | 251 | 1061 | 436 | 24 | 24／25 |

准时／晚支两支的最终位置分别相同：平地 `(0.5,1.0,1.7914984078910146)`，上跳 `(0.5,2.0,1.771203216121863)`，跨隙 `(0.5,1.0,2.900102689134391)`。目标和末态从 proof 独立核对，两支水平速度均为0。

| 代表 | result hash | proof trajectory hash |
|---|---|---|
| flat_walk | a7bd06cd9b880c2312336e95ec13ba5c04b5878687b9414696e21c66cc00492b | 28821bb22331458a201b6125e516cb4f91c0f78d4a974a26ad990acf6e012023 |
| jump_up_straight | 223d995f722cc65aa8d0a3bec313a7a99a31cdfac030a61976c2a8ca079c14ab | 58e9045f5dc5a43f07892f97a54e6396b0dddcebff1bd296df9e9a7c2ac6315e |
| jump_gap_1 | cb6104b03204089ded16afb06799729b596c09481a1e1563c1cad29b9b836961 | 21281e387697cd6fad6c2de28dd179935e603407a32dca94d7bf83517e50058f |

外层单次 `perf_counter()` 测得搜索加验证约38.279／57.918／119.815 ms。墙钟只在临时测试命令外层使用，不参与排序或终止。它不是分位数、完整性能矩阵或 P1 通过证据，也未测准备／后台交付／消费阶段。Gap1 单次已约120ms，后续完整 P0 必须实测其成本，不能提前宣称达到 P1 期限。

## 6. 必需变异

使用独立 Python 进程和 `unittest.mock.patch`；每次只运行一个行为测试：

| 变异 | 对应测试 | 结果 |
|---|---|---|
| 不检查 LATE_ONE_TICK 的末态目标，直接伪授 accepted | `test_nominal_goal_is_insufficient_when_walk_wait_misses_goal` | 1失败／0错误，错误 FOUND 被发现 |
| 调用 scanner 时丢弃共享 counter，重新给原预算 | `test_search_and_scanner_share_almost_spent_total_physics_budget` | 1失败／0错误，错误 FOUND 被发现 |
| 不重放 prelude，直接信任声明入口 | `test_real_prelude_replay_is_required_even_if_declared_tick_is_right` | 1失败／0错误，错误 FOUND 被发现 |
| 恢复区间忽略公开支撑 | `test_gap_risk_recovers_only_on_real_landing_support` | 1失败／0错误，错误恢复边界5被发现 |

这些量尺末尾断言确认预期失败数量，所以外层命令退出0；不把错误实现写成通过。首次共享预算变异被计数断言发现，随后补强为从 proof 的实际尾迹输入推导199步，不依赖可被变异污染的总计数；最终变异明确发现错误 FOUND。

## 7. 最终 Windows 验证

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_reference_search -v
```

`Ran 17 tests in 1.338s / OK`。

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment tests.motion_nav.test_trajectory_proto_reference_search -q
```

`Ran 71 tests in 36.945s / OK`。包含原34项承诺扫描与20项合同，原多风险、真实等待、等待后的停车、累计伤害、已知 stale 分类和统一预算均不退步。

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_b09r_physics_step tests.motion_nav.test_b09r_physics_rollout tests.motion_nav.test_b10_gap_solver tests.motion_nav.test_motion_continuation_quality -q
```

`Ran 62 tests in 16.063s / OK`。

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_standalone_export -q
```

`Ran 12 tests in 25.571s / OK`。原型排除通配覆盖新增搜索测试，没有修改公开导出配置或脚本。

最后仅整理 fixture 的类型注解与模块说明，没有改变地形、请求、搜索或扫描行为；直接搜索复核为 `Ran 17 tests in 1.331s / OK`。`git diff --check`、`git diff --cached --check` 和提交差异检查均返回0。提交只含7个允许文件，`git diff HEAD~1 HEAD -- mc2p` 为空；提交后的 `git status --short` 为空。

## 8. 文件与交付范围

- 新增 `reference_search.py`、搜索测试；
- `scenarios.py` 增加三个可执行 fixture，保留旧18项场景元数据；
- `physics.py` 澄清请求级计数归属；
- `commitment.py` 只增加共享 counter 接口和本轮 RED 证明的真实恢复支撑检查；
- TP stage／acceptance 记录代表结果、集成 P1 与证据边界；
- 本报告保存在本地 `.superpowers/sdd/`，按前序报告约定不纳入源码提交。

未实现请求级已知必要条件 BLOCKED 证明；单个候选碰撞只返回有界搜索耗尽。未运行完整 P0 18项矩阵、完整运动导航、正式性能统计、闭环、Fabric 或 worker 生命周期。没有进入5B。Task 5A 的三族代表已经成立，完整 P0 仍须另行验收。
