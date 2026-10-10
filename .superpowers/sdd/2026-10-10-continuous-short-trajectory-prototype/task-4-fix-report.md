# Task 4 限定修正报告

本轮按独立审查 `1964afd6db04ff14647cba8b439c71e27de9aa2e` 修正两个提交校验问题，并补齐真实伤害测试。聚焦提交为 `2732e39a7ffd4f087acfe8a92fc21c9c6f380c58`，时间 `2026-10-10T12:25:32+08:00`。修改限定在 `commitment.py`、组件测试、D095 和仓库根 AGENTS.md；生产代码、既有历史报告和失败证据未修改。Task 3／4 组件已完成，P0 仍未签署。报告保存在本地代理目录，不纳入源码提交。

## 修正后的行为

`validate_commitment(..., boundary=1)` 先检查请求、世界、目标、锚点和账本身份，再检查证明哈希及全部已绑定世界事实。只有这些检查仍有效时，才读取每个时序分支的当前边界 1。过去边界 0、未来边界 20 和末尾边界 33 缺少应用事实，都不阻塞当前已验证的提交，也不改变原证明。

当前边界必须覆盖全部声明分支。任何分支已知的输入与证明不同，都返回 `STALE/INPUT_LEDGER`，不重扫描。如果身份仍有效、当前边界没有已知矛盾，但任一分支缺少当前应用事实，则返回 `NEEDS_INFORMATION/MISSING_INPUT_APPLICATION`。校验不调用物理展开，计数为零。

请求 ID 过期仍为 `STALE/REQUEST_IDENTITY`；世界 session 或绑定事实过期为 `STALE/WORLD_DEPENDENCY`；目标、入口锚点和账本身份分别使用 `GOAL_REVISION`、`ANCHOR`、`INPUT_LEDGER`。这些明确过期的结果优先于应用事实缺失。全局 geometry revision 因未绑定事实变化而增加时，仍按绑定事实校验，不重新挑选证明。

扫描接口没有改变：`scan_commitment()` 仍要求所有候选边界的应用事实完整，并绑定完整证明。修改的是实际提交时应该检查哪一个边界，不能把未来实际回执作为当前提交的前提。

## 先 RED，再修正

先保留原有 15 项测试，新增 6 项：当前边界检查、过期优先级、当前输入矛盾、一笔非零落地伤害、两次落地累计、停车尾迹保留已走前缀伤害。尚未改实现时执行：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_commitment -v
```

实际输出：

```text
test_current_input_disagreement_is_typed_stale_without_rescan ... ERROR
ContractViolation: irrevocable inputs must agree with the shared candidate continuation
test_stale_identity_precedes_current_missing_application ... FAIL
AssertionError: NEEDS_INFORMATION is not STALE
test_submission_uses_current_boundary_only_for_each_branch ... FAIL
AssertionError: NEEDS_INFORMATION is not VERIFIED_CANDIDATE
Ran 21 tests in 0.746s
FAILED (failures=6, errors=1)
```

过期组合的 5 个失败对应请求 ID、世界 session、目标、锚点以及绑定支撑变化。账本 ID 变化原本就能返回过期。当前已知输入与共同候选不一致时，原实现抛出合同异常；新测试要求它报告 typed `INPUT_LEDGER` 过期。

随后最小修改 `validate_commitment()`，同一命令实际输出：

```text
Ran 21 tests in 0.918s
OK
```

## 真实伤害与测试有效性

伤害实现 `_damage()` 本轮没有修改。测试世界用已知方块构成顶面 y=9、5、1 的两级落地，入口为 `(0.5,9.0,0.5)`，速度为 `(0,-0.0784,0.1)`，`fall_distance_blocks=0`；候选为 40 tick 普通 Walk。落距完全由现有 `step()` 计算，没有手工填入受伤落距。

一笔落地在 tick 14 发生，落地前 `fall_distance_blocks=3.3462703824114044`，落地后重置为 0，预测伤害为 1 点。剩余额度 0 拒绝，额度 1 通过。

两次落地分别在 tick 14 和 30 发生。每次落地前落距相同，累计伤害为 2 点。剩余额度 0／1 均返回 `CANDIDATE_REJECTED/DAMAGE_ALLOWANCE`，额度 2 通过。测试使用游戏落地前保存的 fall_distance，不把顶面之间 4 格的总高度差强加到落距上，也不添加落地那一 tick 已清零的下降量。

停车尾迹也有实际断言：tick 14 已落地、随后中性停车全程着地，尾迹仍保存已发生的 1 点伤害；tick 25 处于第二次下落，停车尾迹保存前缀 1 点加本次落地 1 点，共 2 点；最后的停车尾迹仍为 2 点。这些值能够发现尾迹重置余额的退化。

在改实现前运行 `_damage()` 置零的独立内存变异。完整命令：

```powershell
@'
import unittest
from unittest.mock import patch
from tests.motion_nav.test_trajectory_proto_commitment import TrajectoryProtoCommitmentTests
names = (
    'test_nonzero_landing_damage_requires_available_allowance',
    'test_two_landings_spend_cumulative_allowance',
    'test_stop_tail_keeps_damage_already_spent_in_candidate_prefix',
)
suite = unittest.TestSuite(TrajectoryProtoCommitmentTests(name) for name in names)
with patch('experiments.motion_navigation.trajectory_proto.commitment._damage', return_value=0.):
    result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
