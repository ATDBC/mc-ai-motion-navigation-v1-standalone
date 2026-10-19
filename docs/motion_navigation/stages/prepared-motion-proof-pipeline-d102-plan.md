# D102 提前准备与可信注册运动证明 Implementation Plan

> 2026-10-13：本计划止于 D103 G2-F2 Task C。Task D、旧 Task 10、G3、F-TICK 与 F-CONTINUITY 不再按本文顺序实施。后续使用 [D104 关键边界证明与连续动作计划](key-boundary-motion-proof-and-continuity-plan.md)；本文保留为历史计划。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

2026-10-12。状态：**实施计划草案，尚未实施。** 规格基线为 `3668cb3c1fc55c9ba44a36208fecafa0286cb8ee`。本次只写计划，不改生产代码、不启动 Fabric、不提交。任务之间的门槛表示先后依赖，不表示自动获得下一阶段授权；执行前以用户明确授权为准。

**Goal:** 在现有 motion 服务内提前完成完整证明、后台校验与登记，使 `Walk→JumpGap→Walk→JumpGap` 在真实交接处只比较已有依据，并通过 Windows 正式链和两阶段 Fabric 验收。

**Architecture:** Runtime 的正式知识提供稳定局部快照和租约；现有 `MotionRouteCoordinator` 提供固定输入、请求与输入流 generation；现有 worker 计算，现有 inbox 的唯一 receiver 验真并登记。registry 单独记录 candidate／executing／safe-tail 用途；监督者决定同身体 owner 的动作交接。首条命令赢得 Runtime 仲裁后产生 binding，先无副作用准备，再由 registry 接纳并交付 binding，最后在短 commit 边界推进动作及转换用途，完成后才发送。

**Tech Stack:** 项目 Python 3.11.16、`unittest`、Windows `multiprocessing` spawn、现有 Java 1.21 物理计算器、Java 21／Fabric deployment probe、JSON／JSONL、SHA-256。

**Spec:** [D102 决定](../decisions/0102-prepare-and-register-motion-proof-ahead-of-handoff.md)、[提前准备与可信注册架构 v1](../architecture/prepared-motion-proof-pipeline-v1.md)、[D102／D103 隔离验收](../acceptance/prepared-motion-proof-spikes-d102-d103.md)；同时遵守 D089、D099—D101 和[按风险连续运动架构](../architecture/risk-aware-continuous-motion-v1.md)。实施事实必须以代码、测试和 raw 为准。

## Global Constraints

- Windows 是正式平台。完整控制决定要求 **P95≤8 ms、P99≤15 ms、最大值<30 ms**；按 nearest-rank 求分位数。
- 热路径 scan／搜索／完整 proof hash／反序列化／整份快照构造调用必须为 0；序列化、整段物理回放与长时间锁等待也不得进入热路径。
- 完整决定必须包含本阶段实际观察接收、handle 查询、当前比较、账本／回执、预算、交接、仲裁和发送；不能靠切分阶段漏掉控制成本。
- 复用 `MotionSolverWorker`／`MotionWorkerPort`、`MotionResultInbox`、现有计算身份与退场；只有一个结果消费者。不得复制 D103 pipeline／registry 或创建第三条异步管线。
- Runtime／正式知识唯一拥有世界；后台只借不可变 facts。UNKNOWN 不补 AIR，不读取 TEST_ORACLE，不重建 fixture，不修改物理公式。
- `MotionRouteCoordinator` 唯一创建／换代正式 stream；输入、入口、许可或安全后缀改变才换代，目标 revision 本身不换代。
- 准备请求不预留实际 control sequence。首条序号与复合命令只由 Runtime 在最终仲裁／发送边界绑定；activation 不修改 bundle 或重算完整摘要。
- 激活／交接的 prepare 与许可只形成不可变事务事实，不推进 action、不换 owner、不转换执行用途。registry 接纳失败保留旧 action／owner／executing／safe-tail；接纳、binding 交付全部成立后才 commit。commit 后 backend send 异常仍可能已发送，必须保留输入与收尾责任。
- 0／1 tick 只在流起点；首条实际应用立即剪枝。中段晚到、缺失或内容不同属于偏离。
- 当前 owner 不因取消、scope 失效、worker 死亡或 close 自动释放身体；没有命令终态或监督者释放时保留执行／尾迹依据。
- 只新增准备、登记、交接与证据接口；正式 Python 路线仅 `Walk→JumpGap→Walk→JumpGap`，不扩动作类型、不优化寻路、不建普通地面输入包执行器。
- 普通宽地面继续只用 `FixedRouteController`；不调整 `MotionResidualThresholds`、GoalState、任务伤害额度、UNKNOWN 规则或现有客户端许可窗口。
- 本阶段冻结精确入口。偏差矩阵每个起点独立完整准备，不能把三个采样点写成一份 proof 覆盖连续 ±0.05 范围；真实身体不匹配就失效并从真实状态处理。
- 行为分支读取 movement tick 与 typed 事实；墙钟只用于节拍、度量和有界资源关闭。不得把“提前 3 tick”写成统一交付保证。
- F-TICK 先测真实顺序，只有门槛通过后才允许 F-CONTINUITY。若实测要求客户端预排程／generation 队列／撤销替换协议，停止并另写决定，本计划不顺手实施。
- 每项按 RED→确认 RED→最小实现→GREEN→小提交执行；同一任务最多两轮限定修正。硬安全失败立即止损，回归失败或性能门槛失败阻止签署，保留全部失败 raw。
- 原 D101 M0 **整体未通过**，P99／最大值为 48.6079／57.5597 ms；M1／M2 历史未实施、未运行，Fabric 未运行。D098 P3 原失败保留。D102／D103 APPROVED 仅签署各自隔离范围，不能改写这些历史。

## Review Focus

- 合法替换 inputs 并重新封存 proof 仍须拒绝，不能只检查 bundle 内部一致；Task 4／5 的 `test_other_legal_inputs_resealed_are_rejected` 固定这个反例。
- 取消发生在完整验真与原子发布之间时，旧结果不能重新出现；Task 5 的 `test_cancel_wins_at_publish_barrier` 与反顺序测试使用 barrier。
- 当前动作继续发输入时，准备器不得预占序号；同帧战斗／视角不能改变已证明运动；Task 8 的 `test_global_sequences_continue_during_preparation`、`test_composite_motion_projection_must_match` 验证。
- 计算关闭后空中 owner 仍需依据，缺任一命令终态／监督者释放不得回收 lease；Task 6 的 `test_close_keeps_tail_until_all_release_facts` 覆盖。
- Fabric 时间线缺事件、重复、关联错或直接相减不同进程时钟时，证据不完整；Task 11 的 `test_incomplete_or_cross_clock_trace_cannot_pass` 覆盖。

---

## 文件职责与共同接口约定

新增模块只承担明确职责：`mc2p/contracts/motion_activation.py` 保存 Runtime 激活的低层值与端口；`mc2p/motion_nav/prepared_motion.py` 保存准备／证明／handle 的不可变值；`world_snapshot.py` 实现正式知识的版本与 lease；`prepared_motion_proof.py` 提供固定输入的纯 scanner；`prepared_motion_validation.py` 提供 receiver 调用的纯完整校验。线程、pending、登记与关闭仍放在现有 `motion_worker.py`。

依赖方向固定为低层激活契约→准备值／物理类型→纯证明与校验→现有 inbox／协调器。executor 通过低层端口消费已登记引用，不 import `MotionResultInbox` 或协调器；supervisor 经 `RouteControl` 接口处理动作，不直接 import 具体 executor。WorldKnowledge 不 import Session／worker。纯模块不拥有线程、pending 或任务状态。

本文列出的新增接口均为实施目标，当前不存在。共有类型在 Task 2 定义，后续任务不得更名或自行增加另一个 owner。`DependencyVersions = tuple[tuple[BlockPos, int], ...]`；`Digest = str` 为 64 位小写 SHA-256；`JobToken`／`ProofToken`／`LeaseToken` 为不可复用字符串。`FirstStreamApplication` 只表示已实际采样／应用事实，不表示预测。

所有命令从 `D:\My_project\mc_ai` 在 PowerShell 运行，使用 `& .\.venv\python.exe`。新命令只能在定义它的任务完成后运行；计划撰写不运行这些命令。单元测试默认只在 `TemporaryDirectory` 中写夹具／证据，结束即清理，不写固定 `.tmp` 路径。只有显式证据模式可以写外部新建的唯一 run 目录；非空目录返回 `OUTPUT_EXISTS`，不得覆盖。verifier 的 `--input` 可接受 summary 或只读阶段目录，但 G1 必须只读一次唯一 run 的 `index.json`，按索引读取其列明文件，不扫描共享 g1 目录或混入旧运行。导出检查的所有证据／输出路径必须在导出树之外；导出树内路径立即拒绝。阶段目录模式不重写 raw 或伪造尚不存在的父级 summary。

容量作为本次限定组合的配置冻结：全服务未完成作业最多 2（包括排队、运行、接纳，不是仅 queue 大小）；请求／结果 FIFO 各 2；登记 bundle 最多 8；snapshot lease 最多 8；局部快照最多 16,384 格；单份封存请求／结果各最多 2 MiB；保留的 snapshot／request／proof／transport bytes 总量最多 16 MiB；诊断历史 64。bytes 按同对象只记一次，计数同时覆盖在途校验。第 3 项作业 typed `QUEUE_FULL`；承担责任的引用不能被容量驱逐。固定输入扫描预算为物理步 65,536、轨迹 40 tick、起点分支 2、每边界尾迹最多 80 tick，不增加搜索能力。

## 阶段门槛

