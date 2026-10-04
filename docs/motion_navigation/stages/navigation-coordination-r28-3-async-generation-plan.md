# R28-3：异步计算世代与当前目标绑定实施计划

> **子 Agent 实施要求：** 必须使用串行的子 Agent 开发流程。每个任务由一个实现 Agent 完成，再由另一个只读审查 Agent检查；共享文件不能并行修改。步骤使用复选框跟踪。

日期：2026-10-04。状态：A0、A1、B、C、D、D1 和 E 的限定安全／功能交付及最终审查已完成；`050c10b` 历史窗口修复复审通过，无未关闭 P0／P1／P2。结构净减少未通过、产品统计未签署、响应 P95 七 tick 未达五 tick，R28-3 全阶段仍打开。先完成结构收敛的取舍，再进入正式跟随；不在本文擅自改动 R28 顺序。

**目标：** 同一任务频繁修订目标时，不再无条件取消正在进行的后台计算；结果返回后，只有在完整满足当前目标、身体、事实、风险和时间条件时，才能生成当前候选。

**架构：** `GoalRequestLedger` 同时保存当前目标和计算世代，但二者独立变化。`PlanningCoordinator` 保存一份不可变的计算依据，从快照构造开始一直使用到结果退场；当前目标只在结果绑定和下一项工作启动时读取。`AsyncWorkLifecycle` 继续负责单项工作的身份和期限，规划与运动领域继续负责各自的事实检查。

**技术栈：** Python 3.11、现有 `unittest`、`tests/sim`、独立 Fabric 1.21。不得新增依赖、线程、进程或通用工作流框架。

**依据：** [R28 总计划](navigation-coordination-convergence-r28-plan.md)、[D048](../decisions/0048-converge-recovery-by-risk-and-product-evidence.md)、[协调架构](../architecture/navigation-coordination-v1.md)、[R28 验收](../acceptance/navigation-coordination-convergence.md)。

## 1. 要解决的问题

当前每次 `update_goal()` 都会生成新的请求号，并通过 `PlanningCoordinator.begin()` 退场旧工作、重建快照和期限。跟随目标若每几帧移动一次，计算可能反复被取消，永远没有机会交付。

现有代码还有四个相关缺口：

1. Session 和规划 owner 共用最新请求。目标修订后，快照边界、许可、超时和结果检查也会读到新请求，无法准确表达“这项计算当时依据什么开始”。
2. 旧否定结果可能借当前请求身份进入缺信息、搭桥或失败流程。旧目标无路，不能说明新目标也无路。
3. `PlannerWorker.poll_latest()` 用请求序号大小选择结果。新任务的序号从 1 重新开始，复用 worker 时，旧任务的高序号结果可能压掉新任务结果。
4. `spawn_successor()` 只要求新的 Session ID，没有强制新的任务 ID。调用者仍可能通过 successor 获得新账本，却继续声称是原任务。

R28-3 要修的是这些共同规则，不修改寻路算法、动作物理或跟随策略。

## 2. 不变量与边界

### 2.1 三种身份各自回答一个问题

| 身份 | 回答的问题 | 谁拥有 | 什么情况下变化 |
|---|---|---|---|
| 任务 ID | 恢复、风险和伤害属于哪项长期任务 | 任务创建者和现有账本 | 只有上层明确创建新任务 |
| 计算世代 | 旧计算是否仍有资格交给当前任务检查 | `GoalRequestLedger` | 世界切换、取消、新任务、必须换状态锚点或计算依据明确失效 |
| 工作身份 | 同一世代中的哪一次规划、信息或动作求解 | 各异步 owner 的 `AsyncWorkLifecycle` | 每项真实新工作 |

目标位置、目标修订号和请求序号不等于计算世代。普通目标修订只改变当前目标，不递增计算世代。

### 2.2 当前请求与计算依据分开

`GoalRequestLedger.request` 是当前业务请求。`PlanningCoordinator` 另存不可变的 `PlanningCalculationBasis`，至少包含：

- 计算开始时的完整请求；
- 计算世代；
- 一次性许可；
- 快照边界和提交身份。

固定工作窗口仍只由 `AsyncWorkLifecycle` 拥有，`PlanningCalculationBasis` 只通过工作身份引用它，不复制起止时间。快照构造、提交、超时、原依赖范围和 worker 生产者检查始终读取原计算依据及其 lifecycle。目标修订只更新当前请求。计算结果到达后，再同时读取原依据和当前请求。

这不是第二份当前目标。它只是后台工作已经发生过的不可变事实。

### 2.3 结果处理顺序固定

任何规划或运动结果都按下面顺序处理：

1. 核对世界、任务、计算世代、owner、工作身份、固定期限和是否已退场；
2. 核对结果确实来自该工作的原计算依据；
3. 由领域 owner 检查当前事实；
4. 只有正结果可以尝试绑定当前请求；
5. 绑定通过后，才生成当前候选并交给监督者。

旧目标的无路、缺信息、预算耗尽和内部搜索结论不能绑定到新目标。它们只让原工作有界退场，然后根据当前请求决定是否开始新工作。

成功路线也不能只替换 `request_id` 或 `goal_revision`。接纳必须重新核对：

- 当前完整 `GoalState`，包括位置、速度、姿态和朝向；
- 当前身体能否连接路线入口；
- 当前地形依赖、规则和能力版本；
- 当前风险和伤害余额；
- 原工作期限和动作命令窗口。

首版不截断旧路线，不定义“小幅修订”，不复用不完整的路线前段。无法证明适用就从当前请求重新计算。

### 2.4 身体与效果责任保持原规则

计算失效不等于身体可以释放。已经起跳、正在落地、仍有在途输入或已经点击放置时，原 owner 继续负责收尾。R28-3 不改变 `ExecutionSupervisor`、世界操作确认、任务风险账本和 R28-4 恢复预算。

运动结果只迁移重复的身份和工作关联判断。动作入口、证明、否定依据、复核、实际生效窗口和落地责任全部保留。

### 2.5 successor 只能表示新任务

正式接口改为：

```python
spawn_successor(session_id: str, *, task_id: str) -> NavigationSession
```

`task_id` 必须非空，且不同于原任务。新 Session 在首次 `start()` 或 `start_goal()` 时继续核对该任务 ID。

具体顺序固定如下：无效或与原任务相同的 `task_id` 在 `spawn_successor()` 转移任何 worker 前拒绝。合法 spawn 把该 ID 保存为 successor 的不可变任务声明，然后一次性转移 worker 并关闭原 Session。首次启动若与声明不符，必须在建立账本、选择到达策略、提交工作或改变生命周期前拒绝；worker 仍归 successor，不回滚给已经关闭的原 Session，调用者可以用正确任务 ID 重试首次启动。

同一任务换 Session 只能使用现有 `rebuild_same_task()`。它继续保留任务 ID、到达策略、恢复账本、风险账本、伤害支出和持续任务时钟。

## 3. 公共接口

### 3.1 计算作用范围

在 `async_work.py` 增加不可变值对象：

```python
@dataclass(frozen=True, slots=True)
class AsyncComputationScope:
    world_session_id: str
    task_id: str
    generation: int
```

`AsyncWorkIdentity` 保存这份 scope，仍保留 `owner_instance_id`、`work_kind`、`subject_id` 和工作 revision。不要建立全局工作注册表。

`AsyncWorkLifecycle` 的共同门禁至少区分：当前工作、旧世代、同世代其他工作、已退场、重复和过期。返回枚举或不可变结果，不靠原因字符串选择流程。

目标账本通过只读 `current_computation_scope` 提供当前 scope。`NavigationSession` 只负责在逐帧推进时把它传给规划和运动结果入口；这两个 owner 不保存或修改目标账本。仅把 scope 写进活动工作和返回结果还不够，因为二者可能同时属于已经失效的旧世代。

### 3.2 目标账本

`GoalRequestLedger` 提供三类明确操作：

- 绑定任务和世界，并产生首个计算 scope；
- 修订当前请求，不改变 scope；
- 以有类型原因撤销计算资格，产生下一世代。

有类型原因首版只覆盖：世界切换、取消、需要新状态锚点、相关计算依据失效。普通观察、转头、目标位置修订和无关格变化不能递增世代。

### 3.3 规划修订和结果绑定

`PlanningCoordinator` 增加“只修订当前请求”的入口。它不能退场活动工作、重置原窗口或重建快照。真正开始新工作仍使用 `begin()`，并消费现有许可。

