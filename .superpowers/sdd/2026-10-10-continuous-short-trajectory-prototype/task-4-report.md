# Task 4 实现报告

Task 4 已实现显式候选承诺扫描，基线为 `77e05bda30c723da12a6e1505a03083e70faf396`，聚焦提交为 `588c6baaf783ca9d629b58ce49b15b210b628010`（`2026-10-10T12:09:32+08:00`）。生产 `mc2p` 未修改，没有新增搜索器、worker、Runtime 接入或动作迁移。报告保留在本地代理目录，不纳入源码提交。

已读取 brief 中全部必读文件，并按 `superpowers:test-driven-development` 的先失败再实现流程执行；完成前按 `superpowers:verification-before-completion` 核对实际检查输出。

## RED 与 GREEN

先创建 `tests/motion_nav/test_trajectory_proto_commitment.py`，当时 `commitment.py`／`physics.py` 尚不存在。执行：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_commitment -v
```

实际失败输出：

```text
ModuleNotFoundError: No module named 'experiments.motion_navigation.trajectory_proto.commitment'
Ran 1 test in 0.000s
FAILED (errors=1)
```

这次 RED 来自 API 缺失。随后最小实现，同一命令实际运行 11 项，10 项通过，落地关闭测试失败：

```text
test_landing_closes_but_preserves_previous_risk_interval ... FAIL
AssertionError: False is not true
Ran 11 tests in 0.325s
FAILED (failures=1)
```

原因是停车尾迹已能安全松键时，扫描器就关闭区间，身体当时仍在空中。依 D095 的落地责任边界，改为真实入口状态已经着地且停车尾迹安全才关闭；同一命令得到 `Ran 11 tests in 0.327s / OK`。

随后补充停车尾迹专属 UNKNOWN、第二风险区间、身份过期和提交前缺输入应用证据。实际第二次行为 RED：

```text
test_missing_input_application_before_submission_is_information ... FAIL
AssertionError: <ScanStatus.STALE: 'STALE'> is not <ScanStatus.NEEDS_INFORMATION: 'NEEDS_INFORMATION'>
Ran 15 tests in 0.444s
FAILED (failures=1)
```

修正提交检查：原账本身份未变但应用事实缺失时，先返回 `NEEDS_INFORMATION/MISSING_INPUT_APPLICATION`，不假设中性，也不把缺事实改叫过期。同一命令得到 `Ran 15 tests in 0.447s / OK`。最后把双分支测试改为入口位置分别为 z=0.50／0.52，确认它们用同一条输入却产生不同预测状态；最终再次运行该命令：`Ran 15 tests in 0.444s / OK`。

## 验证命令与实际结果

brief 的 `tests.motion_nav.test_motion_solver_recovery` 不存在。先用 `rg -n '_release_recovery_evidence' tests` 查到真实覆盖模块 `test_b10_gap_solver` 和 `test_motion_continuation_quality`，两者全部加入验证，没有跳过相关检查。

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment tests.motion_nav.test_b09r_physics_step tests.motion_nav.test_b09r_physics_rollout tests.motion_nav.test_b10_gap_solver tests.motion_nav.test_motion_continuation_quality -q
```