| 门槛 | 完成任务 | 可以声称什么／下一步 |
|---|---|---|
| G0 | 1 | 验收范围和 RED 已冻结，尚无实现通过。 |
| G1 | 2—8 | 契约、快照、同服务登记、引用、交接和 Runtime 激活的直接测试通过；不声称正式性能。 |
| G2 | 9 | Windows 隔离功能、真实 spawn／20 Hz 性能、欠载与资源门槛通过，才可接正式 Python 单一组合。 |
| G3 | 10 | 正式 Python 限定组合、受影响产品族、正逆全量和正式性能通过，才可进入 F-TICK。 |
| F-TICK | 11 | 40／40 时间线完整并可复算；更晚 tick 也是合法分类，不强制 NEXT_MOVEMENT_TICK。 |
| F-CONTINUITY | 12 | F-TICK 通过且逐 tick 输入协议足够时，24 正例与 4 欠载反例完成。 |
| G4 | 13 | 独立导出运行、证据重算与文档收尾完成；只签署实际通过范围。 |

### Task 1: 冻结正式验收、五组 RED 与证据 schema

**Files:**
- Create: `docs/motion_navigation/acceptance/prepared-motion-proof-pipeline-d102.md`
- Create: `tests/motion_nav/d102_contract_cases.py`
- Create: `tests/motion_nav/test_d102_contract_red.py`
- Create: `tests/motion_nav/test_d102_evidence_contract.py`
- Create: `tests/sim/manifests/prepared-motion-proof-d102-v1.json`
- Create: `scripts/verify_prepared_motion_d102.py`

**Interfaces:**
- Consumes: 本计划、D102 §2—13、最新 §5.5 激活契约及原证据哈希。
- Produces: `D102_STAGE_ORDER = ("G0", "G1", "G2", "G3", "F-TICK", "F-CONTINUITY", "G4")`；`verify_stage_report(document: dict, *, stage: str) -> tuple[str, ...]`，空元组才合格；CLI `--stage STAGE --input PATH [--verify-red]`，成功 0、证据缺失／错配 1。测试共用 `write_contract_evidence(case_id: str, facts: dict, *, output: Path) -> Path`，普通测试传 TemporaryDirectory；显式 `--record-contracts --output UNIQUE_RUN` 才运行直接契约证据批次并写一次索引，加 `--verify-red` 时只采冻结五组 RED。索引列完整 case ID、相对路径、哈希、线程／激活／引用事件、环境与唯一 run ID。G1 只读该索引验证；不能用 unittest 文字日志或共享目录扫描代替。

- [ ] **Step 1: 写失败测试。** `EvidenceContractTests.test_missing_raw_or_activation_cannot_pass` 删除 raw／activation 关联，断言 verifier 非空；`test_historical_failure_cannot_be_promoted` 断言 D101 M0／D098 P3 仍为失败；`test_default_unittest_never_writes_workspace_evidence`、`test_explicit_run_is_unique_and_g1_reads_only_its_index` 固定证据隔离。`ContractRedTests` 的五组为 `test_parent_inputs_are_independent`、`test_snapshot_graph_is_immutable`、`test_control_drain_is_not_receiver`、`test_job_retirement_preserves_execution_refs`、`test_overdue_keeps_registered_tail`。先用现有正式端口的薄适配器触发旧行为，记录断言失败；不得用只捕获 ImportError 或 `skip` 代替 RED。

```python
self.assertEqual(report["historical_d101_m0"], "FAILED")
self.assertEqual(report["historical_d098_p3"], "FAILED")
self.assertEqual(rejected["unauthorized_body_steps"], 0)
```

- [ ] **Step 2: 确认 RED。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_contract_red tests.motion_nav.test_d102_evidence_contract -v`。Expected: 验收 verifier 尚缺或旧行为断言失败，退出 1；普通测试的工作文件只在 TemporaryDirectory。正式 RED 证据由显式证据模式写到 `.tmp/d102-formal-v1/red/<唯一 run ID>/`，保存每组完整错误、输入哈希、命令和退出码。后续每个 owner 任务还须写其具体行为 RED。
- [ ] **Step 3: 实现验收与证据校验。** 在新 acceptance 和 manifest 冻结矩阵、分母、容量、精确入口、历史状态、门槛、raw schema；实现 `verify_stage_report`，检查源码／输入／快照／proof 哈希、schema、命令、线程、引用、激活、实际发布／消费和失败原因。此任务不实现 registry 或控制器。
- [ ] **Step 4: 检查 G0。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_evidence_contract -v`，然后显式采集并验证唯一 RED run：

```powershell
$d102RedRun = Join-Path 'D:\My_project\mc_ai\.tmp\d102-formal-v1\red' ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ') + '-' + [Guid]::NewGuid().ToString('N'))
& .\.venv\python.exe scripts/verify_prepared_motion_d102.py --record-contracts --verify-red --output $d102RedRun
& .\.venv\python.exe scripts/verify_prepared_motion_d102.py --stage G0 --input (Join-Path $d102RedRun 'index.json') --verify-red
```

Expected: 证据校验 PASS／退出 0，五组已确认 RED 的状态明确保留；不把整体测试写成全绿。
- [ ] **Step 5: 提交冻结验收与 RED。** `git add -- docs/motion_navigation/acceptance/prepared-motion-proof-pipeline-d102.md tests/motion_nav/d102_contract_cases.py tests/motion_nav/test_d102_contract_red.py tests/motion_nav/test_d102_evidence_contract.py tests/sim/manifests/prepared-motion-proof-d102-v1.json scripts/verify_prepared_motion_d102.py`，然后 `git commit -m "test(motion): freeze D102 acceptance and contract RED"`。

**止损条件：** RED 仅是导入失败、历史失败被改写、矩阵／容量未冻结或 verifier 能接纳缺证据时，不进入 Task 2。Task 1 的 RED-only 提交是明确暂存状态，不能当成阶段成功或最终可交付提交。

### Task 2: 不可变准备值、输入流与激活契约

**Files:**
- Create: `mc2p/contracts/motion_activation.py`
- Create: `mc2p/motion_nav/prepared_motion.py`
- Create: `mc2p/motion_nav/input_stream.py`
- Create: `tests/motion_nav/test_d102_contracts.py`
- Modify: `mc2p/motion_nav/__init__.py`（只导出正式公开值）

**Interfaces:**
- Consumes: `AsyncWorkIdentity`、`PhysicsState/Ruleset/TickInput`、`MotionCommandTick`、`CandidateExecutionWindow`、`TaskDamageBudget`、已有低层 movement／action 契约。
- Produces: 下列 frozen 值及 typed 结果；构造均拒绝可变内部图和矛盾字段。
  - `StreamGeneration(generation_id, owner_id, request_id, route_id, action_index, expected_inputs, entry_identity, requested_window, suffix_identity)`；**没有实际 first sequence 字段**。
  - `StreamActivationBinding(proof_token, generation_id, owner_id, ledger_id, world_session_id, first_control_sequence, requested_window, submitted_movement_tick, compound_action)`；`compound_action: ActionSnapshotV1` 是实际最终命令，不含伪造 effect tick；`requested_window: MovementTickWindowV1` 使用既有低层类型，不使 contracts 反向 import motion_nav。
  - `StreamActivationProposal(proof_token, generation_id, owner_id, ledger_id, world_session_id, movement_intent_id, expected_movement, expected_yaw_radians, requested_window, handoff_token)`，其 `requested_window` 同为 `MovementTickWindowV1`；准备侧 `CandidateExecutionWindow` 与它按两个绝对 tick 数值对应，不比较不同类型对象相等。
  - `FirstStreamApplication(generation_id: str, control_sequence: int, effect_tick: int, movement: MovementV1, movement_yaw_radians: float)`。
  - `PreparedStreamActivationPort.prepare_activation(binding: StreamActivationBinding) -> PreparedActivationTransaction | ActivationResult`；`accept_activation(transaction: PreparedActivationTransaction) -> PreparedActivationAcceptance | ActivationResult`；`deliver_activation(transaction: PreparedActivationTransaction, accepted: PreparedActivationAcceptance) -> PreparedActivationDelivery | ActivationResult`；`commit_activation(transaction: PreparedActivationTransaction, accepted: PreparedActivationAcceptance, delivered: PreparedActivationDelivery) -> ActivationResult`；`record_application(application: FirstStreamApplication) -> StreamApplicationResult`。三种新纯值只绑定事务／接纳 token、binding、旧／新 action、许可与交付确认，不是第二套 pending 或身体状态；prepare 无副作用，接纳／交付尚不推进动作或转换用途，commit 才完成。`PreparedStreamDispatch(proposal: StreamActivationProposal, port: PreparedStreamActivationPort)` 是本进程短期借用端口，不序列化、封存或跨线程存活；数据 bundle 不含 owner 对象。
  - `WorldSnapshotVersion`：Runtime 发布的不可变 section/fact 引用、bounds、session、source geometry/evidence revision、local revision 与逐格语义版本；尚未 seal 时不声称有 snapshot digest。`SnapshotIdentity`：同版来源、local revision、digest、ruleset、bounds；`SnapshotLease`：lease token、identity、不可变 facts 与逐格语义版本；`SnapshotLeaseResult`：`READY/NOT_READY/CAPACITY`。已发布 lease 必须带已完成的正式 digest。
  - `PreparedMotionRequest`：不可复用 job token、完整 Async identity、stream、route／action／goal 修订、commands、父侧 expected inputs／digest、精确 entry／exit、窗口／期限、ledger/session、已发生边界应用、snapshot identity／lease token、继承依赖、ruleset、伤害余额、固定扫描预算和安全尾迹要求。
  - `PreparedSafetyTail`、`PreparedBranchProof`、`PreparedMotionProof`：主轨迹、0／1 起点、每边界停车／落地依据、承诺／最后可放弃点、依赖 facts／versions、完整 ruleset、预算计数。
  - `PreparedMotionBundle(request, inputs, input_digest, proof, proof_digest, snapshot_identity, ruleset, sealed_bytes)`；`PreparedMotionHandle(proof_token, request_id, generation_id, owner_id, route_id, action_index, registered_digest, dependency_versions, hot_view)`；`hot_view` 为同 bundle 的只读有界比较视图，不复制权威身体／路线。
  - `PreparedMotionHotView`：精确 entry／关键节点、已预建起点分支、主输入／有限尾迹的只读引用、对应依赖／伤害／许可／movement-tick 期限。`PreparedCurrentFacts`：借用的当前 scope、stream identity、身体 owner、世界／规则／依赖变化投影、ledger/session、现有预算余额；不保存第二份 owner 状态。
  - `PreparedSubmitResult`：`ACCEPTED/QUEUE_FULL/CLOSED`；`PreparedComputeResult`：`PREPARED/MISSING_INFORMATION/UNSUPPORTED/BLOCKED/BUDGET_EXHAUSTED/WORKER_FAILED`；`PreparedAdmissionResult`：`REGISTERED/STALE/CANCELLED/REJECTED`；`PreparedQueryResult`：`READY_MATCH/NOT_READY/OVERDUE/ENTRY_MISMATCH/DEPENDENCY_CHANGED/TIMING_MISMATCH`。
  - `ActivationResult`：`ACTIVATED/NOT_SELECTED/ACTIVATION_MISMATCH/STALE_GENERATION`；`PreparedRejectReason` 区分 `MESSAGE/PROOF/MUTABLE_GRAPH/REQUEST/GENERATION/OWNER/ROUTE_ACTION/INPUTS/LEDGER/RULESET/WORLD_SNAPSHOT/DEPENDENCY`。
  - `StreamApplicationResult`：`APPLIED/REJECTED/DEVIATION`、唯一已选 `ON_TIME/LATE_ONE` 分支和 typed 原因；`InputStreamCommandFact(sequence: int, generation_id: str, command_index: int)` 只记录实际序号关联。
  - `InputStreamTracker.start(generation: StreamGeneration) -> InputStreamTracker`；`bind_activation(binding: StreamActivationBinding) -> ActivationResult`；`record_first_application(application: FirstStreamApplication) -> StreamApplicationResult`；`record_application(application: FirstStreamApplication, *, command_index: int) -> StreamApplicationResult`；`expected_effect_tick(command_index: int) -> int`。

