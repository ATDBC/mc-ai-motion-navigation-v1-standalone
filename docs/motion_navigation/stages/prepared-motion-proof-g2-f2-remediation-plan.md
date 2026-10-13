# D102 G2-F2 最后一轮修正 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

日期：2026-10-13。状态：**Task A／B 已完成；Task C 触发止损，Task D 与正式 F2 未运行。** 基线为 `2bb64336e63761229d6f4ffe26f00b6f71f12060`。

**Goal:** 修复 G2 量尺窗口分叉，消除正常控制决定中的重复 JSON 容量校验和对象图遍历，并在不降低安全门槛的前提下完成唯一一次 F2 fast 重跑。

**Architecture:** 动作决定只通过一个共同接口生成 Runtime 执行窗口。观察请求对固定原生字段使用可复算上界，激活与引用值只在本进程已校验事实上使用窄构造；公共构造、receiver 和外部输入继续完整校验。typed 失败结束正常动作循环，身体收尾仍由原 owner 负责。

**Tech Stack:** Python 3.11.16、Windows、`unittest`、现有 Runtime／MotionResultInbox／MotionSolverWorker、JSON/JSONL、SHA-256。

**Spec:** [D103](../decisions/0103-repair-g2-window-and-control-hot-path.md)、[D102 架构](../architecture/prepared-motion-proof-pipeline-v1.md)、[D102 正式验收](../acceptance/prepared-motion-proof-pipeline-d102.md)。

## Global Constraints

- G2-F2 是第二轮也是最后一轮限定修正；三个代码批次只购买一次正式 fast 重跑。
- 原 fast 与 G2-F1 raw、index、summary、失败指针和哈希不改写。
- 每批按真实 RED→GREEN→回归→独立审查执行；缺方法、ImportError 和夹具异常不算 RED。
- 不全局关闭不可变图校验，不把 production 调用改标为 fixture backend，不扣除 profile 或模拟 backend 成本。
- 不新增 worker、receiver、registry、ledger、body owner 或普通地面输入包控制器。
- 不修改 UNKNOWN、伤害额度、入口范围、0／1 tick 语义、Runtime 默认精确 tick 或现有安全释放门槛。
- Windows 是正式平台。Task 10、G3、Fabric 均保持未授权。

## Review Focus

- 只缺一个执行窗口端点时必须拒绝，不能回退到精确 tick 或自动补一 tick。
- 默认观察请求复用不能把上一帧的动态 air positions 或 entity track 带入下一帧。
- 私有窄构造必须拒绝 list、dict、对象子类、旧 generation 和伪 handle。
- 真实 typed 失败结束正常循环后，原 owner、账本和安全尾迹仍保留，finally 不能偷偷释放。
- fallback 若没有已登记安全尾迹，必须停止并报告，不能现场 rollout 后发送 Neutral。

---

### Task A：统一执行窗口和 typed 失败驱动

**Files:**
- Modify: `mc2p/motion_nav/action_route_executor.py`
- Modify: `mc2p/motion_nav/navigation_session.py`
- Modify: `tests/sim/d102_prepared_motion.py`
- Modify: `tests/motion_nav/test_runtime_navigation_verified_handoff.py`
- Modify: `tests/motion_nav/test_d102_windows_matrix.py`

**Interfaces:**
- Produce: `decision_input_window(decision: ActionRouteDecision) -> MovementTickWindowV1 | None`（或同模块等价窄函数）。
- Consume: 既有 `expected_movement_tick`、`latest_movement_tick` 与 Runtime intent。