'@ | .\.venv\python.exe -
```

实际输出：`Ran 3 tests in 0.151s / FAILED (failures=3)`，退出码 1。零伤害额度错误接受受伤候选，两次累计错误接受额度 1，停车尾迹伤害 0 而非 1，三项均被发现。

另外对“只取最大落距”和“尾迹丢掉前缀”做独立内存变异，没有写入源码：

```powershell
@'
import unittest
from unittest.mock import patch
import experiments.motion_navigation.trajectory_proto.commitment as implementation
from tests.motion_nav.test_trajectory_proto_commitment import TrajectoryProtoCommitmentTests
from mc2p.motion_nav.motion_risk import conservative_plain_fall_damage_points
max_only = lambda states: conservative_plain_fall_damage_points(max(s.fall_distance_blocks for s in states))
with patch.object(implementation, '_damage', side_effect=max_only):
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([
        TrajectoryProtoCommitmentTests('test_two_landings_spend_cumulative_allowance')]))
    assert len(result.failures) == 1 and not result.errors
original_tail = implementation._tail
without_prefix = lambda counter, boundary, prefix, *args: original_tail(counter, boundary, prefix[-1:], *args)
with patch.object(implementation, '_tail', side_effect=without_prefix):
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([
        TrajectoryProtoCommitmentTests('test_stop_tail_keeps_damage_already_spent_in_candidate_prefix')]))
    assert len(result.failures) == 1 and not result.errors
'@ | .\.venv\python.exe -
```

两个量尺各得到 `FAILED (failures=1)`，分别用时 0.063s／0.049s。命令本身返回 0，是因为末尾断言确认了各自确实出现一个预期失败；不能把变异测试的失败输出写成变异实现通过。

## 最终验证

组件检查沿用上面的完整命令，最终为 21／21。原有 15 项全部保留。

相关 93 项组合因新增 6 项变为 99 项，完整命令：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment tests.motion_nav.test_b09r_physics_step tests.motion_nav.test_b09r_physics_rollout tests.motion_nav.test_b10_gap_solver tests.motion_nav.test_motion_continuation_quality -q
```

实际输出：

```text
Ran 99 tests in 56.211s
OK
```

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_standalone_export -q
```

实际输出：

```text
Ran 12 tests in 28.988s
OK
```

`git diff --check` 和 `git diff --cached --check` 均返回 0。提交后的 `git status --short` 为空，提交只含上述 4 个限定文件。所有检查使用正式 Windows 项目解释器。组合与导出检查之后仅执行独立进程内存变异，未再修改实现或测试。

## 文档与证据边界

D095 和仓库根 AGENTS.md 已把旧的“P0 尚未开始”改为“P0 已开始，Task 3／4 组件完成，P0 尚未签署”。D095 同时明确首笔原型提交 `7d997913e4a6e1a406be5309b5b4b01c0681f88c` 与时间盒起点 `2026-10-10T11:52:53+08:00`。未重置时间盒，未修改历史 TP-0／D093／D094 结果。

本轮未运行完整 motion_nav、参考搜索、P0 冻结矩阵、性能量尺、闭环或 Fabric。受伤候选测试只证明预算检查有效，不授予原型新的动作或实机能力。