- [ ] **Step 1: 写失败测试。** `ContractsTests.test_preparation_has_no_actual_sequence`、`test_activation_does_not_mutate_bundle`、`test_nested_mapping_or_list_is_rejected`、`test_first_receipt_selects_only_start_branch`、`test_second_command_cannot_reselect_late_branch`、`test_duplicate_or_late_two_receipt_is_rejected`。断言首次 tick 为 requested 或 requested＋1，中段只接受该偏移；字段相同假 handle 此时仅是值，不获得登记资格。

```python
self.assertFalse(hasattr(request, "first_control_sequence"))
self.assertEqual(tracker.expected_effect_tick(1), actual_first_tick + 1)
self.assertEqual(bundle, before_activation)
```

- [ ] **Step 2: 确认 RED。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_contracts -v`。Expected: 新契约／行为缺失，退出 1；保留具体失败断言。
- [ ] **Step 3: 实现上述值和 tracker。** 校验完整对象图；未来输入用 generation＋index，已应用输入才有真实序号。实际 generation 生产权限留给 Task 7，activation 生产权限留给 Task 8；此任务不启线程或登记服务。
- [ ] **Step 4: GREEN 与原协议回归。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_contracts tests.motion_nav.test_d101_input_stream tests.motion_nav.test_b10_online_motion -q`。Expected: 零失败／错误／跳过；0／1 语义未放宽。
- [ ] **Step 5: 小提交。** `git add -- mc2p/contracts/motion_activation.py mc2p/motion_nav/prepared_motion.py mc2p/motion_nav/input_stream.py mc2p/motion_nav/__init__.py tests/motion_nav/test_d102_contracts.py`；`git commit -m "feat(motion): define prepared proof and activation contracts"`。

**止损条件：** 准备预占序号、内部可变、三个身份合并或以 reason 字符串改变权限时停止。

### Task 3: Runtime 拥有的局部不可变版本和 snapshot lease

**Files:**
- Create: `mc2p/motion_nav/world_snapshot.py`
- Create: `tests/motion_nav/test_d102_world_snapshot.py`
- Modify: `mc2p/motion_nav/world_model.py:408`、`runtime_adapter.py:215`、`physics_adapter.py:122`
- Modify: `mc2p/runtime/player_runtime_v1.py:84`（世界端口接线，不新增世界 owner）

**Interfaces:**
- Consumes: Task 2 snapshot 值、现有 `CellFact`、正式观察变化、`PhysicsWorldBounds`。
- Produces: `WorldKnowledge.borrow_snapshot_version(bounds: PhysicsWorldBounds, *, ruleset: PhysicsRuleset) -> WorldSnapshotVersion | None`；`publish_sealed_snapshot(version: WorldSnapshotVersion, sealed: SealedSnapshot) -> bool`；`acquire_published_snapshot(bounds: PhysicsWorldBounds, *, ruleset: PhysicsRuleset) -> SnapshotLeaseResult`；`release_snapshot(lease_token: str) -> bool`；`dependency_versions(positions: tuple[BlockPos, ...]) -> DependencyVersions`；`dependency_changes(versions: DependencyVersions) -> DependencyCheck`；`seal_snapshot_version(version: WorldSnapshotVersion, ruleset: PhysicsRuleset) -> SealedSnapshot`；`SnapshotLease.sealed_snapshot() -> SealedSnapshot`；`PhysicsWorldView.from_snapshot(snapshot: SealedSnapshot) -> PhysicsWorldView`。`DependencyStatus` 为 `UNCHANGED/CHANGED/HISTORY_INCOMPLETE`，`DependencyCheck(status: DependencyStatus, changed_positions: tuple[BlockPos, ...])`；`SealedSnapshot(identity: SnapshotIdentity, canonical_bytes: bytes, facts: tuple[tuple[BlockPos, CellFact], ...], dependency_versions: DependencyVersions)`。

- [ ] **Step 1: 写失败测试。** `SnapshotTests.test_old_lease_survives_new_observation`、`test_revision_digest_and_facts_are_one_version`、`test_unknown_is_not_air`、`test_unrelated_cell_change_keeps_dependency_valid`、`test_related_change_invalidates_without_reclaiming_lease`、`test_control_acquire_never_builds_or_serializes`、`test_stamp_refresh_does_not_fake_geometry_change`、`test_capacity_returns_not_ready_without_waiting`；测试 BLOCK／AIR、非 full-cube、材料差异和会话更换。

```python
self.assertEqual(old_lease.cell(position), old_fact)
self.assertEqual(current_check.status, DependencyStatus.CHANGED)
self.assertEqual(control_counts["copy"] + control_counts["serialize"], 0)
```

- [ ] **Step 2: RED。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_world_snapshot -v`。Expected: 旧活视图／detached 可变与新接口缺失触发断言，退出 1。
- [ ] **Step 3: 最小实现。** 在 WorldKnowledge 用 section／局部版本按变化发布不可变 facts 与逐格语义版本；不每 tick 复制全地图。Task 5 的同一 receiver 在现有准备记录中借 `WorldSnapshotVersion`，后台导出／seal 后交回世界 owner 发布，不另建快照 pending 队列或资源管理者。已发布 ready lease 的 identity／bytes／digest 来自同一版；acquire 只读已发布引用，范围不足返回 NOT_READY，协调器保持原有待准备工作。请求发给 worker 前才绑定该 ready lease；不能提前用空 digest 填请求。lease release 只减存储引用。PhysicsWorldView 的私有 shape cache 不进入 signed bundle 图，spawn 不携带 live owner、锁或 MappingProxyType。
- [ ] **Step 4: GREEN。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_world_snapshot tests.motion_nav.test_world_knowledge_geometry tests.motion_nav.test_runtime_adapter tests.motion_nav.test_b10_gap_solver tests.motion_nav.test_runtime_backend_io -q`。Expected: 旧 UNKNOWN／材料／观察语义保持；所有新 lease 条件通过，热线程复制／序列化／digest 计数为零。
- [ ] **Step 5: 提交。** `git add -- mc2p/motion_nav/world_snapshot.py mc2p/motion_nav/world_model.py mc2p/motion_nav/runtime_adapter.py mc2p/motion_nav/physics_adapter.py mc2p/runtime/player_runtime_v1.py tests/motion_nav/test_d102_world_snapshot.py`；`git commit -m "feat(motion): publish Runtime-owned immutable snapshot leases"`。

**止损条件：** 混合 revision、未知补 AIR、同 section 无关变化全部作废、控制线程临时 seal 或 lease 提前回收时，不启动 worker 实测。

### Task 4: 同 worker 的固定 inputs 完整证明与封存

**Files:**
- Create: `mc2p/motion_nav/prepared_motion_proof.py`
- Create: `tests/motion_nav/test_d102_prepared_proof.py`
- Modify: `mc2p/motion_nav/motion_worker.py:24`、`motion_solver.py:534`
- Reference only: `experiments/motion_navigation/trajectory_proto/commitment.py`、`physics.py`、`contracts.py`