`RouteAdmitter` 增加按当前请求接纳正结果的入口。调用者必须同时传入原 candidate、原计算请求、当前请求、当前身体帧和当前剩余风险额度。返回沿用现有有类型接纳结果。

绑定成功后的 `ActiveRoute` 使用当前请求身份和当前完整目标，同时保留原 `AsyncWorkIdentity` 供诊断。绑定不会刷新原工作期限、动作证明或命令窗口。

### 3.4 worker 结果选择

`PlannerWorker` 不再跨任务或世代比较 `request_sequence`。控制侧只根据完整工作身份判断结果是否属于当前已提交工作。旧结果可以被读出并记录为旧工作，但不能清掉当前 `_last_submitted`，也不能压住随后到达的当前结果。

D1 后正式 worker 仍为单进程串行搜索，请求与结果容量各为二；逐项 `poll_available()` 交付，按完整工作身份释放对应提交。满载返回 BUSY，不删除已接纳请求，不按 latest 覆盖结果。逻辑退场后，已提交工作仍占 receipt 容量，直到真实回执到达。原 C 批次的一槽规则只保留为实施历史。

世界交互继续遵守“已经派发的效果独立结算”。放置点击前可以因计算 scope 失效而撤销准备；点击已经实际派发后，确认事务按原 identity 和窗口完成，不因目标修订、取消或计算世代变化丢失。R28-3 不把放置确认并入规划／运动结果接纳。

## 4. 串行实施任务

### A0：冻结反例、调用点和复杂度基线

**文件：**

- 新增：`tests/motion_nav/test_r28_async_generation.py`
- 修改：`tests/motion_nav/test_planner_worker.py`
- 记录：本计划第 8 节

**接口：** 本任务只建立失败检查，不改生产接口。

- [x] 复现目标修订会退场正在构造快照和已经提交的规划工作。
- [x] 检查旧 `NO_ROUTE`、缺信息和预算类结果能否被错误用于新目标；现行门禁已拒绝，三个防回归检查通过，见第 8.1 节。
- [x] 复现旧任务高请求序号与新任务低序号同时出现时的 worker 选择问题。
- [x] 检查同任务 successor 目前仍能创建新账本。
- [x] 冻结受影响生产文件的行数、状态字段、身份比较和退场调用数量。
- [x] 最小集合为五个辅助探针和三个防回归检查。三项必需缺口均成立；旧否定保护通过，首次误收集既有 fixture 的额外执行另记，不作完整回归声明。
- [x] 提交 A0（提交信息 `test(navigation): freeze R28-3 async baselines`）。

### A1：封住新任务和同任务重建边界

**文件：**

- 修改：`mc2p/motion_nav/navigation_session.py`
- 修改：`tests/navigation_session_fixtures.py`
- 修改：`tests/sim/async_work_sequences.py`
- 修改：`scripts/r27_successor_gap_runtime.py`
- 测试：`tests/motion_nav/test_navigation_session.py`
- 测试：`tests/motion_nav/test_r28_planning_retry_successor.py`

**产生：** `spawn_successor(session_id, *, task_id)`；新 Session 首次启动必须匹配声明的任务。

- [x] 先写无新任务 ID、沿用原任务 ID、首次启动 ID 不一致和 spawn 参数拒绝的检查。spawn 参数拒绝时原 Session 保有 worker；合法 spawn 后首次启动拒绝时 successor 保有 worker，且可以用正确 ID 重试。
- [x] 修改接口及所有测试、脚本和 fake；原生产调用中不得出现 successor 回退。
- [x] 检查 `rebuild_same_task()` 仍保留同一个账本对象和累计伤害。
- [x] 跑 successor／continuation 专项并提交 A1。

### B：建立计算世代和共同工作门禁

**文件：**

- 修改：`mc2p/motion_nav/async_work.py`
- 修改：`mc2p/motion_nav/navigation_owners.py`
- 修改：`mc2p/motion_nav/known_map_planner.py`
- 修改：`mc2p/motion_nav/planning_coordinator.py`（本任务只迁移 identity 构造和只读 scope 参数）
- 修改：`mc2p/motion_nav/motion_coordination.py`（本任务只迁移 identity 构造和只读 scope 参数）
- 修改：`mc2p/motion_nav/world_interaction.py`
- 修改：`tests/sim/async_monitor.py`
- 测试：`tests/motion_nav/test_async_work_verification.py`
- 测试：`tests/motion_nav/test_planning_ownership_gate.py`

**产生：** `AsyncComputationScope`、带 scope 的 `AsyncWorkIdentity`、目标账本的修订与失效接口，以及逐帧只读 current-scope 输入。

- [x] 先写 scope、世代单调、同世代不同工作、重复交付和退场后的检查。
- [x] 写普通修订、转头、无关格变化不递增世代的检查。
- [x] 写世界切换、取消、新锚点和相关依据失效会作废旧结果的检查。
- [x] 实现最小值对象和门禁；不增加全局 registry 或第二本请求账本。
- [x] 更新规划、运动、信息和世界交互的全部身份生产者，以及测试监视器。已派发放置确认继续按原事务结算，不读取当前世代决定是否回滚。
- [x] 让规划和运动的结果入口显式接收目标账本当前 scope；写旧 scope 的活动工作和返回结果不能互相为对方证明仍然有效。
- [x] 跑直接相关检查并提交 B。

### C：保留不可变规划依据并处理旧否定结果

**文件：**

- 修改：`mc2p/motion_nav/planning_coordinator.py`
- 修改：`mc2p/motion_nav/navigation_session.py`
- 修改：`mc2p/motion_nav/planner_worker.py`
- 测试：`tests/motion_nav/test_planning_coordinator.py`
- 测试：`tests/motion_nav/test_r28_information_planning_updates.py`
- 测试：`tests/motion_nav/test_r28_planning_failure_handoff.py`
- 测试：`tests/motion_nav/test_planner_worker.py`

**产生：** 不可变 `PlanningCalculationBasis`；只更新当前目标的规划入口；按完整工作身份选择结果。

- [x] 先覆盖快照构造前、构造中、提交后和结果已返回四个修订时点。
- [x] 让 builder、bounds、许可、超时和生产者核对只读原计算依据。
- [x] 让目标修订更新当前请求，但不退场活动计算或刷新期限。
- [x] 让旧否定、旧缺信息和旧搭桥结论只退场原工作，并从当前请求启动必要工作；不得构造当前目标失败。
- [x] 修改 worker 的结果选择，覆盖 successor 跨 scope 高低序号、重复结果和 worker 死亡。
- [x] 删除 Session 与规划 owner 中已经被共同门禁接替的同义比较。
- [x] 跑直接相关检查并提交 C。

### D：把正结果绑定到当前完整目标，并收敛运动结果关联

**文件：**

- 修改：`mc2p/motion_nav/route_admission.py`
- 修改：`mc2p/motion_nav/planning_coordinator.py`
- 修改：`mc2p/motion_nav/motion_coordination.py`
- 修改：`mc2p/motion_nav/motion_worker.py`
- 测试：`tests/motion_nav/test_r28_async_generation.py`
- 测试：`tests/motion_nav/test_r27_async_admission.py`
- 测试：`tests/motion_nav/test_b10_motion_candidate.py`
- 测试：`tests/motion_nav/test_async_gap_revalidation_delivery.py`

**产生：** 当前请求正结果绑定；规划和运动共用计算世代门禁，但保持各自领域检查。

- [x] 写旧路线能完整满足新目标的正例，并核对生成的是当前请求候选。
- [x] 写相同终点但速度、姿态、朝向、风险或资源要求变化的拒绝例。
- [x] 写身体入口变化、相关依赖变化、窗口过期和目标持续远离的有界结果。
- [x] 使用当前风险余额和资源要求重新接纳，不能读取旧 candidate 的旧预算授权。
- [x] 迁移 motion 的重复身份判断；保留 proof、negative basis、`REVALIDATE`、命令窗口和原执行器收尾。
- [x] `MotionRouteCoordinator.decide()` 或等价正式入口必须逐帧收到只读 current scope；目标修订保持同一 scope，取消、世界切换和新锚点后的旧结果必须被共同门禁拒绝。
- [x] 写空中改目标、取消和旧结果迟到时身体 owner 不丢失的正式链检查。
- [x] 写已派发放置后取消／修订的确认检查，证明计算失效没有回滚或丢失实际效果。
- [x] 删除无消费者的旧状态和重复比较，跑直接相关检查并提交 D。

### E：正式链、完整回归、实机与文档

**文件：**

