# D119 0 高度 step 候选修复计划

日期：2026-10-19。对应决定见 [D119](../decisions/0119-repair-zero-height-step-candidate.md)。

状态：**COMPLETED。** 本阶段已修复 D118-PHYSICS-01；D118-0 不恢复，D118-1—D118-5 仍未授权。

## 1. 目标

让项目的 1.21 运动计算器按原版规则处理 0 高度 step 候选，并证明这项修复没有改变无关运动结果。

## 2. 修改范围

实现阶段只应改动：

- `mc2p/motion_nav/physics_1_21.py`；
- `tests/motion_nav/test_b09r_physics_step.py`；
- 必要时增加只读 D118 重放断言，但不能改 D118 输入和 fixture；
- D119 验收、缺陷台账和入口文档。

保持不变：

- `scripts/d118_momentum_calibration_runtime.py` 的固定输入与场景；
- D118 原始 evidence 和 stop 结论；
- B09-R 的 `0.001` 格位置、`0.0001` 格／tick 速度门槛；
- 求解器、规划器、Session、prepared 链和 Runtime；
- 冻结离线表的内容。

## 3. 实施步骤

### S0：固定 RED

在 `B09RPhysicsStepTests` 增加 `test_d118_tick_246_zero_height_step_preserves_ground_contact`。测试使用封存的真实预状态、后退输入和 `z=28／30` 支撑、`z=29` 空气的世界。

RED 必须证明旧代码错误地得到：

- `position.y=-0.0784000015258789`；
- `position.z=29.3`；
- `on_ground=false`；
- `horizontal_collision=true`；
- `vertical_collision=false`；
- 事件包含 `left_ground`。

目标断言固定为 Fabric 结果：

- 位置约为 `(14.5, 0.0, 29.29013755153609)`；
- 速度约为 `(0.0, -0.0784000015258789, -0.1175942998516743)`；
- `on_ground=true`；
- `horizontal_collision=false`；
- `vertical_collision=true`；
- contact event 只有 `vertical_collision`；
- 不包含 `step_up`。

运行：

```powershell
.\.venv\python.exe -m unittest `
  tests.motion_nav.test_b09r_physics_step.B09RPhysicsStepTests.test_d118_tick_246_zero_height_step_preserves_ground_contact -v
```

预期：修改生产代码前失败，且失败字段正是上述碰撞分支。

### S1：最小实现

在 `_resolve_with_step()` 中对齐原版 `collectStepHeights`：

- 保留范围内的 0 高度；
- 排除与基础解 Y displacement 相同的高度；
- 继续拒绝负高度和超过 `step_height` 的高度；
- 区分“采用 step 求解候选”和“实际向上 step”，0 高度候选不发布 `step_up`。

不得改变 `_resolve()` 的 Y／水平轴顺序，也不得为 D118 增加特判。

完成后先运行 S0 单测。预期通过。

### S2：只读重放 D118 raw

使用现有 raw run 重新生成分析结果，输出写入 `.tmp`，不得覆盖 D118 原 evidence：

```powershell
.\.venv\python.exe scripts\d118_momentum_calibration.py `
  --run-dir artifacts\fabric-deployment\20261018T145935753542Z-be4892d2 `
  --output .tmp\d119-d118-read-only-replay
```

只签署以下结论：

- `fallback-back-south-0` 的 tick 246 不再有位置、速度或离散状态分叉；
- 273 个逐 tick 独立重放保持完整；
- D118 原始输入、receipt、physics 和 F-TICK 文件哈希不变。

D118 的入口包络仍为 `unverified`。因此该命令的顶层结果不用于签署 D118-0，也不触发新的 Fabric 运行。

### S3：受影响回归

按下列顺序运行：

```powershell
.\.venv\python.exe -m unittest `
  tests.motion_nav.test_b09r_physics_step `
  tests.motion_nav.test_b09r_physics_rollout `
  tests.motion_nav.test_ground_traversal -q

.\.venv\python.exe -m unittest `
  tests.motion_nav.test_b10_gap_solver `
  tests.motion_nav.test_b10_motion_candidate `
  tests.motion_nav.test_action_continuity_solver_limits `
  tests.motion_nav.test_air_transition_solver -q

.\.venv\python.exe -m unittest `
  tests.motion_nav.test_d118_momentum_calibration -q
```

同时核对冻结离线表、候选输入哈希和旧成功索引。允许的变化只有原来经过 0 高度边界的预测分支。无法解释的变化触发止损，不能直接重建表。

### S4：完整检查和公开导出

受影响集合通过后，运行现行 Windows 完整正序、逆序分片。两遍都必须零失败、错误、超时和跳过。

随后生成独立快照：

```powershell
.\.venv\python.exe scripts\export_motion_navigation_standalone.py export `
  --root .tmp\d119-public --clean
.\.venv\python.exe scripts\export_motion_navigation_standalone.py verify `
  --root .tmp\d119-public
.\.venv\python.exe -m unittest `
  tests.motion_nav.test_public_repository_completeness `
  tests.motion_nav.test_standalone_export -q
```

公开快照也要运行同一组直接物理和 D118 检查。

## 4. 完成条件

D119 只有同时满足以下条件才能签署：

- tick 246 RED 先红后绿；
- 0 高度候选不产生 `step_up`；
- 现有 D118 raw 只读重放关闭原单帧差异；
- B09-R、ground traversal、motion solver／候选和 D118 检查全绿；
- 旧成功退步为 0；
- 冻结离线表变化为 0，或每一项都能由本规则直接解释并另行决定；
- Windows 完整正逆和独立公开快照全绿；
- 没有修改 D118 输入、容差或历史证据。

签署 D119 只关闭 D118-PHYSICS-01。D118-0 保持历史 FAILED／STOPPED；后续校准必须另行授权。

## 5. 实施结果

- tick 246 的精确用例先在旧实现上复现错误下沉，修复后与 Fabric 的位置、速度、着地和碰撞状态一致；
- Java `float` 相等、极小正高度、向下碰撞后的原始 `baseY` 和 0 高度不发布 `step_up` 均有独立检查；
- D118 raw 只读重放覆盖 7 个 trial、273 个 tick，离散差异为 0；
- B09-R、ground traversal、motion solver／候选、D118、冻结表和轨迹实验检查通过；
- 源码树正序、逆序各 196／196 个模块、2507 项通过；
- 独立公开快照正序、逆序各 195／195 个模块、2483 项通过；
- 四组完整门禁均为零失败、错误、超时和跳过；
- 公开快照包含 2598 个文件并通过封闭哈希验证；
- 未运行新的 Fabric，也没有修改 D118 原始输入、容差或历史证据。