**Interfaces:**
- Consumes: Task 2 request／proof 值、Task 3 sealed snapshot，既有 1.21 `step`、支撑查询和伤害函数。
- Produces: `prepare_fixed_motion(request: PreparedMotionRequest, snapshot: SealedSnapshot) -> PreparedComputeResult`；`seal_bundle(bundle: PreparedMotionBundle) -> PreparedWireMessage`；`PreparedWireMessage(job_token: str, schema: str, payload: bytes, payload_digest: str)`；`PreparedMotionJob(request: PreparedMotionRequest, snapshot: SealedSnapshot)`；`MotionJobOperation.PREPARE`。保留 `MotionWorkerPort` 的四个方法名：`submit(job: GapMotionSolveJob | PreparedMotionJob) -> bool`，`poll_available() -> tuple[GapMotionSolveResult | PreparedWireMessage, ...]`、`close()`、`is_alive()`；inbox 将 bool 提交结果映射为 typed 状态，不新增必需方法使所有旧 inline ports 失效。固定输入扫描只有一个请求预算计数者，0／1 两分支、主轨迹与全部尾迹共同消费预算。

- [ ] **Step 1: 写失败测试。** `ProofTests.test_same_fixed_inputs_match_frozen_scanner`、`test_each_boundary_has_valid_tail_and_dependencies`、`test_one_budget_covers_branches_and_tails`、`test_unknown_or_unsupported_is_typed`、`test_future_inputs_do_not_invent_sequences`、`test_wrong_or_other_legal_inputs_are_not_substituted`、`test_spawn_uses_transmitted_snapshot`。选原 `jump_gap_1:A3` 输入及一个非 full-cube／材料拒绝样本，对比非计时 proof 字段与计数。

```python
self.assertEqual(result.bundle.inputs, request.expected_inputs)
self.assertLessEqual(result.counts.physics_steps, 65_536)
self.assertEqual(worker_fixture_read_spy.call_count, 0)
```

- [ ] **Step 2: RED。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_prepared_proof -v`。Expected: PREPARE／完整尾迹入口尚缺，退出 1。
- [ ] **Step 3: 实现固定输入 scanner 和原 worker 操作。** 只迁移已核验 scanner 的纯计算语义到正式模块，使用既有物理与风险函数，不复制公式或实验 pipeline。future 边界用 generation＋index，不能保留实验 request 的伪首序号。已有 SOLVE 可以选择动作输入；父侧协调器独立授权后，再以新 PREPARE 请求计算，SOLVE 结果本身不作为已登记资格。
- [ ] **Step 4: GREEN／spawn。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_prepared_proof tests.motion_nav.test_b10_motion_worker tests.motion_nav.test_incremental_motion_proof -q`。Expected: 正常固定 proof 等价、计数明确、typed 负例、一个持久 spawn worker，无新增搜索或 fixture 读取。
- [ ] **Step 5: 提交。** `git add -- mc2p/motion_nav/prepared_motion_proof.py mc2p/motion_nav/motion_worker.py mc2p/motion_nav/motion_solver.py tests/motion_nav/test_d102_prepared_proof.py`；`git commit -m "feat(motion): prepare fixed-input proofs in the existing worker"`。

**止损条件：** worker 自换 inputs、预算分开购买、尾迹缺失、只保留 release-safe indices 冒充完整 proof 或正式模块 import 实验 pipeline 时停止。

### Task 5: 现有 inbox 的唯一后台 receiver 与原子登记

**Files:**
- Create: `mc2p/motion_nav/prepared_motion_validation.py`
- Create: `tests/motion_nav/test_d102_receiver.py`
- Modify: `mc2p/motion_nav/motion_worker.py:137`、`:207`

**Interfaces:**
- Consumes: Tasks 2—4、父侧不可变请求与借用的当前 scope／stream／世界依赖投影。
- Produces: `validate_prepared_message(message: PreparedWireMessage, expected: PreparedMotionRequest, snapshot: SealedSnapshot, current: PreparedCurrentFacts) -> PreparedAdmissionResult`；`MotionResultInbox.prepare_snapshot(bounds: PhysicsWorldBounds, *, ruleset: PhysicsRuleset, identity: AsyncWorkIdentity) -> SnapshotLeaseResult` 在现有 active 准备记录中安排 Task 3 借版本／seal／发布；`register_preparation(request: PreparedMotionRequest, lease: SnapshotLease) -> PreparedSubmitResult`；`query_prepared(identity: AsyncWorkIdentity, *, movement_tick: int) -> PreparedQueryResult`；`retire(identity: AsyncWorkIdentity) -> None` 仍只退场计算。`PreparedCurrentFacts` 为原 owner 发布的不可变当前事实视图，不另存权威 scope／generation。

- [ ] **Step 1: 写失败测试。** `ReceiverTests.test_other_legal_inputs_resealed_are_rejected`、`test_three_way_ruleset_after_reseal_is_rejected`、`test_all_binding_fields_are_independently_checked`、`test_mutable_graph_is_rejected`、`test_cancel_wins_at_publish_barrier`、`test_publish_then_cancel_releases_only_candidate`、`test_slow_validation_does_not_block_control_query`、`test_one_consumer_for_shared_worker`、`test_duplicate_old_or_foreign_token_is_rejected`。用 Events／barriers 固定竞态，禁止靠 sleep 猜先后。

```python
self.assertEqual(result.reason, PreparedRejectReason.INPUTS)
self.assertIsNone(result.handle)
self.assertEqual(control_calls["deserialize"] + control_calls["proof_hash"], 0)
```

- [ ] **Step 2: RED。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_receiver -v`。Expected: 控制 drain／身份-only 接收不能满足断言，退出 1。
- [ ] **Step 3: 扩展同服务。** 唯一 receiver 在锁外按规格 §6 顺序做 payload hash→反序列化→三向 ruleset→不可变图／完整 proof digest→父侧 inputs／digest→身份／入口／许可／账本→来源与实际依赖。短 metadata 同步边界重查 active token／scope／stream，然后登记 candidate 和实际发布 handle。控制查询不再 drain 原结果队列；父侧 expected tuple 先独立存好，再后台封装／摘要后提交，receiver 不倒推期待。
- [ ] **Step 4: GREEN。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_receiver tests.motion_nav.test_b10_motion_worker tests.motion_nav.test_r28_async_generation tests.motion_nav.test_r28_world_change_retirement -q`。Expected: 每个负例 typed 拒绝、未授权推进 0、竞态两顺序成立，单消费者，实际 receiver 线程身份有证据。
- [ ] **Step 5: 提交。** `git add -- mc2p/motion_nav/prepared_motion_validation.py mc2p/motion_nav/motion_worker.py tests/motion_nav/test_d102_receiver.py`；`git commit -m "feat(motion): register prepared proofs in the shared receiver"`。

**止损条件：** 第二个 pending／重试／generation owner、长锁、payload 解包先于摘要、父侧字段来自返回值或取消旧结果复活时停止。

### Task 6: 三种证明用途、计算退场与有界关闭

**Files:**
- Create: `tests/motion_nav/test_d102_registry_lifecycle.py`
- Modify: `mc2p/motion_nav/prepared_motion.py`（用途／释放与关闭结果纯值）
- Modify: `mc2p/motion_nav/motion_worker.py:183`、`:272`
- Modify: `mc2p/motion_nav/motion_coordination.py:830`、`:994`
- Modify: `mc2p/motion_nav/action_route_executor.py:524`、`:584`、`:597`、`:1174`
- Modify: `mc2p/motion_nav/navigation_session.py:3461`、`:3528`、`:3554`

**Interfaces:**
- Consumes: Task 5 唯一 registry、真实命令终态与监督者用途释放事实。
- Produces: `MotionResultInbox.accept_activation(handle: PreparedMotionHandle, transaction: PreparedActivationTransaction, handoff: HandoffEvidence) -> PreparedActivationAcceptance | ActivationResult`；`commit_activation(accepted: PreparedActivationAcceptance, delivered: PreparedActivationDelivery) -> ActivationResult`；`abort_activation(accepted: PreparedActivationAcceptance) -> ReferenceResult`；`retain_safe_tail(handle: PreparedMotionHandle, *, action_index: int) -> ReferenceResult`；`release_usage(handle: PreparedMotionHandle, *, use: ProofUse, evidence: ProofReleaseEvidence) -> ReferenceResult`；`job_active(identity: AsyncWorkIdentity) -> bool`；`usage_active(handle: PreparedMotionHandle, use: ProofUse) -> bool`；`close(*, timeout_seconds: float = 5.0) -> MotionCloseReport`。accept 只登记同服务的短期在途接纳引用，旧 executing／safe-tail 保持；commit 重查取消与资格，才转换新用途。`ProofUse` 为 `CANDIDATE/EXECUTING/SAFE_TAIL`；`ReferenceResult` 为 `RETAINED/RELEASED/REJECTED` 与 typed 原因；`MotionCloseReport` 含实际 exit code、live worker／receiver、refs／leases／bytes、timeout 与保留责任。`ProofReleaseEvidence` 分别含命令终态、动作安全结束／交接、监督者确认与用途消费者身份，不以 task terminal 代替；这些纯值在 `prepared_motion.py` 定义。

- [ ] **Step 1: 写失败测试。** `RegistryTests.test_candidate_is_retained_before_job_retirement`、`test_next_preparation_keeps_current_execution_ref`、`test_stale_scope_keeps_valid_tail`、`test_each_missing_release_fact_keeps_lease`、`test_close_keeps_tail_until_all_release_facts`、`test_last_reference_reclaims_bytes_and_lease`、`test_constructor_exception_hard_exit_full_queue_and_repeat_close_are_bounded`、`test_worker_transfer_keeps_one_receiver`。

```python
self.assertFalse(inbox.job_active(identity))
self.assertTrue(inbox.usage_active(handle, ProofUse.EXECUTING))
self.assertEqual(final.live_workers + final.live_receivers + final.lease_count, 0)
```