- 修改：现有 `tests/sim` 场景和 R28 专项工具
- 修改：`docs/motion_navigation/architecture/navigation-coordination-v1.md`
- 修改：`docs/motion_navigation/acceptance/navigation-coordination-convergence.md`
- 修改：`docs/motion_navigation/stages/navigation-coordination-convergence-r28-plan.md`
- 修改：`docs/motion_navigation/decisions/0048-converge-recovery-by-risk-and-product-evidence.md`
- 修改：`AGENTS.md`

- [x] 增加每 3／5／8 tick 修订、同世代多工作、旧否定、重复交付、取消、世界切换、新锚点和持续远离场景。
- [x] 先跑 R28-3 专项和受影响正式调用链；失败立即停止扩测。
- [x] 稳定后运行完整 `tests/motion_nav`、1,448 项协调集合、四个补充故障入口和 v7。
- [x] 逐项报告严格组、安全事件、作业数、输入和轨迹差异；不能只比较成功总数。
- [ ] 在候选完整运行前声明受影响分层、实质退步阈值和行为等价口径。若候选改变目标修订的实际结果或输入时序，先用独立探索数据校准，再按验收第 5 节冻结每个受影响分层的种子、首次查看样本量、最大样本量、两次查看点和停止规则。首次查看每层至少 1,500 对；3,200／6,400 只作为既有校准参考，不能替代本机数据。中途改代码必须重新开始，不能拼接样本。
- [x] 若现有运行器尚不能执行两次查看，R28-3 可以先关闭安全迁移门槛，但产品非退步必须明确保持未签署；不得用 v7 的 200 项分层样本代替足量结论。
- [x] 若旧计算绑定当前目标改变了正式输入时序，运行代表性 Fabric：规划中修订目标、严格下降或跨隙中修订／取消、首条晚一 tick。未改变的 B11 与完整 M3 可以复用原证据。
- [x] 统计删除的旧路径、Session 行数、核心协调总代码、状态字段和决定分支。若权威状态或协调分支没有减少，R28-3 不关闭，先由独立审查判断根因。
- [x] 更新四类文档和 AGENTS，提交 E。

### D1：修复频繁修订时最新目标启动过晚

E 的首轮 v7 证明，单活动规划工作虽然安全，却让最新目标等旧结果判定不适用后才开始计算。`target-late` 的零位移停顿率由约 0.153% 增至 0.299%，超过验收门槛。这个问题必须在 E 最终验收前处理，原失败结果保留。

**文件：**

- 修改：`mc2p/motion_nav/planning_coordinator.py`
- 修改：`mc2p/motion_nav/planner_worker.py`
- 修改：规划诊断与监视器的现有文件
- 测试：R28-3 正式链、worker 容量、target-late 代表场景

**产生：** 同一个 `PlanningCoordinator` 最多拥有两项有完整身份的规划工作；仍只使用一个后台进程。

- [x] 把单项计算状态收进私有 `PlanningWork` 记录。每条记录各自拥有不可变 basis、一个 `AsyncWorkLifecycle` 和 pipeline；目标账本、许可集合、`LocalAttemptChain`、恢复与风险账本继续唯一共享。
- [x] 普通目标修订有空位时立即为最新请求创建第二项工作，不取消第一项、不换计算世代，也不等待旧结果是否适用。第三次及后续修订只保留账本最新值；真实容量释放后跳过中间修订，为最新请求补工作。
- [x] 最大逻辑 work、已接纳待算 work 和未回执结果均有明确上限二。逻辑退场不能冒充 worker 已释放容量，也不能无限提交。
- [x] `PlannerWorker` 保持单进程串行搜索，但正式请求和结果容量各为二；逐项交付，不以 latest 覆盖仍可绑定的旧结果，不按请求序号排序。满载返回有类型 BUSY，由 owner 保留未提交工作，不能偷删队列内容。
- [x] 每项工作保留自己的固定期限。两项准备共用原每帧快照／事实扫描预算；不得把控制线程准备成本翻倍，也不得在控制侧同步搜索。
- [x] 同帧最多安装一条当前路线。旧正结果完整适用时可以先绑定；最新工作已经提交时仍按实际回执退场，不能产生第二次执行许可。旧否定只结束自己的工作。
- [x] 覆盖两结果顺序互换、旧结果重复、一个过期、两个过期、worker 死亡、队列 BUSY、取消、世界切换、新锚点和空中身体收尾。任务恢复不能重复购买。
- [x] 先复跑 19 项差异和 seed 8／44，再跑完整 `target-late`。零位移停顿率不得比 R28-4 基线恶化超过 5%，规划提交、控制切换、到达均值／P95 和控制准备 P95 仍执行原门槛。
- [x] 若两项设计不能同时满足安全、容量、前台 8 ms 和停顿门槛，保留 P1 并停止扩大；不得以递归 `advance()`、放宽终点检查或继续增加并发掩盖失败。

实现及定向检查见第 8.7 节。D1 将 C 批次的一槽 latest 传输改为显式两槽；E2 已完成固定停顿比较和限定 Fabric 准备成本检查，结果见第 8.8—8.10 节。独立校准及两次查看未实施，结构净减少与绝对响应门槛未通过。上面的完成勾选只表示检查、报告或限定交付已经执行，不表示全部门槛通过。

## 5. fail fast 顺序

每个任务先运行最小反例。出现未预期失败后，不继续完整矩阵或 Fabric。先保存最短反例，再检查共同规则。

顺序固定为：

1. 纯身份和 successor 边界；
2. worker 跨 scope 乱序；
3. 快照构造中修订；
4. 旧否定不能结束新目标；
5. 正结果完整绑定；
6. 空中身体责任；
7. 正式链小集合；
8. 完整回归和实机。

不得通过延长期限、增加重试次数、放宽目标或删除失败样本使检查通过。

## 6. 子 Agent 分工与文件所有权

子 Agent 串行执行，不并发编辑：

| Agent 任务 | 文件所有权 | 交付 |
|---|---|---|
| A0 Agent | 新专项测试、worker 测试 | 反例和基线，不改生产代码 |
| A1 Agent | successor 接口及消费者 | 新任务身份边界 |
| B Agent | `async_work.py`、`navigation_owners.py`、身份契约 | 计算世代和共同门禁 |
| C Agent | `planning_coordinator.py`、`planner_worker.py`、Session 规划路由 | 不可变计算依据和旧否定处理 |
| D Agent | `route_admission.py`、motion 相关文件 | 当前请求绑定和运动关联迁移 |
| E Agent | 正式模拟、文档、最终证据 | 阶段收口 |

每个实现 Agent 完成后，另启一个审查 Agent。审查只报告可操作问题，不修改代码。前三轮问题交回原实现 Agent；第四轮以后才更换实现 Agent。最后再启一个跨任务审查 Agent。

## 7. 明确不做

- 不实现正式跟随技能；
- 不修改室内贴墙、墙角、走廊尽头和边缘接近；
- 不做路径前段截断、任意角度平滑或目标距离阈值；
- 不修改 A*、运动计算器、物理参数和动作收益；
- 不合并任务恢复预算与局部工作期限；
- 不新建全局异步调度器、通用消息总线或第二套状态机；
- 不让技能层保存计算世代、接纳权限或 worker 生命周期。

## 8. 完成标准

R28-3 只有同时满足下面条件才关闭：

1. 普通目标位置修订不会取消正在进行的计算，也不会刷新原期限；
2. 旧否定结果不能结束、阻塞或要求新目标取旧信息；
3. 正结果只有完整满足当前目标、身体、事实、风险和窗口时才能绑定；
4. 世界切换、取消、新任务、新锚点和相关依据失效会拒绝旧计算；
5. successor 必须显式取得新的任务 ID，同任务只能走 continuation；
6. planner worker 不再跨 scope 用请求序号判断最新结果；
7. motion 的原证明、复核、窗口和身体收尾保持有效；
8. 重复交付和同世代其他工作不能改变当前任务；
9. 安全不变量零违规，完整回归没有未解释退步；
10. 旧的无条件取消、重复身份比较和旧否定套当前身份路径已经删除；
11. Session 不新增异步业务状态，协调代码和决定分支有可核对的净减少；
12. 正式跟随探针可以只提交目标修订，不需要自己管理世代、取消计算或接纳结果。

### 8.1 A0 事实清单（2026-10-04）

A0 在源码提交 `67461f76120861c36ee43454961cb9fcfcf1685c` 上完成冻结，没有修改生产代码。以下三项现有缺口均已通过正式接口复现：