- [ ] **Step 1: 写 RED。** 使用 `fast-025` 坐标断言证明窗口 11／12、实际 tick 12 为 `APPLIED`、选择 late-one 分支并到达第一段出口。补半窗口、过期窗口、别的 intent 获胜和中段晚一拒绝。补 typed 失败后同一 observation 不重复终态 decide、不申请第二跳，但收尾仍计量。
- [ ] **Step 2: 确认 RED。** 只接受窗口漏传、重复 decide 或缺共同接口导致的断言失败。
- [ ] **Step 3: 最小实现。** 正式 Session 与量尺共同调用转换入口；只在两个端点齐全时产生窗口。量尺遇 typed 失败退出正常循环，单独运行原 owner 的必要收尾。
- [ ] **Step 4: GREEN。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_windows_matrix tests.motion_nav.test_d102_runtime_activation tests.motion_nav.test_runtime_navigation_verified_handoff tests.test_player_runtime_v1 -q`。
- [ ] **Step 5: 提交并独立审查。** Commit: `fix(motion): preserve verified input windows in G2 decisions`。

**止损：** 需要扩大 proof、全局默认晚一、隐藏 Neutral 来源或改变 Runtime 账本判定时停止。

### Task B：消除观察请求的重复 JSON 容量校验

**Files:**
- Modify: `mc2p/contracts/observation_request_v3.py`
- Modify: 调用 `merge_observation_requests` 的现有模块（只在证明需要时）
- Modify: `tests/test_observation_request_v3.py`
- Modify: `tests/motion_nav/test_d102_windows_matrix.py`

**Interfaces:**
- Produce: 已验证默认请求常量；内容未变时返回原不可变请求；固定原生字段的 `observation_request_wire_upper_bound(...) -> int`（名称可按现有模块习惯调整）。

- [ ] **Step 1: 写 RED。** 完整 `control_frame` 的默认请求和相同请求合并不得调用 `json.dumps`。覆盖 128 格最坏坐标、最长合法身份/profile、129 格、int 溢出、冲突实体、非原生标量和动态内容变化。
- [ ] **Step 2: 确认 RED。** 现实现必须因每次容量编码或缺复用失败。
- [ ] **Step 3: 最小实现。** 公共语义检查保持；固定原生字段用最坏情况上界证明 16 KiB；默认值和无变化合并复用。真实 backend 传输编码不改。
- [ ] **Step 4: GREEN。** Run: `& .\.venv\python.exe -m unittest tests.test_observation_request_v3 tests.test_player_runtime_v1 tests.motion_nav.test_d102_windows_matrix -q`。
- [ ] **Step 5: 提交并独立审查。** Commit: `perf(observation): reuse validated request values on the control path`。

**止损：** 需要改 profile、16 KiB 上限、wire 内容、backend 归属或通过改名规避计数时停止。

### Task C：窄化本地激活与引用构造，核对失败收尾

**Files:**
- Modify: `mc2p/contracts/motion_activation.py`
- Modify: `mc2p/motion_nav/prepared_motion.py`
- Modify: `mc2p/motion_nav/motion_worker.py`
- Modify: `mc2p/motion_nav/action_route_executor.py`
- Modify: `mc2p/motion_nav/navigation_session.py`
- Modify: `tests/sim/d102_prepared_motion.py`
- Modify: `tests/motion_nav/test_d102_runtime_activation.py`
- Modify: `tests/motion_nav/test_d102_registry_lifecycle.py`
- Modify: `tests/motion_nav/test_d102_windows_matrix.py`

**Interfaces:**
- Produce: `PreparedStreamDispatch` 的有界本地构造；有限 `ReferenceResult` 常量；现有 proof-use port 增加 `candidate_release_evidence(handle: PreparedMotionHandle, consumer: PreparedProofConsumer) -> ProofReleaseEvidence | None`（名称可按现有 Protocol 习惯调整），只返回 registry record 内与精确 handle／candidate use 绑定的已验证退场事实。
- Consume: 现有 `_from_native_references`、registry identity/use/command 检查与 receiver 完整校验。

- [ ] **Step 1: 写 RED。** 从 Session propose／dispatch 到 Runtime 首回执和实际 `ActionRouteExecutor.discard_prepared_candidates` caller 的完整计量中，recursive graph walk 必须为 0。补 list/dict/子类、旧 generation、伪 handle、注销后引用、重封存和动态 executing／safe-tail 释放事实复用反例。补真实 spawn 的按时、`fast-025`、typed 失败、晚交付和中段缺失单例；检查现场 rollout。
- [ ] **Step 2: 确认 RED。** 只接受现有公共构造重复遍历或 fallback 现场求证导致的失败。
- [ ] **Step 3: 最小实现。** 只对已校验本地引用逐字段验证后构造；缓存有界并随 registry record 回收、计入 bytes。executor 通过现有窄 port 取得与精确 candidate handle 绑定的退场事实，再调用原 `release_usage`；不得自己重建或保存 handle map。动态命令终态和身体释放事实继续每次核对。量尺改用正式 proposal factory，不用 `dataclasses.replace`。
- [ ] **Step 4: GREEN。** Run: `& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_contracts tests.motion_nav.test_d102_receiver tests.motion_nav.test_d102_registry_lifecycle tests.motion_nav.test_d102_action_handoff tests.motion_nav.test_d102_runtime_activation tests.motion_nav.test_d102_windows_matrix tests.motion_nav.test_d102_evidence_contract -q`。
- [ ] **Step 5: 提交并独立审查。** Commit: `perf(motion): reuse validated activation and reference facts`。

**止损：** 任何拒绝／欠载路径仍必须现场生成新证明，或窄构造能接受外部可变值、错误身份时停止，不运行正式 F2。

### Task D：新 predecessor、唯一 F2 fast 与后续门槛

**Files:**
- Modify: `scripts/verify_prepared_motion_d102.py`
- Modify: `scripts/measure_prepared_motion_d102.py`
- Modify: `tests/sim/d102_prepared_motion.py`
- Modify: `tests/motion_nav/test_d102_evidence_contract.py`
- Modify: `tests/motion_nav/test_d102_windows_matrix.py`
- Record: `.tmp/d102-formal-v1/g2/f2/`
- On full pass only: `evidence/motion_navigation/prepared-motion-proof-d102-v1/g2/`

- [ ] **Step 1: 写 predecessor RED。** 旧 G1 接新候选、缺少 13 个 case 中任一项、source 索引遗漏 `observation_request_v3.py` 或其他改动文件、predecessor 源码哈希与 G2 不一致、错用 F1 根、前阶段 index/hash 改变时，必须在启动 worker 或写正式输出前拒绝；完整新 G1 与正确 F2 根才接纳。
- [ ] **Step 2: 建立候选源码对应的完整 G1 predecessor。** 冻结最终候选和证据工具后，在唯一新目录用现有 producer 重跑全部 13 个 case，生成一个完整同 run index；旧 G0/G1 不改，不跨 run 拼接。将新 G1 的显式路径、index hash 和 source hash 集合传给本轮 G2，每个后续阶段都绑定同一 predecessor。
- [ ] **Step 3: GREEN predecessor 门禁。** 独立 verifier 确认新 G1 的 13 项、当前候选完整 source（含 `observation_request_v3.py`）、G2 source 和 predecessor 逐文件一致；recompute 只读且不启动 worker。旧 G1 或任一篡改复制必须失败。
- [ ] **Step 4: 运行受控前置矩阵。** 一个真实 spawn 的按时、late-one、typed 失败、晚交付和中段缺失；正常与必要收尾的 serialization／graph／replay 禁止调用为 0，安全、资源和关闭门槛通过。
- [ ] **Step 5: 唯一一次 F2 fast。** 预登记 `.tmp/d102-formal-v1/g2/f2/fast`，显式传入新 G1 predecessor，运行完整 144 正例及 12 类负例；第一次失败立即停止并保留 raw。只有 fast 全过才依次运行 spawn-smoke、realtime、fallback 和 G2 verifier。realtime 才能签署 8／15／30 ms；fast 耗时只诊断。通过则导出紧凑 G2 证据；失败则新增明确 FAILED 指针。更新正式验收和阶段状态，提交 `test(motion): rerun the final D102 G2 candidate`。

**止损：** 任一前置、fast 或后续阶段失败即停止。不存在 F3；Task 10、G3 与 Fabric 保持未授权。

## 实施结果

Task A 已完成并通过独立审查，提交为 `552a5673`。正式 Session 与 G2 runner 共用执行窗口转换；`fast-025` 的 11／12 窗口不再在量尺适配处丢失。typed 失败结束正常动作循环，必要 closeout 单独计量；未解决责任保留 owner、账本、执行和尾迹引用，不追加未证明 Neutral。

Task B 已完成并通过独立审查，提交为 `6f5c1c74`、`d555c059`。观察请求的控制路径容量编码降为 0；保守上界为 5,112 bytes。公共字段校验、16 KiB／128 格门槛和真实 backend JSON 编码保持不变，身份字符集合变更会使上界保护测试失败。

Task C 在写生产实现前触发本计划的止损条件。六个真实 spawn 诊断显示：legacy 第一段只有 `install_verified_motion`，没有 registry handle 或登记的 safe-tail；late delivery／fallback 各在控制线程现场执行 `verified_ground_rollout` 2 次，正常 closeout 执行 3 次。中段缺失后 executing／safe-tail、lease 和 bytes 正确保留，后台线程与进程归零。三项正式 RED 共 6 个真实断言失败，独立止损核验 5／5。

因此 Task C 没有实现、没有 GREEN。继续需要为第一段建立可登记的安全尾迹及其 owner／释放生命周期，或重新设计释放契约，已经超出 D103 的有限可信构造范围。Task D、新 G1 predecessor、唯一正式 F2、Task 10 和 Fabric 均未运行。G2 继续为 **FAILED／STOPPED**。