- [ ] **Step 2: RED。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_registry_lifecycle -v`。Expected: 当前退场只有 job，无法保留／证明用途寿命，退出 1。
- [ ] **Step 3: 实现用途记录与关闭。** candidate 安装前完成用途转移，再保留原 `_retire_work("motion_proof_installed")`。所有 executor pop／replace／reprepare／advance／reset 发用途事实。snapshot／bytes 到最后在途验真与全部用途释放才回收；close 停新工作、退场未执行候选、有界唤醒／join／terminate，不用无界 `join_thread()`。总关闭 deadline 5 秒；真实异常退出码照录，不伪造为 0。计算已关闭时 registry 保留现有收尾，只接受释放。后继 Session／continuation 同时移交 worker 和同一 inbox，不另启 receiver。
- [ ] **Step 4: GREEN。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_registry_lifecycle tests.motion_nav.test_b10_motion_candidate tests.motion_nav.test_input_responsibility tests.motion_nav.test_async_work_verification -q`。Expected: 所有关闭路径有界，未解决责任下 facts 可读，解决后 refs／leases／bytes／threads／processes 为零。
- [ ] **Step 5: 提交。** `git add -- mc2p/motion_nav/prepared_motion.py mc2p/motion_nav/motion_worker.py mc2p/motion_nav/motion_coordination.py mc2p/motion_nav/action_route_executor.py mc2p/motion_nav/navigation_session.py tests/motion_nav/test_d102_registry_lifecycle.py`；`git commit -m "feat(motion): separate proof uses from computation retirement"`。

**止损条件：** 空中事实提前回收、缺任一释放条件仍释放、worker/receiver 泄漏、timeout 无返回或满队列覆盖旧工作时停止。

### Task 7: 协调器唯一 stream 与同 body owner 动作交接

**Files:**
- Create: `tests/motion_nav/test_d102_action_handoff.py`
- Modify: `mc2p/motion_nav/prepared_motion.py`（动作边界与无序号提议许可）
- Modify: `mc2p/motion_nav/motion_coordination.py:879`、`:1087`、`:1158`
- Modify: `mc2p/motion_nav/action_route_executor.py:524`、`:1174`
- Modify: `mc2p/motion_nav/motion_candidate.py:403`
- Modify: `mc2p/motion_nav/route_body_controller.py:60`、`execution_supervisor.py:84`
- Modify: `mc2p/motion_nav/body_control.py:81`（仅必要动作级证据字段）

**Interfaces:**
- Consumes: 注册 handle／当前快照事实、固定选择后的父侧 commands、现有 route owner／风险账本。
- Produces: `MotionRouteCoordinator.prepare_action(action_index: int, selected_commands: tuple[MotionCommandTick, ...], anchor: StateAnchor, *, ledger_identity: str) -> PreparedSubmitResult`；`generation_for(action_index: int) -> StreamGeneration | None`；`ActionRouteExecutor.install_prepared_motion(handle: PreparedMotionHandle) -> None`；`stage_prepared_action(frame: NavigationFrame, anchor: StateAnchor) -> PreparedActionHandoff`；`ExecutionSupervisor.approve_action_handoff(control: RouteControl, proposal: PreparedActionHandoff, frame: NavigationFrame, ledger: InputApplicationLedger) -> PreparedHandoffApproval`；`prepare_action_activation(control: RouteControl, approval: PreparedHandoffApproval, binding: StreamActivationBinding) -> PreparedActivationTransaction | ActivationResult`。stage、approve、prepare 均只验证并形成不可变事实，不推进 action、不换 owner、不释放旧用途。`PreparedActionHandoff` 绑定旧／新 action、同 owner、proof token、当前许可、冲突命令终态和真实入口检查结果；`PreparedHandoffApproval` 是无实际序号的提议许可，保存 approval token 和这些绑定。它不是身体释放事实；取得实际 binding 后也只准备交接证据，等 registry 接纳、binding 交付全部成立后才 commit。两个新纯值在 `prepared_motion.py` 定义。

- [ ] **Step 1: 写失败测试。** `ActionHandoffTests.test_next_jump_prepares_while_first_is_airborne`、`test_target_revision_alone_keeps_stream`、`test_inputs_entry_window_or_suffix_change_replaces_stream`、`test_same_route_handoff_keeps_body_owner`、`test_supervisor_approval_is_required`、`test_entry_mismatch_keeps_current_safe_tail`、`test_overdue_or_worker_death_does_not_cancel_valid_prefix`、`test_ground_path_keeps_fixed_controller`。

```python
self.assertEqual(before.owner_id, after.owner_id)
self.assertEqual(unchanged.generation_id, original.generation_id)
self.assertEqual(unapproved.first_command_proposals, 0)
```

- [ ] **Step 2: RED。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_action_handoff -v`。Expected: 当前仅 Walk 预测、内部 `_advance` 无动作级批准，退出 1。
- [ ] **Step 3: 实现现有 owner 适配。** 用同 worker SOLVE 选择输入，再由协调器独立冻结为 PREPARE request；选择结果不是 handle。第一条真实回执选定分支后，根据已有预计出口提前准备下一严格动作，入口资格仍由完整 proof 覆盖。`_advance` 先暂存动作边界事实而不递增 action；监督者无副作用许可后可以提议，直到 Task 8 的接纳／交付／commit 成立才推进，保持同 owner 和 ≤1 tick 接续目标。有界比较使用已发布热视图，不调用现场安全 rollout 或扩大入口；旧 scope／新准备错误只退其计算，不撤仍有效前段。
- [ ] **Step 4: GREEN。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_action_handoff tests.motion_nav.test_execution_supervisor tests.motion_nav.test_action_continuity_formal tests.motion_nav.test_action_entry_handoff_repair tests.motion_nav.test_f2_ground_start_window -q`。Expected: 单一 body owner、single advance／observe、无额外中性帧、普通地面行为不变。
- [ ] **Step 5: 提交。** `git add -- mc2p/motion_nav/prepared_motion.py mc2p/motion_nav/motion_coordination.py mc2p/motion_nav/action_route_executor.py mc2p/motion_nav/motion_candidate.py mc2p/motion_nav/route_body_controller.py mc2p/motion_nav/execution_supervisor.py mc2p/motion_nav/body_control.py tests/motion_nav/test_d102_action_handoff.py`；`git commit -m "feat(motion): approve prepared action handoffs within one body owner"`。

**止损条件：** 新增 body owner、executor／receiver 创建 stream、目标 revision 自动换 stream、未批准动作首提议或真实偏差被夹回预测点时停止。

### Task 8: Runtime 仲裁后激活、真实首条回执与命令终态

**Files:**
- Create: `tests/motion_nav/test_d102_runtime_activation.py`
- Modify: `mc2p/runtime/player_runtime_v1.py:332`、`:417`、`:565`
- Modify: `mc2p/motion_nav/online_motion.py:210`、`:326`、`:410`
- Modify: `mc2p/motion_nav/route_body_controller.py:60`、`motion_candidate.py:443`
- Modify: `mc2p/motion_nav/execution_supervisor.py`（现有身体 owner 的激活端口）
- Modify: `mc2p/motion_nav/navigation_session.py:342`、`:3285`
- Modify: `mc2p/skills/navigation_session_driver.py:411`

**Interfaces:**
- Consumes: Task 2 低层 activation 值／port，Task 6 registry，Task 7 正式 handoff。
- Produces: `PlayerRuntimeV1.control_frame(task, profile, deadline_monotonic_ns, *, proposals=(), input_execution_window=None, prepared_streams: tuple[PreparedStreamDispatch, ...] = ()) -> RuntimeStepResultV1`；`InputApplicationLedger.ledger_identity -> str`；`bind_stream(sequence: int, *, generation_id: str, command_index: int) -> InputStreamCommandFact`；supervisor 实现 Task 2 的 prepare／accept／deliver／commit／record_application 端口，RouteControl 沿用同事务调用 Task 6 registry；`VerifiedMotionExecutor.prepare_activation(transaction: PreparedActivationTransaction, accepted: PreparedActivationAcceptance) -> PreparedActivationDelivery | ActivationResult` 与 `commit_activation(delivered: PreparedActivationDelivery) -> None`。prepare_activation 的交付确认不推进 action 或开始输入，只有 commit 消费确认后改变执行进度。`RuntimeStepResultV1` 增加 `stream_activation: ActivationResult | None = None`，不改变无 prepared 提议的旧调用。

- [ ] **Step 1: 写失败测试。** `ActivationTests.test_global_sequences_continue_during_preparation`、`test_not_selected_keeps_candidate_without_activation`、`test_composite_motion_projection_must_match`、`test_activation_registered_and_delivered_before_backend_send`、`test_receipt_is_reported_and_pruned_before_runtime_returns`、`test_wrong_token_owner_ledger_window_or_generation_sends_zero_bundle_input`、`test_midstream_late_missing_or_changed_input_is_deviation`、`test_no_conflicting_generation_for_same_owner_tick`。新增 `test_cancel_after_supervisor_permission_before_registry_accept_keeps_incumbent`，以 barrier 在许可后、registry 接纳前取消，断言旧 action／owner／executing／safe-tail 不变、旧权限不释放且新流输入 0；`test_backend_send_exception_after_activation_keeps_uncertain_input_tail` 让 backend 可能应用首条后再抛异常，断言真实命令终态未明前仍保留 owner、执行／尾迹和 lease，不能把任务 FAILED 当安全释放。包括同帧攻击不改运动的正例、最终视角改变运动投影的拒绝例、首序号是实际全局值而非连续 stream index。

```python
self.assertLess(events.index("REGISTRY_ACCEPTED"), events.index("BINDING_DELIVERED"))
self.assertLess(events.index("BINDING_DELIVERED"), events.index("ACTION_AND_USAGE_COMMITTED"))
self.assertLess(events.index("ACTION_AND_USAGE_COMMITTED"), events.index("BACKEND_SEND"))
self.assertLess(events.index("TRACKER_ACTIVATED"), events.index("FIRST_APPLICATION"))
self.assertEqual(not_selected.activation, ActivationStatus.NOT_SELECTED)
```