| 缺口 | 实际结果 | 后续任务 |
|---|---|---|
| 普通目标修订退场在算工作 | `NavigationSession.start() -> propose() -> update_goal()`：快照已推进一个批次但未提交、已提交并等待结果两种时点，原工作都有 `finish` 事件，原因为 `planning_work_replaced`，活动身份被替换 | C |
| worker 跨任务比较请求序号 | 当前任务序号 1 已提交后，旧任务序号 99 的结果会清掉当前 `_last_submitted`；两份结果同次可读时仍返回旧结果，当前结果被丢弃 | C |
| successor 没有强制新任务身份 | 原 Session 用 `start_goal()` 与 `propose()` 真实完成后，`spawn_successor()` 不要求任务 ID；新 Session 可以 `start()` 同一任务并创建新的恢复、风险账本 | A1 |

worker 反例使用受控结果队列注入，实际调用生产 `submit_surface_snapshot()` 和 `poll_latest()`，使用带完整 `AsyncWorkIdentity` 的真实规划请求与结果。两份结果同次可读的探针只验证控制侧选择逻辑，不宣称容量为 1 的生产队列始终能形成该时序。单份旧结果清掉当前提交记录的反例不依赖双结果同时可读。

旧无路线、缺信息和预算超时三个保护测试均通过：旧结果交付后，当前请求继续运行，没有失败、旧信息需求或局部重试支出，当前提交和工作身份保留。缺信息结果由真实规划生成；无路线和超时状态通过测试传输注入。当前身份／request／revision 门禁已经拒绝这些结果，不能把它们记成现存缺陷。

五个缺口探针保存为未被 unittest 收集的测试辅助函数；三个正式保护测试保持全绿。修复任务须在修改生产行为时启用相应行为断言，不能把“现在确有缺口”的断言加入正式回归要求。新增文件只收集这三个测试，不重复收集导入的 fixture 测试类。

受影响生产文件基线如下。状态字段按“每个类的不同 `self` 赋值目标，加直接声明的注解字段”统计；身份比较按 AST `Compare` 中命中冻结身份字段名统计；退场按冻结方法名的 AST 调用统计。它们都是可复核的源码清单，不能直接等同于权威状态数或重复比较数。分支统计 `If`、`IfExp` 和 `Match`；不计循环与布尔运算。完整字段名、方法名、逐项位置和源码 SHA-256 见 `.tmp/r28-3/A0-complexity.json`。

| 文件（`mc2p/motion_nav/`） | 行数 | 状态字段 | 身份比较 | 退场调用 | 决定分支 |
|---|---:|---:|---:|---:|---:|
| `async_work.py` | 239 | 45 | 13 | 0 | 25 |
| `navigation_owners.py` | 368 | 35 | 3 | 0 | 39 |
| `known_map_planner.py` | 3,092 | 195 | 8 | 0 | 312 |
| `planning_coordinator.py` | 1,999 | 79 | 68 | 10 | 202 |
| `motion_coordination.py` | 1,500 | 34 | 17 | 1 | 163 |
| `world_interaction.py` | 605 | 51 | 7 | 1 | 68 |
| `navigation_session.py` | 4,587 | 151 | 30 | 9 | 574 |
| `planner_worker.py` | 322 | 16 | 2 | 0 | 39 |
| `route_admission.py` | 908 | 31 | 9 | 0 | 115 |
| `motion_worker.py` | 279 | 24 | 12 | 0 | 32 |
| 合计 | 13,899 | 661 | 169 | 21 | 1,569 |

调用点另按 `mc2p/`、`scripts/`、`tests/` 的 Python AST 冻结：`AsyncWorkIdentity()` 17 处、`spawn_successor()` 7 处、`rebuild_same_task()` 4 处。该清单包括测试与本轮辅助反例；不是生产调用数量。后续变更需按文件类别分别核对。A0 的局部报告保存在 `.tmp/r28-3/A0-counterexamples.json`、`A0-complexity.json` 与可复跑的 `A0-freeze.py`，本节只声明 A0 基线，不声明 R28-3 已关闭。

检查命令：

```powershell
D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python .tmp/r28-3/A0-freeze.py
D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python -m unittest tests.motion_nav.test_r28_async_generation.OldNegativeResultGuards -v
git diff --check
```

上述五个辅助探针和三个保护测试组成 A0 的八项低成本检查。首次执行新增模块时，直接导入 fixture 类曾额外收集 81 项既有检查，合计 84/84；随后改为导入 fixture 模块，最小专项为 3/3，不把额外执行计为本阶段完整回归。

### 8.2 A1：显式新任务与首次启动校验

`spawn_successor(session_id, *, task_id)` 已强制声明新的任务 ID。缺省、位置参数、空值和沿用原任务的输入都在 worker 转移前拒绝。合法 spawn 把任务 ID 保存为后继 Session 的固定声明，转移规划和运动 worker，然后关闭原 Session。

首次 `start()` 或 `start_goal()` 的任务 ID 不符时，后继 Session 保有 worker；恢复和风险账本、到达策略、生命周期及规划提交均不改变。使用声明中的 ID 可以重试。`rebuild_same_task()` 的既有检查继续确认账本对象、到达策略、恢复支出、累计伤害和持续任务时钟保持原值。

两项新增边界检查先在原实现上失败，随后通过最小修改完成。修改后的首次验证发现测试辅助数据未为 `start_goal()` 提供完整 `GoalState`，已补齐；这个测试构造问题没有用于修改生产门槛。调用点复查额外发现 `test_r27_async_admission.py` 中的直接调用，本轮只迁移该调用及对应 driver 启动的任务 ID。同一个目标仍可用于新任务，两者不再共用任务 ID。

检查命令及结果：

```powershell
D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python -m unittest tests.motion_nav.test_r28_planning_retry_successor.SuccessorAndContinuationContractTests -q
D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python -m unittest tests.motion_nav.test_navigation_session tests.motion_nav.test_r28_planning_retry_successor tests.motion_nav.test_r27_async_admission tests.motion_nav.test_async_gap_revalidation_delivery tests.motion_nav.test_r28_async_generation -q
D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python -m unittest tests.test_c1_navigation_session tests.test_c1_external_motion_evidence tests.test_c1_melee_evidence tests.test_c1_moving_melee_evidence tests.test_fixed_melee_driver tests.test_moving_melee_driver tests.test_b12b_runtime_injection_acceptance -q
git diff --check
```

successor／continuation 为 8/8；直接相关集合为 105/105；fake 的消费者集合为 100/100。A0 同任务辅助探针现在报告拒绝、无新账本、原 Session 未关闭；原冻结 JSON 未覆盖。新增边界测试同时检查规划和运动 worker 的所有权与关闭次数。本轮未改计算世代、规划结果选择或动作质量，也未运行完整协调集合、产品 v7 或 Fabric；这些门槛留给后续任务。


### 8.3 B：计算世代与共同门禁

已增加不可变 `AsyncComputationScope(world_session_id, task_id, generation)`。`AsyncWorkIdentity` 引用这份 scope，工作键包含世代；原世界和任务字段改为只读属性。生产代码只有 `GoalRequestLedger` 构造 scope。账本首次绑定产生世代 1，普通请求修订不换代；世界切换、取消、新状态锚点和相关依据失效使用 `ComputationInvalidationCause` 明确换代。普通转头、无关格变化和信息取得本身不换代。

`AsyncWorkLifecycle.check(identity, now_ns, *, current_scope)` 和 `try_apply(...)` 必须显式收到当前 scope。门禁区分当前工作、失效 scope、同 scope 其他工作、已退场、重复交付和过期。每项工作最多应用一次；原固定工作窗口和有界历史保持不变。规划 `advance()`、信息 `reconcile_information()` 和 motion `decide()` 都收到只读当前 scope。Session 经 `ExecutionSupervisor` 和 `RouteControl` 机械传递，未改变身体选择和停止逻辑。

同任务重建通过 `GoalRequestLedger.resume_computation_after_reanchor()` 延续原世代并为新锚点递增。新 Session 的请求仍为空，旧 Session 保留原诊断；恢复、风险和伤害账本不变。尚未形成规划请求的目标也会先绑定 task/world，取消和世界切换可明确撤销其计算资格。

规划、信息、运动和放置身份生产者及测试已迁移。放置事务构造时收到原 scope；导航放置使用 Session 当前 scope，独立放置工具使用其明确 world/task 绑定独立目标账本。已经派发的确认只核对原事务 scope 和固定窗口，账本换代不会抹掉实际效果。本任务没有改变点击准备、取消、确认或动作行为。`known_map_planner.py` 现有请求与候选通过 `work_identity` 携带新 scope，无需增加另一份字段。