```text
Ran 93 tests in 52.889s
OK
```

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_standalone_export -q
```

```text
Ran 12 tests in 27.562s
OK
```

93 项组合检查之后没有修改实现，只加强双分支测试的入口差异断言，并重新运行最终 15 项组件检查。公开导出排除通配已覆盖本次模块，不修改导出配置或脚本。

`git diff --check` 与 `git diff --cached --check` 均返回 0。提交后的 `git status --short` 为空，提交只含 5 个 brief 范围内文件。正式解释器均为项目 Windows `.venv\python.exe`；没有使用 Linux 结果代替项目门槛。

## API 与证明

- `physics.py`：`CountedPhysics` 包装现有 `step()`，累计节点、全部物理调用和停车 tick；`trajectory_digest()` 用排序后的 JSON 计算 SHA-256，不依赖集合迭代顺序或墙钟。
- `commitment.py`：`scan_commitment(request, inputs, world, boundary_inputs, options=...)` 重放一个共同、不可变 `TickInput` 候选。每个入口分支分别提供逐边界 `BoundaryInputs`。已知无在途输入为 `()`，应用事实未知为 `None`，二者不能混用。在途输入必须与共享候选该边界的继续输入一致。
- `StopTail` 保存每个边界的有类型判定、实际先消费的输入、停车预测状态、依赖和伤害。`BranchScan` 保存实际候选预测状态和 `RiskInterval`。区间保留最后可放弃边界、首个承诺边界与真实落地后恢复可停的边界。初始就不能放弃时，最后可放弃边界为 `None`。
- `CommitmentProof` 绑定完整请求（入口、目标、剩余任务伤害额度、世界与账本身份、预算、同一前缀）、共同候选输入、两支边界应用事实、全部候选及停车依赖事实、Java 1.21 ruleset、停车上限、计数和轨迹哈希。
- `validate_commitment()` 在指定候选提交边界检查已绑定身份、输入应用事实、证明哈希与依赖。绑定格被移除或改成 UNKNOWN 时返回 `STALE/WORLD_DEPENDENCY`；远处未绑定事实变化不重新选择或替换证明。目标、锚点、请求或账本身份过期分别保留 typed reason。

扫描输出使用独立 `ScanStatus`：`VERIFIED_CANDIDATE`、`CANDIDATE_REJECTED`、`NEEDS_INFORMATION`、`NO_TRAJECTORY_IN_BUDGET`、`STALE`。这不是新搜索结果类别，Task 3 的五类搜索接口保持不变。候选安全扫描不能签发请求级 `FOUND`；候选拒绝不能签发请求级 `BLOCKED`。目标达成与请求级必要条件由 Task 5 的参考搜索另行证明。

## 安全与预算边界

源码直接调用现有 `step()`／`PhysicsWorldView`／`JAVA_1_21_RULESET`，不复制运动公式。输入世界必须是 detached 快照；此任务不替调用者采集世界、猜测时序或生成在途账本。两支入口是请求已经声明的状态假设，扫描器分别消费它们和同一候选，不自行选择有利分支。

停车复用正式 `_release_recovery_evidence` 的安全语义：入口标记着地不能直接通过；实际逐 tick 计算接触，速度降到 0.01 格／tick 后还要继续计算到水平速度为零。支撑不能低于入口与正常出口两者较低的高度，伤害不能高于候选正常结局。阈值在原型中写明来源，未导入生产私有常量或函数。

候选的伤害额度表示锚点时仍可使用的任务余额，调用者不能传入已经花费过的原始总额度。单个正式动作按最大落距估算伤害；本原型允许多个风险区间，保守累计每次落地的普通下落伤害，再与剩余额度比较。停车尾迹把已走前缀和尾迹一起计算，不能用新尾迹重置已发生的伤害。当前组件正例均为零预期伤害，没有证明新的受伤恢复能力。

节点计数为每支候选重放一次，以及每个边界的停车扫描一次。所有成功与不完整的 `step()` 尝试都计入统一物理预算。共同候选受 `max_trajectory_ticks` 限制；每条停车尾迹另受 `ScanOptions.max_tail_ticks` 限制（默认 40，允许 1—80）。尾迹上限耗尽归入 `NO_TRAJECTORY_IN_BUDGET/TRAJECTORY_TICK_BUDGET`，不说不可达。两支、节点和物理 step 上限分别保留各自 reason。决策源码不导入或读取墙钟。

小型一格跨隙组件中，入口为 (0.5,1.0,0.5)，速度为 (0,-0.0784,0.1)，先 Walk，再 Jump＋10 tick Walk，最后 21 tick 中性。只在边界 1 声明已经无法撤回的 Jump。得到最后可放弃／承诺／恢复边界 **0／1／13**；状态 1 仍真实着地，状态 2 才离地。计数为 **35 nodes／238 physics steps／205 tail ticks**。同一边界不带在途 Jump 时可停，带在途 Jump 时不可停。

两个跨隙组件在 40 tick 共同候选中保留 **0／1／13** 和 **24／25／37** 两个区间。正常候选读取完整、停车分支单独遇到 UNKNOWN 的独立组件也成立，不能只依赖候选本身 UNKNOWN 的测试。

## 文档与未执行范围

TP stage／acceptance 已登记 P0 开始，以及首笔原型提交 `7d997913e4a6e1a406be5309b5b4b01c0681f88c` 和时间 `2026-10-10T11:52:53+08:00`。两周时间盒截止为 `2026-10-24T11:52:53+08:00`，不能等到 Task 5 才重新计时。

Task 4 只实现组件级显式候选安全扫描。未执行参考搜索、18 项完整冻结场景覆盖、P0 Windows 性能量尺、完整运动导航、闭环模拟、Fabric 或 worker 生命周期验证。不把组件检查写成 P0 验收通过，也不冻结 P1 预算。

D095 与 AGENTS.md 中旧的“P0 尚未开始”句子未修改，因为它们不在 Task 4 brief 允许文件范围内；已通知父任务在最终入口状态同步时处理。Task 3 contracts 无需改动，原型源码共新增 367 行，未创建后续空壳模块。