- [ ] **Step 2: RED。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_runtime_activation -v`。Expected: 当前 driver 发送后才绑定，不能满足事件先后，退出 1。
- [ ] **Step 3: 实现唯一出口激活。** Runtime 在最终仲裁后读取真实 sequence、最终复合 action 和许可，确认运动／视角投影匹配。supervisor／RouteControl 无副作用 prepare，registry 最后校验并接纳短事务，executor／tracker 确认 binding 交付；全部成立后在短 commit 边界重查取消／资格，同步动作推进及新用途转换，最后才 `_submit_input_record`／backend send。任一接纳或交付失败只退场新事务，旧 action／owner／executing／safe-tail 保持。commit 后 send 异常按“可能已经发送”保留正式账本记录与收尾依据，等待真实命令终态和监督者释放；取不到事实则报告 `BODY_RESPONSIBILITY_UNRESOLVED`。首次应用由 Runtime 更新账本时立即经同端口上报，返回 driver 前完成归属与剪枝；后续只记录实际 sequence→generation/index。旧 driver 不重复首条登记，拒绝不发送新流、不临时补 Neutral，也不把 SUPERSEDED 无条件当已撤销。
- [ ] **Step 4: GREEN 与 G1。** 先 Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_contract_red tests.motion_nav.test_d102_contracts tests.motion_nav.test_d102_world_snapshot tests.motion_nav.test_d102_prepared_proof tests.motion_nav.test_d102_receiver tests.motion_nav.test_d102_registry_lifecycle tests.motion_nav.test_d102_action_handoff tests.motion_nav.test_d102_runtime_activation tests.test_player_runtime_v1 tests.motion_nav.test_runtime_navigation_verified_handoff tests.motion_nav.test_input_responsibility -q`，普通检查只用 TemporaryDirectory。再显式运行一次证据批次，并只验证其索引：

```powershell
$d102G1Run = Join-Path 'D:\My_project\mc_ai\.tmp\d102-formal-v1\g1' ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ') + '-' + [Guid]::NewGuid().ToString('N'))
& .\.venv\python.exe scripts/verify_prepared_motion_d102.py --record-contracts --output $d102G1Run
& .\.venv\python.exe scripts/verify_prepared_motion_d102.py --stage G1 --input (Join-Path $d102G1Run 'index.json')
```

Expected: 原五组 RED 转 GREEN，零失败／错误／跳过／expected failure，拒绝流推进 0；一次索引中直接测试事件完整且同属该 run，普通测试与后续导出测试不触碰该目录。
- [ ] **Step 5: 提交。** `git add -- mc2p/runtime/player_runtime_v1.py mc2p/motion_nav/online_motion.py mc2p/motion_nav/route_body_controller.py mc2p/motion_nav/execution_supervisor.py mc2p/motion_nav/motion_candidate.py mc2p/motion_nav/navigation_session.py mc2p/skills/navigation_session_driver.py tests/motion_nav/test_d102_runtime_activation.py`；`git commit -m "feat(runtime): activate prepared streams after final arbitration"`。

**止损条件：** preallocated sequence、activation 后改 bundle、callback 在首回执之后、错配仍发该流输入、抢占其他 owner 或未来撤销终态被伪造时停止，不采性能。

### Task 9: 正式 Windows 隔离矩阵与真实 20 Hz 量尺

**Files:**
- Create: `tests/sim/d102_prepared_motion.py`
- Create: `scripts/measure_prepared_motion_d102.py`
- Create: `tests/motion_nav/test_d102_windows_matrix.py`
- Modify: `scripts/verify_prepared_motion_d102.py`、`tests/sim/manifests/prepared-motion-proof-d102-v1.json`
- Record: `.tmp/d102-formal-v1/g2/`；紧凑包到 `evidence/motion_navigation/prepared-motion-proof-d102-v1/g2/`

**Interfaces:**
- Consumes: G1 正式服务；夹具只提供观察 facts，不给 worker 全知入口。
- Produces: `run_d102_matrix(stage: str, *, manifest: Path, output: Path) -> dict`；CLI `--stage fast|spawn-smoke|realtime|fallback|python --manifest PATH --output PATH` 与 `--recompute --input PATH`。报告包含 raw 索引和实测分母；recompute 不启动 worker、不增性能样本。