新增七项行为检查覆盖世代单调、普通修订／转头／无关观察、六类工作门禁、正式规划和 motion 的旧 scope 来件、已派发放置确认及同任务重建。旧活动工作与结果拥有相同旧 scope 时，仍不能相互证明有效。初次检查因新接口尚未实现失败；同任务重建检查也先暴露未延续 scope 的缺口。机械迁移最初误为三个底层执行器调用增加了 scope 参数，已修正；motion 测试还补齐了结果实际进入消费窗口的到达时点。这些失败未用于修改动作或接纳门槛。

最终直接相关集合为 **272/272**，`git diff --check` 通过：

```powershell
D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python -m unittest tests.motion_nav.test_r28_async_generation tests.motion_nav.test_planning_ownership_gate tests.motion_nav.test_navigation_session tests.motion_nav.test_r28_planning_retry_successor tests.motion_nav.test_async_work_verification tests.motion_nav.test_planning_coordinator tests.motion_nav.test_r27_async_admission tests.motion_nav.test_b10_motion_candidate tests.motion_nav.test_b11_block_placement tests.motion_nav.test_b10_motion_worker tests.motion_nav.test_planner_worker tests.motion_nav.test_action_continuity_solver_limits tests.motion_nav.test_continuous_descent tests.motion_nav.test_r28_information_planning_updates tests.motion_nav.test_r28_planning_failure_handoff tests.motion_nav.test_async_gap_revalidation_delivery -q
git diff --check
```

B 只完成共同门禁、世代事件和机械传递。普通目标修订仍按既有规则替换规划工作；原否定结果、正结果绑定与 motion 领域处置继续留给 C／D。未运行完整运动导航、协调矩阵、产品 v7 或 Fabric，R28-3 尚未关闭。

#### B 审查补正：规划内部相关依据失效

独立审查发现两条遗漏：返回候选的相关依据改变，以及快照已扫描的已知事实改变。原实现都通过 `_retry_or_fail()` 重建工作，却没有撤销计算世代；原 272 项检查未覆盖这两条换代断言。两条新增正式路径检查先以 `generation 1 != 2` 失败，失败事实保留在审查记录中。

补丁在 `_retry_or_fail()` 的共同入口按 `RetryCause.DEPENDENCY` 调用共享账本的 `BASIS_INVALIDATED`，然后继续原局部链、替代请求和许可处理。一次依赖失效只换代一次；局部重建不能使原计算依据继续保有资格，耗尽时仍使用原失败原因。没有修改任务恢复购买、身体停止和动作责任。

正式 Session 候选复核与 PlanningCoordinator 快照构造两条路径均确认新工作使用下一世代，旧工作 scope 被拒绝；真实旧候选再次交付会被丢弃。无关格变化保持原世代；局部失败数仍为 1、任务恢复购买数仍为 0。使用上面的同一完整直接命令复跑，结果为 **274/274**，`git diff --check` 通过。此补正不扩大 B 或 R28-3 的验收范围。

### 8.4 C：保留原规划依据与旧结论退场

`PlanningCalculationBasis` 已保存开始时的完整请求、原 scope、一次性许可、快照边界、工作身份和实际提交请求。它是不可变值对象，提交时通过替换值记录真实生产者；没有复制工作起止时间。窗口仍由 `AsyncWorkLifecycle` 拥有。原 `_active_permit` 和 `_submitted_request` 状态字段已合并到 basis；保留只读属性供现有诊断读取。

`update_goal()` 对仍在构造快照或等待 worker 的普通修订调用 `revise_request()`。当前业务请求变化，原 builder、待复核结果、提交请求、scope 和固定窗口保持原值。快照重启仍使用原 bounds，超时仍读取原请求的限制。检查覆盖构造前、构造中、提交后、结果已返回但正在复核事实四个时点，并确认每 3／5／8 tick 修订时只提交一项原工作。

旧无路、缺信息、搭桥结论、搜索超时、原窗口到期和旧提交拒绝只使原工作结束，然后为当前请求消费任务更新许可。没有局部失败支出或任务恢复购买。旧缺信息结果遇到知识增加时，也不会把旧 blocker 登记成当前任务进展。补充检查先发现这条进展污染和旧提交拒绝套当前失败的遗漏，再分别修正；原失败没有用于放宽门槛。

worker 按完整工作身份选中当前提交的结果。旧任务高序号、新任务低序号以及反向序号组合均不再影响选择；两种交付顺序、重复结果和死亡检查通过。旧结果可以被读出，但不会清掉当前 `_last_submitted`；死亡报告保留当前工作身份。无 scope 的诊断路径只比较明确生产者字段，不生成默认世代。

删除了 worker 跨工作请求序号比较、规划结果门禁前的重复活动 identity 比较，以及许可诊断中重复的 owner／subject／revision／工作键比较。Session 没有新增异步业务状态；原身体 owner、路线归属、风险和动作检查保持原规则，本轮没有把这些领域检查当作重复代码删除。

新增最小集合为 15 项。直接相关集合为 **303/303**，`git diff --check` 通过：

```powershell
D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python -m unittest tests.motion_nav.test_r28_async_generation tests.motion_nav.test_planning_ownership_gate tests.motion_nav.test_navigation_session tests.motion_nav.test_r28_planning_retry_successor tests.motion_nav.test_async_work_verification tests.motion_nav.test_planning_coordinator tests.motion_nav.test_r27_async_admission tests.motion_nav.test_b10_motion_candidate tests.motion_nav.test_b11_block_placement tests.motion_nav.test_b10_motion_worker tests.motion_nav.test_planner_worker tests.motion_nav.test_action_continuity_solver_limits tests.motion_nav.test_continuous_descent tests.motion_nav.test_r28_information_planning_updates tests.motion_nav.test_r28_planning_failure_handoff tests.motion_nav.test_async_gap_revalidation_delivery tests.motion_nav.test_r28_reanchor_handoff -q
git diff --check
```

D 的正结果绑定尚未实施。旧正结果通过原事实检查后也只启动当前请求的新工作，不替换身份后直接授权。完整目标、当前身体、风险余额及当前资源的正结果绑定仍须由 D 实施和审查。未运行完整运动导航、协调矩阵、产品 v7 或 Fabric，R28-3 尚未关闭。

#### C 审查补正：拒绝复用原规划请求 ID

独立审查发现，直接调用 `revise_request()` 时仍可保留原请求 ID、只提高修订号和序号。这样会让旧无路结果被当作当前请求的失败。新增真实 owner 入口检查先以未抛出 `ContractViolation` 失败。

补丁只在现有修订校验中拒绝复用 `calculation_basis.request.request_id`，拒绝发生在账本更新前。测试确认当前请求对象、builder、工作身份和期限都保持原值；随后旧无路结果仍只产生原修订的失败。没有修改 `_has_revised_request()`，也没有新增状态。使用上面同一直接命令复跑，结果为 **304/304**，`git diff --check` 通过。

### 8.5 D：完整正结果绑定与运动接纳门禁

`RouteAdmitter.admit_current_request()` 同时读取原候选、实际提交请求、当前请求、当前身体与剩余伤害额度。它先核对原生产者字段，再使用当前食物、当前最低资源要求和当前伤害余额重放整条路线的资源变化。普通 surface 接纳与绑定复用同一个资源检查函数。成功路线保存重算后的最终资源，旧候选保持不变。

修订后的正结果只在实际入口和出口仍满足要求时绑定。入口核对当前落地状态、姿态、模式和速度；有动作或地面证明窗口时核对原窗口。出口读取原可执行路线的实际终点与最终动作出口范围，调用当前完整 `GoalState.accepts()`。首版不截断路线，不按新区域延长末段，不把旧 `GoalState` 的字段相等当成新终态证明。缺少终端朝向范围、严格低速出口或完整入口证据时，保守转当前请求重算。去搭桥工作点的中间路线也不能作为旧完整正结果复用。

绑定后的 `ActiveRoute` 使用当前请求号、序号、目标修订号和完整目标；`work_identity` 保留原 `AsyncWorkIdentity`。原 lifecycle 窗口、地面动作证明、动作命令窗口均不刷新。不可变规划 basis 增加原能力值；实际提交始终使用它，接纳再核对当前能力、候选 transition 的环境/profile 与地面证明的规则。原否定、缺信息和搭桥结论仍只使旧工作退场，不能绑定新目标。

