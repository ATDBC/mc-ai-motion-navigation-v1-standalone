# D101 连续运动最小试验 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

2026-10-11 当前状态：**D101 最小原型止于 M0，阶段未通过。** Task 1／2 已完成实施；M0 功能和安全条件通过，但完整决定 P99 48.6079 ms、最大值 57.5597 ms 超出 15／30 ms 门槛。Task 3／4／5 未获授权，M1／M2 未实现、未运行，Fabric 未运行。Task 6 只核对证据并记录止损；收尾事实见文末和[验收第 11 节](../acceptance/continuous-motion-minimal-probe-d101.md#11-task-6-收尾核验)。下文保留原任务顺序和冻结门槛，步骤执行完成不表示阶段通过。

**Goal:** 在不修改 `mc2p` 正式运动链的前提下，用 Windows 隔离闭环验证输入流起点时序、首条回执剪枝、关键节点比较和连续两跳提前计算，再用最小 Fabric 探针测清客户端 tick 顺序。

**Architecture:** M0／M1 只扩展 `experiments/motion_navigation/trajectory_proto/`，复用现有 1.21 计算器、冻结 `jump_gap_1` 输入和逐次证明数据。普通宽地面不建立新控制器；连续两跳由隔离 worker 提前准备第二跳，着地后只比较已有入口范围。M2 只给 deployment 探针增加只读时间线，不实现正式客户端队列或预排程。

**Tech Stack:** Python 3.11、`unittest`、现有 1.21 运动计算器、Windows `multiprocessing` spawn worker、Java 21、Fabric 1.21 deployment probe、JSON／JSONL 结构化证据。

**Spec:** `docs/motion_navigation/decisions/0101-clarify-short-open-loop-timing-and-lifecycle.md`；同时遵守 `docs/motion_navigation/decisions/0099-adopt-game-first-risk-graded-short-open-loop-control.md`、`docs/motion_navigation/decisions/0100-use-risk-aware-continuous-deviation-recovery.md` 和 `docs/motion_navigation/architecture/risk-aware-continuous-motion-v1.md`。

## Global Constraints

- 正式开发和性能判定平台是 Windows；Linux 只可作补充可移植性复核。
- 不修改 `mc2p/**`、正式 Runtime、Session、规划器、动作执行器、正式输入账本或 `ClientBehaviorInput`。
- M0 未通过立即停止，不进入 M1；M1 未通过立即停止，不进入 M2。
- M2 只测量 Fabric tick 顺序，不实现正式客户端命令队列、generation 队列、撤销队列或预排程。
- D101 的正式撤销、替换和输入欠载收尾不在本最小试验实现范围；本轮只验证 stale generation 不被接纳，不能据此宣称完整生命周期已交付。
- 0／1 tick 只属于输入流起点；第一条实际应用回执立即剪掉另一分支；后续单条晚到或缺失必须判为偏离。
- generation、owner、时序分支、身体范围和依赖一致时，关键节点只比较，不调用完整证明。
- 证明有效范围与外力归因阈值分开；本试验不调整 `MotionResidualThresholds`。
- 宽阔普通地面只使用现有 `FixedRouteController`。本试验不得增加普通地面输入包执行器。
- 所有场景使用现有 `TaskDamageBudget`、UNKNOWN 规则和单一 owner；不能用放宽安全条件换取完成率。
- 行为选择只读取计数和有类型事实，不读取墙钟；墙钟只用于性能报告和 worker 期限。
- E13／E14 的三方数据只作参考，不进入通过判定，也不能代替本项目 Windows 结果。
- M0／M1 正式性能口径为完整决定 P95≤8 ms、P99≤15 ms、最大值<30 ms；fixture 建造、证据写盘和 worker `close()` 不计入决定时间。
- 原型创建的 worker 必须在成功、失败、异常和阶段止损路径关闭；运行结束前不得留下 `d101-precompute` 活子进程。
- 每个任务先写失败测试，再写最小实现；每个任务单独提交。不得顺手修复范围外问题。
- 原 D098 P3、E13／E14 和既有证据只读保留，不能回写成通过。

## Review Focus

- 第一条回执重复、倒序或晚 2 tick 时，输入流必须拒绝歧义，不能选择最方便的分支；Task 1 覆盖。
- 着地点恰好位于前向 ±0.05 边界时，仍在入口范围内就只比较，超出范围才要求新证明；Task 3 覆盖。
- 第二跳 worker 返回旧 generation 时，结果必须丢弃，第一跳 owner 和当前输入流不受影响；Task 3 覆盖。
- worker 在异常、早停和 M1 门槛失败时必须关闭，不能因证据写盘失败泄漏子进程；Task 3／4 覆盖。
- Fabric 任一时间线事件缺失、重复或跨进程时钟被直接相减时，M2 必须标记证据不完整，不能回答“能否赶上下一个 tick”；Task 5 覆盖。

---

## 文件结构

```text
experiments/motion_navigation/trajectory_proto/
  d101_input_stream.py          # 输入流 generation、首条回执剪枝、关键节点比较
  d101_continuous_probe.py      # 单跳／双跳 fixture、闭环和隔离预计算 worker
  d101_measurement.py           # M0／M1 矩阵、性能统计和证据模型
scripts/
  measure_continuous_motion_d101.py  # Windows M0／M1 入口与阶段门禁
  d101_fabric_tick_runtime.py        # Fabric 四方向场景和证据汇总
  run_d101_fabric_tick_probe.py      # deployment launcher 参数入口
  probe_fabric_deployment_observation.py  # 只增加 D101 deployment runtime 选择
deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/
  D101TickTrace.java            # deployment-only 有界事件记录器
tests/motion_nav/
  test_d101_input_stream.py
  test_d101_continuous_probe.py
  test_d101_measurement.py
  test_d101_fabric_tick_probe_options.py
tests/java/
  D101TickTraceTest.java
tests/
  test_standalone_java_gates.py
evidence/motion_navigation/continuous-motion-d101-v1/
  m0/
  m1/
  m2/
```

M2 还允许小范围修改以下 deployment-only 文件：

- `deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/DeploymentObservationProbe.java`
- `deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/DeploymentDiagnostics.java`
- `deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/mixin/BehaviorPlayerMixin.java`

不得修改 `mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorInput.java`。

### Task 1: M0 输入流协议与关键节点比较

**Files:**
- Create: `experiments/motion_navigation/trajectory_proto/d101_input_stream.py`
- Create: `tests/motion_nav/test_d101_input_stream.py`
- Test: `tests/motion_nav/test_d101_input_stream.py`

**Interfaces:**
- Consumes: `PhysicsState`、`TickInput`、现有候选 identity、依赖版本和关键节点范围。
- Produces:
  - `StreamStartBranch(StrEnum)`：`ON_TIME`、`LATE_ONE`。
  - `StreamGeneration`：不可变 `generation_id`、`owner_id`、`request_id`、`action_index`、`requested_start_tick`、`commands`、`dependency_identity`。
  - `FirstApplicationReceipt`：`control_sequence`、`effect_tick`、`command`。
  - `KeyNodeEnvelope`：关键节点允许的位置、速度、姿态、支撑、依赖和承诺阶段。
  - `KeyNodeCheckKind(StrEnum)`：`COMPARE_ONLY`、`REPROOF_REQUIRED`、`REJECTED`。
  - `KeyNodeCheck`：`kind`、`reason`、`full_proof_calls`、`compared_fields`。
  - `InputStreamTracker.start(generation: StreamGeneration) -> InputStreamTracker`。
  - `InputStreamTracker.record_first_application(receipt: FirstApplicationReceipt) -> StreamStartBranch`。
  - `InputStreamTracker.expected_effect_tick(command_index: int) -> int`。
  - `InputStreamTracker.compare_key_node(observed: PhysicsState, envelope: KeyNodeEnvelope, *, generation_id: str, dependency_identity: tuple) -> KeyNodeCheck`。

- [x] **Step 1: 写输入流起点失败测试**

在 `test_d101_input_stream.py` 增加：

```python
def test_first_receipt_selects_only_the_on_time_or_late_one_start_branch(): ...
def test_second_command_cannot_choose_a_new_late_one_branch(): ...
def test_first_receipt_late_two_ticks_is_rejected(): ...
def test_duplicate_or_out_of_order_first_receipt_is_rejected(): ...
```

断言第一条命令只接受 `requested_start_tick` 或 `+1`；分支确定后，第 `n` 条命令的 tick 只能由同一偏移推导。

- [x] **Step 2: 运行输入流测试并确认失败**

Run: `.venv\python.exe -m unittest tests.motion_nav.test_d101_input_stream -q`

Expected: FAIL，原因是 `d101_input_stream` 尚不存在。

- [x] **Step 3: 实现输入流 generation 和首条回执剪枝**

按 Interfaces 创建有类型、不可变对象。禁止读取墙钟；重复、倒序、晚 2 tick、错误命令和旧 generation 都抛出 `ContractViolation`。

- [x] **Step 4: 写关键节点比较失败测试**

```python
def test_matching_key_node_is_compare_only_and_calls_no_full_proof(): ...
def test_body_outside_envelope_requires_reproof_without_external_force_label(): ...
def test_external_force_label_inside_envelope_does_not_invalidate_the_proof(): ...
def test_dependency_or_generation_change_requires_reproof(): ...
```

通过注入计数器断言 `COMPARE_ONLY` 的 `full_proof_calls == 0`，并分别验证证明范围与外力标签互不替代。

- [x] **Step 5: 实现 `compare_key_node()` 并运行测试**

Run: `.venv\python.exe -m unittest tests.motion_nav.test_d101_input_stream -q`

Expected: PASS。

- [x] **Step 6: 提交 Task 1**

```powershell
git add experiments/motion_navigation/trajectory_proto/d101_input_stream.py tests/motion_nav/test_d101_input_stream.py
git commit -m "feat(experiments): add D101 input stream contract"
```

### Task 2: M0 Windows 单跳闭环与正式门禁

**Files:**
- Create: `experiments/motion_navigation/trajectory_proto/d101_continuous_probe.py`
- Create: `experiments/motion_navigation/trajectory_proto/d101_measurement.py`
- Create: `scripts/measure_continuous_motion_d101.py`
- Create: `tests/motion_nav/test_d101_continuous_probe.py`
- Create: `tests/motion_nav/test_d101_measurement.py`
- Modify: `docs/motion_navigation/acceptance/continuous-motion-minimal-probe-d101.md`
- Evidence: `evidence/motion_navigation/continuous-motion-d101-v1/m0/`

**Interfaces:**
- Consumes: Task 1 `InputStreamTracker`；`scenarios.representative_fixture("jump_gap_1")`；`m0_probe.run_case("jump_gap_1", "A3", options=FORMAL_OPTIONS).inputs`；现有证明／物理接口。
- Produces:
  - `SingleJumpProbeSpec(start_branch: StreamStartBranch, rounds: int)`。
  - `SingleJumpProbeResult`：终态、输入哈希、首条回执、存活分支、关键节点比较次数、完整证明次数、安全事件、逐决定耗时、physics step。
  - `run_single_jump_probe(spec: SingleJumpProbeSpec) -> tuple[SingleJumpProbeResult, ...]`。
  - `M0Summary`：schema 版本、源码与输入哈希、正例分组计数、负例分类、关键节点比较与完整证明次数、physics step、P50／P95／P99／最大值、安全违规和总判定。
  - `run_m0(*, rounds: int, output: Path) -> M0Summary`。

- [x] **Step 1: 写 M0 单跳失败测试**

```python
def test_single_jump_on_time_and_late_one_use_the_same_inputs_and_both_land(): ...
def test_first_receipt_prunes_the_other_branch_before_commitment(): ...
def test_matching_commitment_and_landing_nodes_do_not_repeat_full_proof(): ...
def test_middle_command_late_one_is_a_bounded_deviation_not_a_new_start_branch(): ...
```

断言两条起点分支都安全落地；收到第一条回执后存活分支数为 1；一致关键节点的完整证明调用增量为 0；中段晚到不完成动作，也不掉落或获得新分支。

- [x] **Step 2: 运行单跳测试并确认失败**

Run: `.venv\python.exe -m unittest tests.motion_nav.test_d101_continuous_probe -q`

Expected: FAIL，缺少 `run_single_jump_probe`。

- [x] **Step 3: 实现最小单跳闭环**

闭环必须逐 tick 记录 owner、generation、提交、应用、观察和关键节点比较。它只复用已冻结 `jump_gap_1:A3` 输入，不搜索新动作，不把 D098 `RollingProofOwner` 当作新协议实现。

- [x] **Step 4: 写 M0 汇总和门禁测试**

```python
def test_m0_summary_has_both_start_branches_and_no_safety_failure(): ...
def test_m0_performance_uses_complete_decision_time_and_nearest_rank(): ...
def test_m0_rejects_reference_only_e13_e14_as_gate_inputs(): ...
```

汇总必须含 P50／P95／P99／最大值、决定数、physics step、compare-only 数、完整证明数和安全结果。

- [x] **Step 5: 实现 Windows runner 并运行直接检查**

Run: `.venv\python.exe -m unittest tests.motion_nav.test_d101_input_stream tests.motion_nav.test_d101_continuous_probe tests.motion_nav.test_d101_measurement -q`

Expected: PASS。

- [x] **Step 6: 运行正式 M0**

Run: `.venv\python.exe scripts/measure_continuous_motion_d101.py M0 --rounds 100 --output evidence/motion_navigation/continuous-motion-d101-v1/m0`

Expected:

- `on_time` 与 `late_one` 各 100／100 安全落地；
- 安全违规、掉落、UNKNOWN 放行、无人负责推进均为 0；
- 首条回执后错误时序分支存活数为 0；
- 一致关键节点的完整证明调用为 0；
- 完整决定 P95≤8 ms、P99≤15 ms、最大值<30 ms；
- `manifest.json`、`summary.json`、压缩逐决定记录和源码哈希完整。

任一项未通过：在验收文档记录 M0 未通过并停止，不提交 M1 代码。

- [x] **Step 7: 提交 Task 2**

```powershell
git add experiments/motion_navigation/trajectory_proto/d101_continuous_probe.py experiments/motion_navigation/trajectory_proto/d101_measurement.py scripts/measure_continuous_motion_d101.py tests/motion_nav/test_d101_continuous_probe.py tests/motion_nav/test_d101_measurement.py docs/motion_navigation/acceptance/continuous-motion-minimal-probe-d101.md evidence/motion_navigation/continuous-motion-d101-v1/m0
git commit -m "test(motion): validate D101 single-jump stream"
```

### Task 3: M1 连续两跳、提前计算和落地区域比较

**Files:**
- Modify: `experiments/motion_navigation/trajectory_proto/d101_continuous_probe.py`
- Modify: `experiments/motion_navigation/trajectory_proto/d101_measurement.py`
- Modify: `scripts/measure_continuous_motion_d101.py`
- Modify: `tests/motion_nav/test_d101_continuous_probe.py`
- Modify: `tests/motion_nav/test_d101_measurement.py`

**Interfaces:**
- Consumes: M0 输入流、现有 `jump_gap_1:A3` 输入、`PhysicsWorldView` 和 Windows `multiprocessing.get_context("spawn")`。
- Produces:
  - `TwoJumpProbeSpec(platform_length_blocks: Literal[2, 3, 4], start_branch: StreamStartBranch, landing_forward_offset_blocks: Literal[-0.05, 0.0, 0.05], repetitions: int)`。
  - `PreparedSecondJump(generation_id: str, entry_envelope: KeyNodeEnvelope, inputs: tuple[TickInput, ...], proof_identity: tuple)`。
  - `D101PrecomputeWorker.submit(request: SecondJumpPreparationRequest) -> bool`、`poll() -> PreparedSecondJump | None`、`close() -> None`、context manager；内部使用单个 spawn 子进程和容量为 1 的请求／结果队列。
  - `SecondJumpPreparationRequest`：`generation_id`、第一跳空中观察、第二跳入口目标、世界身份和依赖。
  - `run_two_jump_probe(spec: TwoJumpProbeSpec, *, worker_factory=D101PrecomputeWorker, tick_duration_ns: int = 50_000_000) -> tuple[TwoJumpProbeResult, ...]`。
  - `TwoJumpProbeResult`：终态、两次输入流 identity、第一跳着地状态、第二跳准备提交与交付 tick、初始着地比较结果、需要时从真实落点重求解的提交／交付 tick、完整证明次数、接续空档、完整停车 tick、worker 关闭状态、安全事件和逐决定耗时。
  - `M1Summary`：schema 版本、源码与输入哈希、180 项矩阵计数、按平台长度／偏差／起点分支的分层结果、worker 生命周期、P50／P95／P99／最大值、安全违规和总判定。
  - `run_m1(*, repetitions: int, output: Path) -> M1Summary`。

- [ ] **Step 1: 写两跳 fixture 和边界失败测试**

```python
def test_two_jump_fixture_has_two_one_cell_gaps_and_platform_lengths_2_to_4(): ...
def test_forward_landing_offsets_minus_zero_plus_point_zero_five_remain_supported(): ...
def test_offset_outside_the_prepared_envelope_requires_new_proof(): ...
```

只建立朝南的隔离 fixture；前向偏差在第一跳着地观察形成，不改写原输入回执。

- [ ] **Step 2: 写提前计算和 generation 测试**

```python
def test_second_jump_is_submitted_after_first_takeoff_and_before_first_landing(): ...
def test_matching_landing_uses_comparison_only_and_keeps_prepared_inputs(): ...
def test_landing_outside_prepared_envelope_replans_from_observed_state(): ...
def test_stale_worker_generation_is_discarded_without_releasing_the_first_owner(): ...
def test_worker_closes_after_success_exception_and_early_stop(): ...
```

测试必须读取公开诊断，不访问 worker 私有字段。子进程检查按名称 `d101-precompute` 断言结束后为 0。

- [ ] **Step 3: 运行两跳测试并确认失败**

Run: `.venv\python.exe -m unittest tests.motion_nav.test_d101_continuous_probe -q`

Expected: FAIL，缺少双跳 fixture 和 worker。

- [ ] **Step 4: 实现双跳闭环与隔离 worker**

第一跳首次进入空中时提交第二跳准备。worker 只计算现有冻结动作的第二份候选；第一跳 owner 持续负责身体。着地状态落在 `entry_envelope` 时只比较并接纳，不能重复完整证明。着地状态超出该范围时，旧候选失效；同一 worker 从真实着地状态重新求解。等待期间由预登记的短地面安全前段继续前进，不得停车等结果，也不得越出平台。

正式 M1 runner 以每 tick 50 ms 的固定节拍推进隔离客户端，sleep 不计入决定耗时。控制逻辑不读取经过时间选择输入；它只在每个模拟 tick 边界轮询一次 worker。worker 未在着地前交付即按预登记门槛失败，不能暂停模拟等待结果。

所有 worker 使用 `with D101PrecomputeWorker() as worker:`。`close()` 必须发送关闭哨兵、等待有界 join，并在超时后终止子进程；异常和提前返回也执行。传给子进程的世界必须是本试验所需的小型独立快照，不能序列化主项目活世界。

- [ ] **Step 5: 实现 M1 矩阵汇总并运行组件检查**

Run: `.venv\python.exe -m unittest tests.motion_nav.test_d101_input_stream tests.motion_nav.test_d101_continuous_probe tests.motion_nav.test_d101_measurement -q`

Expected: PASS。

- [ ] **Step 6: 提交 Task 3**

```powershell
git add experiments/motion_navigation/trajectory_proto/d101_continuous_probe.py experiments/motion_navigation/trajectory_proto/d101_measurement.py scripts/measure_continuous_motion_d101.py tests/motion_nav/test_d101_continuous_probe.py tests/motion_nav/test_d101_measurement.py
git commit -m "feat(experiments): add D101 two-jump continuity probe"
```

### Task 4: M1 Windows 正式运行与阶段止损

**Files:**
- Modify: `docs/motion_navigation/acceptance/continuous-motion-minimal-probe-d101.md`
- Evidence: `evidence/motion_navigation/continuous-motion-d101-v1/m1/`

**Interfaces:**
- Consumes: Task 3 `run_m1()` 与预登记矩阵。
- Produces: 冻结 `M1Summary`、逐场记录、性能样本、worker 生命周期记录和阶段结论。

- [ ] **Step 1: 运行正式 M1**

Run: `.venv\python.exe scripts/measure_continuous_motion_d101.py M1 --repetitions 10 --output evidence/motion_navigation/continuous-motion-d101-v1/m1`

Expected: 3 个平台长度 × 2 个起点分支 × 3 个落地偏差 × 10 次，共 180 次。

- [ ] **Step 2: 核对冻结门槛**

必须同时满足：

- 180／180 完成两跳；
- 掉落、伤害、安全违规、UNKNOWN 放行和无人负责推进均为 0；
- 第二跳 180／180 在第一跳空中提交准备，且在第一跳着地前交付；
- 初始着地状态仍在入口范围的运行只比较，完整证明调用增量为 0；
- 初始着地状态超出入口范围的运行全部拒绝旧候选，从真实落点重新求解，并在安全前段离开平台前交付；
- 前向 −0.05／0／+0.05 三组掉落均为 0；
- 无偏差组着地到后续有效输入空档≤1 tick；偏差重求解组不得完整停车，且必须在现有安全前段耗尽前接入第二跳；
- 旧 generation 接纳数为 0；
- 每场结束后的 `d101-precompute` 活子进程为 0；
- 完整决定 P95≤8 ms、P99≤15 ms、最大值<30 ms。

- [ ] **Step 3: 运行受影响回归**

Run: `.venv\python.exe -m unittest tests.motion_nav.test_incremental_motion_controller tests.motion_nav.test_incremental_motion_proof tests.motion_nav.test_incremental_motion_closed_loop tests.motion_nav.test_trajectory_proto_m0_probe tests.motion_nav.test_d101_input_stream tests.motion_nav.test_d101_continuous_probe tests.motion_nav.test_d101_measurement -q`

Expected: PASS。

- [ ] **Step 4: 记录 M1 结论并决定是否允许 M2**

将结果追加到验收文档，预登记部分保持原字节。任一门槛未通过则写明失败并停止；不得创建或运行 M2 Fabric 批次。

- [ ] **Step 5: 提交 Task 4**

```powershell
git add docs/motion_navigation/acceptance/continuous-motion-minimal-probe-d101.md evidence/motion_navigation/continuous-motion-d101-v1/m1
git commit -m "test(motion): freeze D101 two-jump Windows results"
```

### Task 5: M2 最小 Fabric tick 顺序测量

**Files:**
- Create: `deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/D101TickTrace.java`
- Modify: `deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/DeploymentObservationProbe.java`
- Modify: `deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/DeploymentDiagnostics.java`
- Modify: `deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/mixin/BehaviorPlayerMixin.java`
- Create: `scripts/d101_fabric_tick_runtime.py`
- Create: `scripts/run_d101_fabric_tick_probe.py`
- Modify: `scripts/probe_fabric_deployment_observation.py`
- Create: `tests/motion_nav/test_d101_fabric_tick_probe_options.py`
- Create: `tests/java/D101TickTraceTest.java`
- Modify: `tests/test_standalone_java_gates.py`
- Modify: `docs/motion_navigation/acceptance/continuous-motion-minimal-probe-d101.md`
- Evidence: `evidence/motion_navigation/continuous-motion-d101-v1/m2/`

**Interfaces:**
- Consumes: M0／M1 已通过的 manifest；既有 deployment transport、观察、动作和 input sample。
- Produces:
  - `D101TickTrace.Event(kind, episodeId, requestSequenceId, observationSequenceId, movementTickId, jvmNs)`。
  - 事件种类：`LANDING_OBSERVATION_EMITTED`、`CLIENT_FRAME_ARRIVED`、`INPUT_SAMPLED`、`MOVEMENT_APPLIED`。
  - Python 侧事件：`PYTHON_DECISION_STARTED`、`PYTHON_DECISION_COMPLETED`。
  - `TickEvent`：进程、事件种类、episode、request／observation sequence、movement tick 和本进程单调时钟。
  - `TickOrderKind(StrEnum)`：`NEXT_MOVEMENT_TICK`、`LATER_MOVEMENT_TICK`、`INCOMPLETE`。
  - `TickOrderResult`：分类、关联 identity、着地 tick、读取 tick、应用 tick 和完整性原因。
  - `classify_tick_order(events: tuple[TickEvent, ...]) -> TickOrderResult`。

- [ ] **Step 1: 写 Java 记录器和 Python 参数失败测试**

Java 测试覆盖有界容量、顺序、重复 identity 和 drain 后清空。Python 测试覆盖四方向、10 次重复、缺少 M0／M1 pass manifest 时拒绝启动，以及不接受“正式队列”相关选项。

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv\python.exe -m unittest tests.motion_nav.test_d101_fabric_tick_probe_options tests.test_standalone_java_gates -q`

Expected: FAIL，缺少 runner／Java 记录器。

- [ ] **Step 3: 实现 deployment-only 时间线**

`DeploymentObservationProbe` 在 step frame 被客户端线程 poll 到时记录 `CLIENT_FRAME_ARRIVED`；`BehaviorPlayerMixin` 在现有 input sample 后和 movement tail 后分别记录 `INPUT_SAMPLED`、`MOVEMENT_APPLIED`。landing observation 发出时记录其 observation sequence 和 movement tick。`probe_fabric_deployment_observation.py` 只在 `MC2P_D101_TICK_PROBE=1` 时选择 `run_d101_fabric_tick_runtime()`，其他 probe 路径逐字节保持原行为。

只增加诊断，不改变动作 admission、lease、输入值、tick 顺序或 transport queue。不得修改 `ClientBehaviorInput.java`。

- [ ] **Step 4: 实现 Python 侧决定记录和完整性分类**

Python 决定开始／结束使用 Python 单调时钟，只用于本进程顺序。JVM `System.nanoTime()` 只用于 JVM 内顺序。禁止直接相减两个进程的纳秒值；跨进程结论只按 observation sequence、request sequence 和 movement tick 关联。

任何事件缺失、重复、identity 不一致或 tick 回退都返回 `INCOMPLETE`，不能回答“能否赶上”。

- [ ] **Step 5: 运行直接检查**

Run: `.venv\python.exe -m unittest tests.motion_nav.test_d101_fabric_tick_probe_options tests.test_standalone_java_gates -q`

Expected: PASS。

- [ ] **Step 6: 运行四方向 Fabric 探针**

仅在 M0／M1 manifest 均为通过时运行：

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/run_d101_fabric_tick_probe.py --repetitions 10 --seed 21101 --server-port 25597 --ipc-port 8140 --timeout-seconds 900
```

Expected: 四方向各 10 次，共 40 次完整时间线。每次在第一跳着地观察送达 Python 后立即生成普通前进命令，并记录它是否在下一个 movement tick 被采样和应用。

M2 不要求结果必须是 `NEXT_MOVEMENT_TICK`。通过条件只是 40／40 时间线完整、分类可复核、没有修改输入行为。实际分布决定后续设计采用逐 tick 发送、有限预排程还是组合方式。

- [ ] **Step 7: 记录 M2 结果并提交**

```powershell
git add deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/D101TickTrace.java deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/DeploymentObservationProbe.java deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/DeploymentDiagnostics.java deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/mixin/BehaviorPlayerMixin.java scripts/d101_fabric_tick_runtime.py scripts/run_d101_fabric_tick_probe.py scripts/probe_fabric_deployment_observation.py tests/motion_nav/test_d101_fabric_tick_probe_options.py tests/java/D101TickTraceTest.java tests/test_standalone_java_gates.py docs/motion_navigation/acceptance/continuous-motion-minimal-probe-d101.md evidence/motion_navigation/continuous-motion-d101-v1/m2
git commit -m "test(fabric): measure D101 client tick order"
```

### Task 6: 最终证据核对和阶段收尾

**Files:**
- Modify: `docs/motion_navigation/stages/continuous-motion-minimal-probe-d101-plan.md`
- Modify: `docs/motion_navigation/acceptance/continuous-motion-minimal-probe-d101.md`
- Modify: `AGENTS.md`
- Evidence: `evidence/motion_navigation/continuous-motion-d101-v1/manifest.json`

**Interfaces:**
- Consumes: 最后完成阶段的不可变 summary、源码哈希和测试输出。
- Produces: 当前阶段结论；不产生正式能力声明。

- [x] **Step 1: 核对证据来源和 worker 关闭**

从原始 JSON／JSONL 重算 M0／M1／M2 汇总。确认所有 worker close 记录存在，运行后工作树没有临时进程或未登记证据。

- [x] **Step 2: 运行完整实验相关检查**

Run: `.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_m0_probe tests.motion_nav.test_incremental_motion_controller tests.motion_nav.test_incremental_motion_proof tests.motion_nav.test_incremental_motion_closed_loop tests.motion_nav.test_d101_input_stream tests.motion_nav.test_d101_continuous_probe tests.motion_nav.test_d101_measurement tests.motion_nav.test_d101_fabric_tick_probe_options tests.test_standalone_java_gates -q`

Expected: PASS；若 M2 未获授权或因 M0／M1 失败未运行，M2 测试仍须通过，证据目录明确不存在而非伪造跳过结果。

- [x] **Step 3: 更新阶段状态**

只写事实：最后通过到 M0、M1 或 M2；D098 仍失败；D101 原型未接入 `mc2p`；Fabric 测量不等于正式客户端队列能力。

- [x] **Step 4: 提交 Task 6**

```powershell
git add AGENTS.md docs/motion_navigation/stages/continuous-motion-minimal-probe-d101-plan.md docs/motion_navigation/acceptance/continuous-motion-minimal-probe-d101.md evidence/motion_navigation/continuous-motion-d101-v1/manifest.json
git commit -m "docs(motion): close D101 minimal probe"
```

## 执行交接

用户已经指定 subagent-driven。实施时必须使用 `superpowers:subagent-driven-development`，按 Task 1→6 顺序执行；M0 或 M1 失败时，后续任务按止损规则停止。每个实现任务由新子 agent 完成，并在进入下一任务前由独立 reviewer 检查。

## Task 6 收尾结果与范围裁决

M0 实施提交为 `97b13ea5c160becdb6ec0b86482d5ded537e76ad`。原始 204 场、4,710 个决定已重算；独立核验重新计算 204 份证明并回放 4,707 次身体推进，新增性能样本为 0。正式失败仍为 `p99_above_15ms`、`max_not_below_30ms`，不能进入 M1。D098 的 P3 失败保持不变。

Task 6 原命令包含 `tests.motion_nav.test_d101_fabric_tick_probe_options`，该模块由 Task 5 才创建。M0 失败后 Task 5 未获授权，因此本次执行其余九个已存在模块，并把缺失模块记为“未授权、未创建、未运行”。没有为检查通过补建 M2 文件；原命令中“M2 测试仍须通过”的预期不适用于尚未授权创建的模块，也不能解释为跳过通过。

收尾首轮检查暴露 Task 2 未登记的实验 runner 导入和 shell 缺少 JDK 路径。限定修正只在既有实验导入检查中登记 `scripts/measure_continuous_motion_d101.py` 的两项导入，并检查其他脚本和正式模块仍被拒绝；JDK 21.0.10 只在检查命令中临时设置，没有修改配置。最终命令和结果见验收与根证据清单。

M0 未创建 worker，因此没有应补写的 close 记录；原 manifest 的 `worker_created`、`worker_alive_at_end` 均为 0。M1 预计算 worker 接口、M2 文件及两个证据目录均不存在。正式链、deployment 探针和实验行为代码未修改，原 M0 证据保持不变。真实 Fabric tick 顺序仍未知，不能据此选定预排程或宣称正式客户端队列、完整生命周期或连续两跳已交付。