- [ ] **Step 1: 写失败测试。** `MatrixTests.test_fast_matrix_is_144_independent_exact_entries`、`test_realtime_denominator_excludes_fast_and_fallback`、`test_ready_is_actual_publish_and_first_consume`、`test_hot_path_counts_and_control_phase_coverage_are_complete`、`test_third_job_and_byte_capacity_are_rejected`、`test_recompute_preserves_raw_and_detects_tampering`。将人为超 30 ms、缺控制阶段或伪造 ready 的报告送 verifier，必须失败。
- [ ] **Step 2: RED。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_windows_matrix -v`。Expected: 正式 runner／完整量尺尚缺，退出 1。
- [ ] **Step 3: 实现量尺与冻结输入。** fast 为 `3平台×2起点×3独立入口偏差×4方向×2积压=144`，每项一次；另 12 类代表负例覆盖身份／scope／输入／规则／UNKNOWN／入口／依赖／取消两顺序／迟到／后续缺失／硬退出。spawn smoke 为六基础组合＋一项积压，共 7 场。realtime 为朝南精确入口 `3平台×2起点×2积压×5重复=60`，50 ms 固定节拍、一个真实持久 spawn 与 receiver、10 次预热单列。fallback 六场确保越过实际 movement-tick 交接期限，6／6 安全收尾、第二跳授权 0。入范围之外偏差另测 typed 拒绝，不把它列为正常完成。
- [ ] **Step 4: GREEN→功能→真实性能。** 依次 Run 下列命令；任一门槛失败即停止后续命令。

```powershell
& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_windows_matrix -q
& .\.venv\python.exe scripts/measure_prepared_motion_d102.py --stage fast --manifest tests/sim/manifests/prepared-motion-proof-d102-v1.json --output .tmp/d102-formal-v1/g2/fast
& .\.venv\python.exe scripts/measure_prepared_motion_d102.py --stage spawn-smoke --manifest tests/sim/manifests/prepared-motion-proof-d102-v1.json --output .tmp/d102-formal-v1/g2/spawn
& .\.venv\python.exe scripts/measure_prepared_motion_d102.py --stage realtime --manifest tests/sim/manifests/prepared-motion-proof-d102-v1.json --output .tmp/d102-formal-v1/g2/realtime
& .\.venv\python.exe scripts/measure_prepared_motion_d102.py --stage fallback --manifest tests/sim/manifests/prepared-motion-proof-d102-v1.json --output .tmp/d102-formal-v1/g2/fallback
& .\.venv\python.exe scripts/verify_prepared_motion_d102.py --stage G2 --input .tmp/d102-formal-v1/g2
```

Expected: 144／144 独立 exact-entry 正例；12 类拒绝均合约成立；spawn 7／7；realtime 60／60；fallback 6／6 安全但连续完成 0／6。起点晚一由实际应用证实；及时准备时着地至有效输入≤1 movement tick，非必要停车 0。完整决定及开始／交接子分布分别达到 8／15／<30 ms，安全与 deadline miss 0，热禁止调用 0，全部 refs／leases／bytes 最终为零。后台封装／校验、队列、GIL 唤醒迟到和从请求准备开始至实际消费独立报告。
- [ ] **Step 5: 提交量尺与紧凑证据。** `git add -- tests/sim/d102_prepared_motion.py scripts/measure_prepared_motion_d102.py scripts/verify_prepared_motion_d102.py tests/motion_nav/test_d102_windows_matrix.py tests/sim/manifests/prepared-motion-proof-d102-v1.json evidence/motion_navigation/prepared-motion-proof-d102-v1/g2`；`git commit -m "test(motion): validate prepared proofs on Windows matrices"`。

**止损条件：** 绑定／引用／close 不合格不采性能；G2 任一正式门槛失败不接 Python 正式链。性能失败不筛掉极值或追加重复平均，最多两轮限定修正后重测受影响冻结批次，旧失败 raw 保留。

### Task 10: 正式 Python 链只接单一两跳组合

**Files:**
- Create: `tests/motion_nav/test_d102_formal_chain.py`
- Modify: `mc2p/motion_nav/navigation_session.py:4506`、`mc2p/skills/navigation_session_driver.py`
- Modify: `tests/sim/d102_prepared_motion.py`、`tests/sim/manifests/prepared-motion-proof-d102-v1.json`
- Record: `.tmp/d102-formal-v1/g3/`；紧凑包到 `evidence/motion_navigation/prepared-motion-proof-d102-v1/g3/`

**Interfaces:**
- Consumes: G2，现有 Session／driver／Runtime 的正式 owner。
- Produces: `NavigationSessionProposal.prepared_stream: PreparedStreamDispatch | None`；driver 将其交给 Task 8 `control_frame(..., prepared_streams=...)`。组合接纳只开 `Walk→JumpGap→Walk→JumpGap`，其他动作继续现行已验证路径；服务在控制决定之前 ready，不在首次接纳中 spawn。

- [ ] **Step 1: 写失败测试。** `FormalChainTests.test_24_routes_use_real_session_runtime_path`、`test_ground_segments_use_fixed_route_controller`、`test_air_cancel_revision_and_overdue_keep_owner`、`test_receiver_cancel_does_not_publish_old_candidate`、`test_real_entry_offset_never_expands_old_proof`、`test_route_activation_happens_before_application`、`test_unsupported_action_combination_keeps_existing_path`。每项经正式 Session→协调器→executor→supervisor→Runtime，禁止测试直接 physics 推进冒充正式链。
- [ ] **Step 2: RED。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_formal_chain -v`。Expected: 新组合尚未接线，退出 1。
- [ ] **Step 3: 最小正式接线。** 注入同一个 Runtime 世界端口、同一个 inbox／worker 与低层 activation dispatch；提前 ready。生产矩阵为 `3平台×2起点×4方向=24`，精确入口；代表反例为第一跳空中修订／取消、校验中取消、第二跳过期、worker 死亡、后续实际缺输入、真实偏差与目标 revision 但输入不变。保留 typed 原因与任务成功／安全收尾的区分。
- [ ] **Step 4: GREEN 与 G3。** 先 Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_formal_chain tests.motion_nav.test_action_continuity_admission tests.motion_nav.test_action_entry_late_hardening tests.motion_nav.test_post_input_loss_landing_recovery tests.motion_nav.test_r28_generation_formal_chain tests.motion_nav.test_r28_planning_history tests.motion_nav.test_r28_planning_failure_handoff -q`；再按以下命令完成最终范围回归，代码冻结后只跑一次正序／逆序及每项正式性能。

```powershell
& .\.venv\python.exe scripts/measure_prepared_motion_d102.py --stage python --manifest tests/sim/manifests/prepared-motion-proof-d102-v1.json --output .tmp/d102-formal-v1/g3/python
& .\.venv\python.exe scripts/run_motion_navigation_checks.py --output .tmp/d102-formal-v1/g3/forward.json
& .\.venv\python.exe scripts/run_motion_navigation_checks.py --reverse --output .tmp/d102-formal-v1/g3/reverse.json
& .\.venv\python.exe scripts/benchmark_d058_route_revalidation.py --output .tmp/d102-formal-v1/g3/d058
& .\.venv\python.exe scripts/benchmark_d061_long_session.py --output .tmp/d102-formal-v1/g3/d061
& .\.venv\python.exe scripts/navigation_s0r_evidence.py --set product --output .tmp/d102-formal-v1/g3/v7
& .\.venv\python.exe scripts/f2_ground_route_evidence.py --output .tmp/d102-formal-v1/g3/f2
& .\.venv\python.exe scripts/f2r_piecewise_evidence.py formal --output .tmp/d102-formal-v1/g3/v8-formal
& .\.venv\python.exe scripts/f2r_piecewise_evidence.py clutter-formal --output .tmp/d102-formal-v1/g3/v8-clutter-formal
& .\.venv\python.exe scripts/f2rec_r0_v9.py --output .tmp/d102-formal-v1/g3/v9
& .\.venv\python.exe scripts/navigation_s0r_evidence.py --set coordination --output .tmp/d102-formal-v1/g3/coordination
& .\.venv\python.exe scripts/verify_prepared_motion_d102.py --stage G3 --input .tmp/d102-formal-v1/g3
```

Expected: 新正式 24／24 与反例判定成立，完整决定 8／15／<30 ms、热调用 0、接续≤1 tick；全检查零失败／错误／跳过／expected failures，场景深度哈希不变。D058／D061 用默认正式配置，原 8 ms／worker 债不被冒写已关闭。G3 分别读取真实入口的 index／runs／summary，核对 v7 2000、F2 528、v9 216、协调 1448 的实际唯一 case 分母、输入哈希和冻结成功签名。v8 必须合并 `v8-formal` 的 104 项与 `v8-clutter-formal` 的 1800 项原始行，按冻结 case ID 检查不缺／不重，再得到 1904 总分母；不能用两份总计或通用 runner 的报告冒充。逐项核对旧成功非退步，保留原求解失败及非计时签名差异原因，不因新任务重复旧 Fabric 93／46 全量。
- [ ] **Step 5: 提交。** `git add -- mc2p/motion_nav/navigation_session.py mc2p/skills/navigation_session_driver.py tests/motion_nav/test_d102_formal_chain.py tests/sim/d102_prepared_motion.py tests/sim/manifests/prepared-motion-proof-d102-v1.json evidence/motion_navigation/prepared-motion-proof-d102-v1/g3`；`git commit -m "feat(navigation): integrate the bounded D102 two-jump route"`。

**止损条件：** 扩动作、旁路正式 owner、旧成功转失败、完整控制成本漏测、G3 失败时不运行 Fabric。若实际 diff 超过表中文件，先收敛并更新范围，不自动扩阶段。

### Task 11: F-TICK 只读记录真实客户端 tick 顺序

**Files:**
- Create: `scripts/run_prepared_motion_fabric.py`
- Create: `scripts/d102_fabric_tick_runtime.py`
- Create: `tests/motion_nav/test_d102_fabric_probe.py`
- Create: `deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/D102TickTrace.java`
- Create: `tests/java/D102TickTraceTest.java`
- Modify: `scripts/probe_fabric_deployment_observation.py`
- Modify: `deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/DeploymentObservationProbe.java`、`DeploymentDiagnostics.java`、`ProbeTransport.java`、`DeploymentSession.java`、`mixin/BehaviorPlayerMixin.java`（只增加有界诊断与关联）
- Modify: `tests/test_standalone_java_gates.py`
- Record: `.tmp/d102-formal-v1/f-tick/`；紧凑包到 `evidence/motion_navigation/prepared-motion-proof-d102-v1/f-tick/`

**Interfaces:**
- Consumes: G3、现有 deployment launcher、现有 movement tick／input sample 诊断。
- Produces: CLI `--stage F-TICK|F-CONTINUITY --seed 21001 --manifest PATH --python-evidence PATH --output PATH [--tick-evidence PATH]`；F-TICK 固定四方向各 10 次。launcher 新增 `--prepared-motion-stage F-TICK|F-CONTINUITY` 并原样转发子 runtime，不改变既有 seed choices；`run_worker(..., prepared_motion_stage: str | None = None)` 在原 launcher 生命周期内选择新探针。`classify_tick_trace(events: tuple[dict, ...]) -> dict` 以 episode、observation/control sequence、movement tick 关联；输出 `NEXT_MOVEMENT_TICK/LATER_MOVEMENT_TICK/INCOMPLETE`。Java `D102TickTrace.record(String kind, String episode, Long observationSequence, Long requestSequence, Long movementTick, long monotonicNs)`、`drain() -> List<TickEvent>`、`dropped() -> long`；只读缓冲上限 1024，溢出必须使证据 INCOMPLETE，未知 ID 用 null，不伪造数值。

- [ ] **Step 1: 写失败测试。** `FabricProbeTests.test_ftick_has_40_timelines_and_preserves_input_behavior`、`test_seed_is_forwarded_as_21001`、`test_incomplete_or_cross_clock_trace_cannot_pass`、`test_later_tick_is_valid_classification`、`test_fcontinuity_requires_verified_ftick_report`。Java `D102TickTraceTest` 检查有界缓冲、去重错误和完整事件。必须记录 LANDING_OBSERVATION_SENT→Python 接收／决定→CLIENT_FRAME_RECEIVED→INPUT_SAMPLED→MOVEMENT_APPLIED，以及实际首回执关联。
- [ ] **Step 2: RED。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_fabric_probe -v`。Expected: 新诊断／stage 门禁缺失，退出 1。
- [ ] **Step 3: 实现只读探针。** 接已有 deployment-only 钩子，不改 `ClientBehaviorInput`、租约行为、客户端命令队列或 tick 顺序。使用 launcher 已接受的 seed 21001，不复制旧计划未接受的 21101。各进程仅本进程相减墙钟；跨进程通过事实 ID 联结。wrapper 先校验 G3 报告，不启动缺条件实机。
- [ ] **Step 4: GREEN 后实机 F-TICK。**

```powershell
& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_fabric_probe tests.test_standalone_java_gates tests.test_deployment_session tests.test_deployment_transport -q
& .\.venv\python.exe scripts/run_prepared_motion_fabric.py --stage F-TICK --seed 21001 --manifest tests/sim/manifests/prepared-motion-proof-d102-v1.json --python-evidence .tmp/d102-formal-v1/g3 --output .tmp/d102-formal-v1/f-tick
& .\.venv\python.exe scripts/verify_prepared_motion_d102.py --stage F-TICK --input .tmp/d102-formal-v1/f-tick/summary.json
```

Expected: **40／40 完整且分类可复算，输入行为不变**；next 或 later 分类均照录。缺／重复／错关联或未知跨时钟差值为 INCOMPLETE，不通过。结果写清逐 tick 发送能否满足下一动作窗口；不能从 Windows 性能推导 tick 顺序。
- [ ] **Step 5: 提交探针与证据。** `git add -- scripts/run_prepared_motion_fabric.py scripts/d102_fabric_tick_runtime.py scripts/probe_fabric_deployment_observation.py tests/motion_nav/test_d102_fabric_probe.py tests/java/D102TickTraceTest.java tests/test_standalone_java_gates.py deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment evidence/motion_navigation/prepared-motion-proof-d102-v1/f-tick`；`git commit -m "test(fabric): measure D102 input and movement tick order"`。

**止损条件：** F-TICK 不完整则不进入 Task 12。若完整实测表明逐 tick 发送无法达连续门槛，保留 F-TICK 通过但停止连续性，另写客户端预排程决定和实施范围；不把更晚分类改写成 next。

### Task 12: F-CONTINUITY 限定真实连续两跳

**Files:**
- Create: `scripts/d102_fabric_continuity_runtime.py`
- Modify: `scripts/run_prepared_motion_fabric.py`、`tests/motion_nav/test_d102_fabric_probe.py`
- Modify: `tests/sim/manifests/prepared-motion-proof-d102-v1.json`
- Record: `.tmp/d102-formal-v1/f-continuity/`；紧凑包到 `evidence/motion_navigation/prepared-motion-proof-d102-v1/f-continuity/`