motion 已删除结果接纳中的重复活动 work 等值比较和无 identity 的兼容授权分支。结果与首次交付采样使用共同 `AsyncWorkLifecycle.check()`；connection、candidate revision、anchor、proof、negative basis、后台 `REVALIDATE`、实际生效窗口和 `LocalAttemptChain` 均保留。scope 失效会撤销计算准备并调用原执行器取消流程，实际空中身体和在途输入继续由原 owner 收尾。已派发放置按原 identity 和原期限确认；新增检查覆盖目标修订和取消后世界/库存效果仍被确认。

新增 D 最小集合 **14/14**。第一次直接集合为 315 项，其中两项失败：`test_admission_rechecks_damage_spent_after_request_was_issued` 报告 `2.0 != 0.0`，`test_goal_revision_releases_edge_probe_before_new_walk` 报告 `forward 0 != 1`。前者由提早拒绝后没有走现有新余额重算引起；后者由把生命/吸收证据提前要求到路线阶段引起。修正后，伤害余额下降仍从当前位置重算；生命/吸收证据继续留在原 motion proof 门槛。没有修改既有断言，也没有放宽动作门槛。

规则/profile 和同位置身体速度、姿态、离地反例也分别先出现失败，再补门禁。最后的直接相关集合为 **318/318**，`git diff --check` 通过。原始最终输出保存在 `.tmp/r28-3/d-direct-final.log`。命令如下：

```powershell
D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python -m unittest tests.motion_nav.test_r28_async_generation tests.motion_nav.test_planning_ownership_gate tests.motion_nav.test_navigation_session tests.motion_nav.test_r28_planning_retry_successor tests.motion_nav.test_async_work_verification tests.motion_nav.test_planning_coordinator tests.motion_nav.test_r27_async_admission tests.motion_nav.test_b10_motion_candidate tests.motion_nav.test_b11_block_placement tests.motion_nav.test_b10_motion_worker tests.motion_nav.test_planner_worker tests.motion_nav.test_action_continuity_solver_limits tests.motion_nav.test_continuous_descent tests.motion_nav.test_r28_information_planning_updates tests.motion_nav.test_r28_planning_failure_handoff tests.motion_nav.test_async_gap_revalidation_delivery tests.motion_nav.test_r28_reanchor_handoff -q
git diff --check
```

D 未运行完整运动导航、1,448 项协调、产品 v7 或 Fabric。E 仍须完成独立审查后的正式场景、完整回归、复杂度统计和实机，并保持产品非退步证据的冻结门槛。严格终态证据不足的拒绝是首版边界，不代表已实现普遍可靠的频繁目标追逐。

### 8.6 E1：正式链与固定集合证据，产品停顿门槛未通过

本轮已完成正式链和固定集合检查，但不关闭 R28-3。候选生产提交为 `9f1e3ad`。目标修订会保留在途工作，正结果重绑定可能改变工作数和输入时序，因此运行前按“改变行为”声明范围。受影响产品层为目标修订正常／晚到；共同工作门禁的其余层仍完成固定 v7 差异检查。原声明保存在 `.tmp/r28-3/E1-run-declaration.md` 和 `E1-v7-run-declaration.md`。

新增 `test_r28_generation_formal_chain.py`，**11/11** 通过。检查覆盖 Runtime 正式链中每 3／5／8 tick 修订时的在途计算、旧否定、同世代其他工作、重复交付、取消、新锚点、持续远离时原工作有界退场、空中换代后的原 owner 收尾，以及已派发放置的确认。世界切换由正式观察适配器生成新世界帧，通过 Session 的观察入口检查。它与既有 owner 级检查共同覆盖完整身份门禁；不把每一项都描述成独立 Fabric 结果。

默认规划工作期限仍为 0.5 秒。每 5／8 tick 修订可能跨过该期限，旧工作正常到期后开始下一项工作；检查分别验证“修订不取消当前工作”和“原期限没有刷新”。持续远离场景最后由测试入口停止任务，证明原工作到期和请求停止有界，不代表已完成持续跟随产品。首次夹具使用了不存在的诊断字段、误把正常到期当成修订取消，以及使用过期 WorldView；这些调试失败均保留在 `E1-fast-first.log` 至 `E1-fast-stable.log`。最终正式链输出为 `E1-final-chain.log`。

D 独立审查已通过，原报告为 `.tmp/r28-3/D-review.md`。E 首次完整运动检查仍出现 **1,185 项中 1 个失败、6 个错误**，原输出 `E1-full-motion.log` 不改写。六个错误来自现有测试 patch 未接收新 `current_scope` 参数；本轮只修正两个测试替身的签名和必要的参数转发。B11 的工作面接近失败则是生产问题：原无路结果已经应用，第二项真实接近规划仍使用原工作身份，被重复交付门禁拒绝。没有修改原 B11 断言。C owner 在 `9f1e3ad` 中为接近规划分配新身份，并保留原工作期限，随后通过独立审查。最短反例与修复后直接 **3/3** 输出分别保存在 `E1-b11-probe.log` 和 `E1-repair-direct.log`。

修复后的固定检查结果：

| 检查 | 实际结果 | 原始目录／日志 |
|---|---|---|
| 完整运动导航 | **1,186/1,186**，370.491 秒 | `E1-full-motion-after-repair.log` |
| 协调集合 | **1,448/1,448**，无检查失败 | `E1-migration-final/` |
| 补充故障入口 | **4/4** | `E1-faults-final/` |
| 固定 v7 | **1,710/2,000**，504.479 秒；无异常、安全违规或证据缺口 | `E1-product-final/` |

以上目录均位于 `.tmp/r28-3/`。v7 保留全部 2,000 项和原失败；成功数不代表产品门槛通过。

协调签名相对 R28-4 有 8 项差异，发现后暂停 fault／v7，先完成独立审查。五项仅取消时的 `controller_phase` 显示不同，实际身体、输入、窗口、来源释放和终态相同。三项重复修订在普通、完整支撑且已停止的地面多一 tick 中立等待：当前目标要求终端速度不超过 0.6 格／秒，而旧 Walk 出口证明上界为 4.3709697324，所以保守重算。它们均成功，严格保护和原 owner 释放前的输入不变，但实际规划提交增加，不能签成纯迁移等价。独立结论见 `coord-diff-review.md`；配对与第一处分歧见 `E1-migration-pair.json`、`E1-migration-diff-detail.log`。

相对 R28-4 的最新 v7，**1,981/2,000** 项在结果、原因、运动作业、输入、轨迹及补提取的规划提交／风险／交接字段上逐项一致。19 项差异全部属于 `target-late` 的重复修订；终态、原因和 motion jobs 保持一致，实际规划提交多 1 次（17 项）或 2 次（2 项）。相对冻结 v7，**1,976/2,000** 项相同；另外五项是 R28-4 已记录的晚到下降改善。本轮没有成功转失败。冻结 v7 仍为 **1,705/2,000**，不修改原记录。

第 5.3 节的体验门槛未通过，19 项差异已登记为停顿 **P1**，后续修正必须另建候选，不拼接本轮样本：

| `target-late`，200 项 | R28-4 | 本轮 |
|---|---:|---:|
| 成功 | 200 | 200 |
| 到达平均 tick／P95 | 79.685／95 | 79.695／95 |
| 实际规划提交 | 1,063 | 1,084（+1.9755%） |
| 控制切换 | 613 | 613 |
| 零位移停顿 tick／区间 | 22／5 | 43／12 |
| 有未满足需求的运动 tick | 14,392 | 14,393 |
| 零位移停顿率 | 0.152863% | 0.298756%（相对 +95.44097%） |
| 出现停顿的任务 | 5/200 | 9/200 |
| 修订响应 P95 | 7 tick | 7 tick |
| 有效响应／被后续修订覆盖／未回答 | 731／65／0 | 730／66／0 |

平均到达用时仅增加 0.0125%，P95 未变，但不能抵消停顿率退步。修订响应 P95 的既有 7 tick 也未达到绝对 5 tick 门槛。所有层的用时、失败结束用时、首次移动、工作数和控制切换保存在 `E1-v7-timing-detail.json`；19 项第一处分歧、实际提交 tick、原始风险与响应记录在同一文件。停顿分母来自原始流，见 `E1-v7-demand-rates.json`。模拟没有测量真实控制准备 P95≤8 ms，不把整个任务耗时当成该指标。

协调 collector 的 signature 没有保存实际 planning submissions、风险和 typed handoff；它还从 `asdict(VerificationAssessment)` 读取不存在的 `complete` 键，使 `verification_complete` 为 `None`。`passed` 已间接检查完整性，所以不是本次运行缺少验证；但这些字段不能支持它文字上宣称的全部比较。独立审查将此登记为 **P2 工具证据缺口**。本轮不扩工具、不改旧归档，从正式 v7 原始流补提取规划提交、风险账本与身体交接，详见 `E1-product-pair-r28-4.json` 和 `E1-product-pair-frozen.json`；协调集合仍保留上述证据边界。

A0 同一语法统计口径下，十个受影响生产文件总行数 **13,899→14,374**，字段 **661→674**，身份比较 **169→190**，决定分支 **1,569→1,648**，退场调用 **21→22**。Session 为 **4,587→4,653** 行、字段 **151→152**、分支 **574→584**。这些是定位用的语法统计，不等于长期权威状态数；但本轮没有净减少证据，结构门槛保持未通过。原始和复算文件为 `A0-complexity.json`、`E1-complexity-final.json`，后续由独立审查区分必要 typed contract 与尚未删除的重复分支，不能为降数字随意改代码。

现有运行器没有独立探索校准和两次查看批次调度。本轮没有用每层 200 对冒充首次至少 1,500 对，**产品统计非退步未签署**。停顿 P1、结构净减少、统计门槛和代表性 Fabric 均未关闭。本轮未运行 Fabric；未验证的代表场景开关已恢复到 HEAD，补丁仅留在 `.tmp/r28-3/E1-unrun-fabric.patch`。M3／B11 的既有实机证据不被本轮模拟替代。

本轮实际命令（均通过项目 `conda run --prefix ... --no-capture-output python`）：

```powershell
D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python -m unittest tests.motion_nav.test_r28_generation_formal_chain -q
D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python -m unittest discover -s tests/motion_nav -p 'test_*.py' -q
D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python scripts/navigation_migration_evidence.py --output .tmp/r28-3/E1-migration-final --workers 4
D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python scripts/navigation_migration_evidence.py --output .tmp/r28-3/E1-faults-final --workers 1 --faults-only
D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python scripts/navigation_coordination_metrics.py baseline --manifest tests/sim/manifests/navigation-product-r28-v7.json --output .tmp/r28-3/E1-product-final --workers 4
git diff --check
```

### 8.7 D1：两项有界规划工作与固定目标层检查

2026-10-04，D1 的实现与定向检查完成。每项规划工作各自保存依据、生命周期、快照和来件；当前目标、许可、局部失败、任务恢复和风险仍由原 owner 统一保存。目标修订有空位时立即准备最新工作。两个位置占满后，真实回执释放容量，再计算账本中的最新请求。正式 worker 保持单进程串行，两项请求和两项结果都逐项交付。

独立审查发现并修正三项边界：最新工作失败会误退场仍合法的旧工作；观察阶段的 RUNNING 缓存可能永久挡住来件；延后交付失败时，游标仍指向旧工作，导致 handoff 拒绝真实失败。现在保存完整失败依据和原通知，继续等待仍合法的工作；旧正结果完整适用时可以接纳，否则按真实已退场身份交付一次失败。观察更新只缓存当前运动 tick。正式 Runtime／Session／handoff 反例已证明失败能被接纳并有界终结。

本轮先复跑原 19 项差异，再重新冻结完整 `target-late` 单层。最终直接检查为 **205/205**，补充故障入口 **4/4**。固定目标晚到层为 **200/200**；零位移停顿恢复为 **22/14,392**，与 R28-4 对应层一致；规划提交为 **1,063→1,084（+1.98%）**；到达均值 **79.685 tick**、P95 **95 tick**、控制切换 **613** 均一致。该层的固定停顿、提交、切换和到达门槛通过，响应 P95 仍为 **7 tick**。原 E1 失败、独立审查反例和中断候选均保留。

十个生产文件的语法统计相对 E1 为：行数 **14,374→14,708**，字段 **674→683**，身份比较 **190→209**，退场调用 **22→28**，决定分支 **1,648→1,704**。两项 work 各有自己的状态，不能把它们包装成一个字段后宣称权威状态减少。结构净减少、绝对响应五 tick、真实前台 P95 八毫秒、代表性 Fabric 和统计非退步门槛继续开放；本批不关闭 R28-3。完整命令、源码指纹和证据边界由 E 的验收记录统一维护。

### 8.8 E：D1 最终候选的完整固定集合

2026-10-04，生产候选为 `2eb5f61f8f80456eb747594e36dee0ed7e53c29c`，D1 独立复审无 P0／P1。本轮重新运行整套候选证据，目录统一使用 `.tmp/r28-3/E2-*`；8.6 的 E1 失败与停顿结果继续保留，不拼接为本轮样本。运行前声明见 `E2-run-declaration.md`：目标修订正常／晚到是直接受影响层，按改变行为验收；其余固定 v7 层检查共同 worker 和身份门禁。比较结果、原因、实际规划提交、运动作业、输入、轨迹、风险、交接和安全，不改变原门槛。

| 按顺序执行的检查 | 结果 | 原始证据 |
|---|---|---|
| 11 项正式链 | **11/11**，3.513 秒 | `E2-chain-first.log` |
| 双 work／真实 worker／原 B11 与两个 patch 直接场景 | **31/31**，8.771 秒 | `E2-minimal.log` |
| 完整运动导航 | **1,202/1,202**，278.112 秒 | `E2-motion-final.log` |
| 协调固定集合 | **1,448/1,448** | `E2-migration-final/` |
| 补充故障入口 | **4/4** | `E2-faults-final/` |
| 完整固定 v7 | **1,710/2,000**，374.421 秒；无异常、安全违规或证据缺口 | `E2-product-final/` |

协调相对 R28-4 仅剩五项取消时的 phase 显示差异；输入、身体、窗口、来源释放及终态保持一致。E1 的三项多一 tick 等待已消失。配对及第一处分歧见 `E2-migration-pair.json`、`E2-migration-diff-detail.log`。8.6 记录的 collector 字段缺口仍开放，本轮不扩通用工具。

v7 相对 R28-4 的最新集合，19 项仍有实际规划提交差异（17 项多一次、两项多两次），全部属于 `target-late` 重复修订。**2,000/2,000** 的结果、原因、运动作业、实际输入、轨迹、风险和交接一致；仅把工作数也纳入签名时是 **1,981/2,000** 一致。相对冻结 v7 是 **1,976/2,000** 完整签名一致，另外五项为 R28-4 已记录的晚到下降改善，没有成功转失败；冻结 **1,705/2,000** 原结果不改。原始流哈希及外部字段配对见 `E2-product-pair-r28-4.json`、`E2-product-pair-frozen.json`。

`target-late` 的 200 项均成功。到达均值 **79.685 tick**、P95 **95 tick**、控制切换 **613** 与 R28-4 相同；实际规划提交 **1,063→1,084（+1.97554%）**。零位移停顿恢复为 **22 tick／5 区间**，需求分母 **14,392 tick**，停顿率 **0.152863%**，出现停顿的任务 **5/200**，均与 R28-4 一致。有效响应／被覆盖／未回答为 **731／65／0**，响应 P95 仍为 **7 tick**。E1 停顿 P1 在本固定集合中不再重现；绝对五 tick 响应门槛仍未达到。各层完成及失败结束用时、首次移动、提交、切换和首差异见 `E2-v7-timing-detail.json`；停顿分母见 `E2-v7-demand-rates.json`。

重新按 A0 口径计算十个生产文件：行数 **14,708**、字段 **683**、身份比较 **209**、退场调用 **28**、决定分支 **1,704**，与 D1 记录一致。相对 A0 的 **13,899／661／169／21／1,569** 没有净减少；Session 仍为 **4,653** 行、字段 **152**、分支 **584**。原始统计为 `E2-complexity-final.json`。结构门槛保持开放，必要的两项 typed work 状态与尚未删除的旧重复必须由独立审查分别判断，不能以包装字段掩盖增加。

现有工具没有独立探索校准和两次查看调度；本轮固定每层 200 对不满足首次至少 1,500 对要求。**产品统计非退步未签署**，不编造校准样本或统计结论。固定集合安全检查通过不等于 R28-3 全部关闭。

### 8.9 E：真实 Fabric 代表场景与准备成本