**Interfaces:**
- Consumes: hash 验证通过的 F-TICK 完整报告、G3 正式路线、实測已支持的逐 tick 发送协议。
- Produces: `run_fabric_continuity(*, tick_evidence: Path, manifest: Path, output: Path) -> dict`；无 F-TICK 或有 `requires_client_preschedule=true` 必须 typed `STAGE_NOT_READY`，不启动客户端。

- [ ] **Step 1: 写失败测试。** `FabricProbeTests.test_continuity_requires_tick_hash_and_protocol`、`test_smoke_precedes_24_positive_and_4_fallback_cases`、`test_late_one_uses_actual_application`、`test_safe_fallback_is_not_two_jump_success`、`test_full_stop_unknown_damage_or_owner_gap_fails`。以伪造 first receipt、落地空档 2 tick、任务结束但 lease 未释放的报告做失败例。

```python
self.assertEqual(fallback["second_jump_authorized"], 0)
self.assertLessEqual(positive["landing_to_input_gap_ticks"], 1)
self.assertEqual(positive["nonessential_full_stops"], 0)
```

- [ ] **Step 2: RED。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_fabric_probe -v`。Expected: 连续性 runner／完整判定尚缺，退出 1。
- [ ] **Step 3: 实现限定实机 runtime。** 先朝南普通 1 场 smoke，成功才跑 `3平台×4方向×2起点=24` 正例及四方向各一次准备过期 fallback 共 4 反例。沿正式 Session／Runtime 路线，真实起跳／落地与首条应用证实；精确入口失配按真实状态失败／收尾，不移动身体或扩大证明。
- [ ] **Step 4: GREEN 后 F-CONTINUITY。**

```powershell
& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_fabric_probe -q
& .\.venv\python.exe scripts/run_prepared_motion_fabric.py --stage F-CONTINUITY --seed 21001 --manifest tests/sim/manifests/prepared-motion-proof-d102-v1.json --python-evidence .tmp/d102-formal-v1/g3 --tick-evidence .tmp/d102-formal-v1/f-tick/summary.json --output .tmp/d102-formal-v1/f-continuity
& .\.venv\python.exe scripts/verify_prepared_motion_d102.py --stage F-CONTINUITY --input .tmp/d102-formal-v1/f-continuity/summary.json
```

Expected: smoke 1／1 单列；正例 24／24，两次真实起跳／落地，空档≤1 tick，非必要完整停车 0；伤害超额、UNKNOWN 放行、未授权／无人负责推进、期限违规均 0。晚一条件必须实际确认。反例 4／4 安全收尾、第二跳授权 0，命令终态齐全与最后 refs／lease／bytes 释放；不计两跳成功。
- [ ] **Step 5: 提交。** `git add -- scripts/d102_fabric_continuity_runtime.py scripts/run_prepared_motion_fabric.py tests/motion_nav/test_d102_fabric_probe.py tests/sim/manifests/prepared-motion-proof-d102-v1.json evidence/motion_navigation/prepared-motion-proof-d102-v1/f-continuity`；`git commit -m "test(fabric): validate the bounded D102 continuous two-jump route"`。

**止损条件：** 任一硬安全事件立即停；真实入口／连续性失败阻止签署，最多两轮限定修正。不得因失败自动增加动作、范围、重复次数或客户端预排程。

### Task 13: 独立导出、证据重算与阶段收尾

**Files:**
- Modify: `config/motion-navigation/standalone-export-v1.json`
- Modify: `tests/motion_nav/test_standalone_export.py`
- Modify: `packaging/motion-navigation-standalone/README.md`、`AGENTS.md`
- Modify: 根 `README.md`、`AGENTS.md`、本计划和 `docs/motion_navigation/acceptance/prepared-motion-proof-pipeline-d102.md`
- Modify: 架构文档仅回填实际职责／接口，不改历史决定与原证据
- Record: `evidence/motion_navigation/prepared-motion-proof-d102-v1/manifest.json`、各阶段紧凑 summary／verification／report／review／来源清单

**Interfaces:**
- Consumes: 各实际阶段 raw 与失败批次，现有 export／verify／check-java。
- Produces: `verify_prepared_motion_d102.py --stage G4 --input PATH` 验证所有实际签署门槛与 provenance；原始大 proof／tick 在本地保留，Git 紧凑包不声称可独立重放全部 raw。

- [ ] **Step 1: 写失败测试。** `StandaloneExportTests.test_d102_required_sources_and_compact_evidence_are_closed`、`test_d102_export_excludes_local_raw_and_tmp`、`test_d102_historical_failures_remain_explicit`、`test_d102_export_rejects_in_tree_evidence_destination`；`EvidenceContractTests.test_recompute_adds_no_performance_samples`、`test_export_hash_is_not_test_execution_evidence`。required paths 精确列 D102 正式模块、runner、测试、文档、紧凑证据和必要历史 summary，不笼统 include `.tmp`／整个 evidence。
- [ ] **Step 2: RED。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_standalone_export tests.motion_nav.test_d102_evidence_contract -v`。Expected: 新依赖／紧凑包不闭合，退出 1。
- [ ] **Step 3: 更新导出与文档。** 添加精确 required paths／include，README／AGENTS 模板同步实际签署状态和复现命令。逐阶段重算 raw→summary，校验 SHA-256／大小／schema／环境／命令／容量、source commit 与实际 dirty provenance；不用新结果覆盖旧批次。F-CONTINUITY 因未满足前置而停止时，收尾记“未运行／未通过”，不得将整个目标写 COMPLETE。
- [ ] **Step 4: GREEN、独立公开快照运行与 G4。**

```powershell
& .\.venv\python.exe -m unittest tests.motion_nav.test_standalone_export tests.motion_nav.test_d102_evidence_contract tests.test_standalone_java_gates -q
& .\.venv\python.exe scripts/export_motion_navigation_standalone.py export --root .tmp/d102-formal-v1/export
& .\.venv\python.exe scripts/export_motion_navigation_standalone.py verify --root .tmp/d102-formal-v1/export
& .\.venv\python.exe scripts/export_motion_navigation_standalone.py check-java
git diff --check
```

再运行独立导出目录的检查；普通单元夹具只用 TemporaryDirectory，解析后的所有证据／输出路径强制在导出树之外，传入树内路径立即拒绝，不得新建导出树下 `.tmp`、g1 或 run 目录：

```powershell
Push-Location .tmp/d102-formal-v1/export
& 'D:\My_project\mc_ai\.venv\python.exe' scripts/run_motion_navigation_checks.py --output 'D:\My_project\mc_ai\.tmp\d102-formal-v1\export-check-forward.json'
& 'D:\My_project\mc_ai\.venv\python.exe' scripts/run_motion_navigation_checks.py --reverse --output 'D:\My_project\mc_ai\.tmp\d102-formal-v1\export-check-reverse.json'
& 'D:\My_project\mc_ai\.venv\python.exe' -m unittest tests.test_standalone_java_gates -q
Pop-Location
& .\.venv\python.exe scripts/export_motion_navigation_standalone.py verify --root .tmp/d102-formal-v1/export
& .\.venv\python.exe scripts/verify_prepared_motion_d102.py --stage G4 --input evidence/motion_navigation/prepared-motion-proof-d102-v1/manifest.json
```

确认加载模块来自导出树。Expected: 导出字节完整、依赖闭合、独立正逆零失败／错误／跳过／expected failures，运行只产生允许 cache，未改导出字节；重算未新增性能样本。检查失败 raw 和 D101／D098 原哈希保持不变。
- [ ] **Step 5: 收尾提交。** `git add -- config/motion-navigation/standalone-export-v1.json tests/motion_nav/test_standalone_export.py tests/motion_nav/test_d102_evidence_contract.py packaging/motion-navigation-standalone/README.md packaging/motion-navigation-standalone/AGENTS.md README.md AGENTS.md docs/motion_navigation/stages/prepared-motion-proof-pipeline-d102-plan.md docs/motion_navigation/acceptance/prepared-motion-proof-pipeline-d102.md docs/motion_navigation/architecture/prepared-motion-proof-pipeline-v1.md evidence/motion_navigation/prepared-motion-proof-d102-v1`；`git commit -m "docs(motion): close D102 with independently verified evidence"`。

**止损条件：** 导出缺依赖、内部运行冒充独立验证、历史失败被升格、证据分母／哈希不一致或未关闭安全／生命周期问题时，记录停止点并保持失败；不推送、发布或自动扩阶段。

## 计划自检与执行记录

本计划按 writing-plans 格式自检一次：规格 §2—3 对应 Tasks 2／5—8；§4 对应 Task 3；§5／5.5 对应 Tasks 2／6／8；§6 对应 Task 5；§7—10 对应 Tasks 6—8／10；§11 对应 Task 6；§12 对应 Tasks 9—12；§13 不变量贯穿直接测试与阶段 verifier。Review Focus 五项均有归属测试。所有新增方法名与跨任务值以各 Interfaces 区块为准；新增命令先定义再运行。计划没有函数实现正文，也没有新增第三条 pipeline。

旧 `.tmp/d102-code-map.md` 与 `.tmp/d102-test-map.md` 中“准备前绑定首序号”和“candidate→executing 后才能首条提议”的历史措辞，由 `3668cb3c` 的准备／激活两阶段契约取代：candidate 可在监督者许可后提议，真正发送前才由 Runtime 绑定并确认 executing。偏差样本不代表连续入口范围。G0／G1／G2／G3／Fabric 的成功分母和责任边界不得相互替代。

执行者逐 task 记录 RED 命令与断言、GREEN 命令与结果、提交、实际修改文件、raw 与紧凑证据路径、第一／第二轮限定修正及停止原因。每个 task 的提交只含该任务文件；不为凑计划数量提交无意义脚手架，也不为时间不足减少门槛。最终状态由实际门槛决定，本计划草案不预填任何“通过”。