完整固定检查稳定后，才串行运行本地独立 Fabric。只给现有动作衔接脚本增加 R28-3 代表场景开关和真实规划 worker 记录，不改变 actor、输入仲裁、观察权限或安全窗口。worker 在夹具观察前准备；规划在途和双重修订启用已有的 **0.10 秒**调试延迟，仍由真实单进程串行执行，容量最多两项。普通修订保持 scope；空中取消换代后原身体 owner 继续收尾。

首次规划代表批次 `20261004T052053340872Z-864b0de3` 因测试记录 wrapper 缺少协议要求的 `poll_latest()` 而失败，尚未开始控制，原输出 `E2-fabric-planning-first.log` 和原批次保留。只补薄转发和协议检查，先复跑同一正常例，成功后再晚一 tick；没有改生产或放宽原断言。协议检查证明两项回执均原样交付，最终选项检查 **4/4**，见 `E2-fabric-options-final.log`。完整 **1,202** 项检查发生在增加这项测试之前，不把它记为未经运行的 1,203 项全量。

| 代表场景（方向 0，正常／首条晚一 tick） | 实际结果 | 正常／晚到准备 P95，ms | 批次 |
|---|---|---:|---|
| 规划在途修订后跨隙 | **2/2 success** | 2.2969／2.2160 | `20261004T052542012314Z-a2751d48` |
| 二格严格下降中修订 | **2/2 success** | 2.5268／2.4595 | `20261004T052853443448Z-ce639ff9` |
| 二格严格下降中取消 | **2/2 cancelled**，身体收尾完成 | 3.0054／3.0068 | `20261004T053229416787Z-a5d45462` |
| 两项在途时再次修订后跨隙 | **2/2 success** | 2.6531／2.5025 | `20261004T053510205519Z-949bdc62` |

八项均来源释放、零伤害、无窗口外或无人负责输入。四次晚到的真实首次应用 tick 为 **123→124、198→199、166→167、130→131**，不是只记录 sleep 注入。空中修订／取消事件均保留身体 owner；双重修订第二次事件记录了两项完整真实工作身份。八个真实 worker PID 共记录 **16 次提交、16 次回执**，所有原始身份、请求、状态和未完成数均保留。双修订复用现有跨隙夹具，代表 seed44 的“两项在途后再次修订”边界，不声称复现 seed44 全部地形或普遍跟随能力。

逐帧记录共有 **465** 个 navigation 准备样本，总体 P95 **2.5268 ms**、最大 **4.2138 ms**；P95 直接从全部原始样本取分位数，没有平均各场 P95。此限定热 worker 代表集合满足八毫秒门槛，不扩大到冷启动、所有方向或所有场景。原批次仍位于 `artifacts/fabric-deployment/`，选择的 summary、逐帧真实输入账本、规划与运动 worker 事件已复制并记录哈希到 `.tmp/r28-3/E2-fabric-final/`；统一索引为 `summary.json`。未重跑 M3／B11。

本轮实际命令除第 9 节全量命令（输出前缀改为 `E2-*`）外，还包括：

```powershell
D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python -m unittest tests.motion_nav.test_r28_generation_formal_chain -q
D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python -m unittest tests.motion_nav.test_action_continuity_fabric_options -q
# 四个代表批次依次使用 moving_gap/planning、drop_2/revision、drop_2/cancel、moving_gap/double。
D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python scripts/run_action_continuity_fabric.py --start-delivery --kind moving_gap --direction 0 --r28-3-operation planning --time-diagnostics --seed 21001 --server-port 25597 --ipc-port 8140 --timeout-seconds 600
git diff --check
```

本轮可确认固定迁移安全和上述代表场景；结构净减少、产品统计和绝对响应门槛仍打开。四类最终文档与 AGENTS 由 E2 根据独立最终审查更新，本事实记录不代替签署。

### 8.10 最终审查、历史窗口修复与开放门槛

最终全分支审查覆盖 `67461f7..2d5a4ec1`，报告为 `.tmp/r28-3/final-review.md`。审查认可计算世代、完整结果绑定、两项 work 容量、失败归属、新任务身份和身体／效果责任的限定安全与功能范围。E1 停顿 P1 在 E2 固定集合中不再重现；原退场历史 P2 由 `050c10ba5910a8629fb17c3a442cadbf4ff48e57` 修复并定向复审。当前没有未关闭 P0／P1／P2；旧失败、首次 Fabric 协议失败和 collector 证据缺口仍按原边界保留，collector 不属于本轮修复范围。

`_retired` 与 `_known_work_windows` 各最多保存 64 条，事件历史最多 128 条。淘汰跳过活动工作、待真实回执、延后失败和终态失败来源；全部受保护时返回 `PlanningHistoryCapacityExceeded`，保留原记录，也不消费新 permit、递增尝试或购买恢复。新增历史检查 **4/4**，覆盖 160 次修订、旧 receipt、延后失败归属和容量满时拒绝新工作。E2 完整集合及 Fabric 仍引用原候选源码，不冒充 `050c10b` 后的完整运行。

按 A0 相同 AST 口径重算修复后的十个生产文件，另存 `E2-complexity-after-050c10b.json`，不覆盖 `E2-complexity-final.json`：

| 语法清单 | A0 | E2 候选 | `050c10b` 后 | 最终相对 A0 |
|---|---:|---:|---:|---:|
| 行数 | 13,899 | 14,708 | 14,729 | +830 |
| 字段 | 661 | 683 | 683 | +22 |
| 身份比较 | 169 | 209 | 210 | +41 |
| 退场调用 | 21 | 28 | 28 | +7 |
| 决定分支 | 1,569 | 1,704 | 1,708 | +139 |

Session 仍为 4,653 行、字段 152、分支 584，相对 A0 为 +66／+1／+10。字段统计是语法清单，不等于长期权威状态数；但行数和决定分支确实没有净减少，完成标准 11 未通过。

旧路径核销与保留边界如下：

| 路径／状态 | 现状与检查边界 |
|---|---|
| 普通目标修订无条件取消并 `begin()` | 已由 `revise_request()` 和有空位时的最新 work 准备替代，原工作期限保留 |
| worker 跨请求序号选最新、latest 覆盖 | 正式链改为两槽逐项交付，完整 identity 路由；`poll_latest()` 只保留既有兼容调用，不承担正式双 work 接纳 |
| 旧否定套当前请求身份 | 已退出；只退场原 work，最新工作不存在时才按当前请求准备 |
| 规划门禁前重复活动 identity、许可诊断的同义比较 | 已删除指定重复；原生产者请求核验和当前领域事实检查继续保留 |
| motion 无 identity 的兼容授权与重复活动比较 | 已删除；原 proof、negative basis、REVALIDATE、命令窗口和 LocalAttemptChain 保留 |
| 原单 work 的计算状态 | 收进各自 `_PlanningWork`；目标、许可、恢复、风险和桥接业务仍由唯一 owner 保存 |
| 无界退场历史 | `050c10b` 已恢复有保护身份的 64 条上限；不是删除所有失败记录 |
| successor 隐含新任务约定 | 已替换为显式 `task_id` 契约；同任务继续只走 continuation |

后续仍须逐项判断私有推进与公开入口的重复校验，以及残留协调分支是否可删除。不可把必要的 scope、不可变 basis、工作期限／candidate／receipt 或失败来源当成重复删掉，也不能靠移到技能层、压成集合或改写表达式制造减少。

**R28-3 全阶段不关闭。** 固定集合和八项 Fabric 支持限定安全／功能交付；统计校准及两次查看未做，产品非退步未签署；绝对响应 P95 **7>5 tick**。结构净减少仍阻塞原定义的关闭。下一步先作结构收敛取舍；若选择留给 R28-5，必须显式修改 D048、总计划、本计划和验收的范围与顺序，保留原数字和删除清单。当前记录不作该例外决定，也不提前启动 R28-5 或正式跟随。正式跟随、室内接近、长期运行及整体 R28 仍打开。正式证据、命令和最终边界见[验收第 19 节](../acceptance/navigation-coordination-convergence.md#19-r28-3-异步计算世代与当前目标绑定)。

## 9. 现行检查命令

专项稳定后使用：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest discover -s tests/motion_nav -p 'test_*.py' -q
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/navigation_migration_evidence.py --output .tmp/r28-3/migration-final --workers 4
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/navigation_migration_evidence.py --output .tmp/r28-3/faults-final --workers 1 --faults-only
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/navigation_coordination_metrics.py baseline --manifest tests/sim/manifests/navigation-product-r28-v7.json --output .tmp/r28-3/product-final --workers 4
git diff --check
```

实际命令、样本数量、源码指纹、失败和证据边界只在验收文档中记录，不能提前写成通过。
