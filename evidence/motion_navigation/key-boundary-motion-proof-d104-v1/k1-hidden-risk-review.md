# D104 K1 隐藏短风险复现

日期：2026-10-13。用途：独立审查记录。本文不修改生产代码或测试。

## 结论

当前关键边界算法只在以下位置尝试停车尾迹：

- 输入发生变化的边界附近；
- 已经采样、不可撤回的输入发生变化的边界附近；
- `support_safe` 布尔值发生变化的边界附近；
- 入口和终点。

只有某个已采样边界的尾迹为 `UNSAFE` 时，算法才继续向前、向后逐边界搜索风险区间。因此，两个 seed 之间若短暂出现 `UNSAFE`，随后又恢复为 `SAFE`，当前算法没有证据能发现它。

下面的复现把正式 `_tail` 结果只在第 5 个边界改成 `UNSAFE`。主轨迹、世界、支撑和其他尾迹都保持正式实现的结果。旧扫描器检查每个边界，因此识别出风险区间；紧凑扫描器没有检查第 5 个边界，因此返回零风险。

这个复现不是在证明现有某个 Minecraft 场景必然出现该风险。它证明了更基础的问题：当前算法依赖“风险变化必然靠近 seed”的假设，但实现和契约中没有证明这条假设。

## 运行命令

在仓库根目录执行：

```powershell
@'
from dataclasses import replace
from unittest.mock import patch

from experiments.motion_navigation.trajectory_proto import commitment as full
from experiments.motion_navigation.trajectory_proto import key_boundary_proof as compact
from experiments.motion_navigation.trajectory_proto import m0_probe
from experiments.motion_navigation.trajectory_proto.reference_search import (
    _boundary_evidence,
)


fixture = m0_probe._fixture_for("flat_walk", "A3")
request = fixture.request
inputs = (request.stop_input,) * 10
evidence = _boundary_evidence(request, inputs)
original_tail = full._tail


def one_boundary_is_unsafe(*args, **kwargs):
    tail = original_tail(*args, **kwargs)
    boundary = args[1]
    if boundary == 5:
        return replace(tail, status=full.TailStatus.UNSAFE)
    return tail


with (
    patch.object(full, "_tail", side_effect=one_boundary_is_unsafe),
    patch.object(compact, "_tail", side_effect=one_boundary_is_unsafe),
):
    old_result = full.scan_commitment(
        request, inputs, fixture.world, evidence,
    )
    compact_result = compact.compact_scan_commitment(
        request, inputs, fixture.world, evidence,
    )

print("old status:", old_result.status)
print(
    "old risks:",
    [branch.risk_intervals for branch in old_result.proof.branches],
)
print("compact status:", compact_result.status)
print(
    "compact risks:",
    [branch.risk_intervals for branch in compact_result.proof.branches],
)
print(
    "compact retained tails:",
    [
        [tail.boundary for tail in branch.safety_tails]
        for branch in compact_result.proof.branches
    ],
)
'@ | .venv\python.exe -
```

## 实际输出

```text
old status: VERIFIED_CANDIDATE
old risks: [(RiskInterval(last_abandon_boundary=4, first_committed_boundary=5, recovered_boundary=6, last_abandon_tick=14, first_committed_tick=15, commitment_effect_tick=16, recovered_tick=16),), (RiskInterval(last_abandon_boundary=4, first_committed_boundary=5, recovered_boundary=6, last_abandon_tick=15, first_committed_tick=16, commitment_effect_tick=17, recovered_tick=17),)]
compact status: VERIFIED_CANDIDATE
compact risks: [(), ()]
compact retained tails: [[0, 10], [0, 10]]
```

## 为什么现有测试没有发现

现有 18 个 D 样本只证明这些固定样本的新旧结果一致。它们没有制造 seed 之间的短风险，也没有对“未采样边界保持安全”做变异检查。

`test_missing_or_mutated_key_tail_is_rejected` 检查的是已经保存的尾迹被删除或改写后，摘要和结构校验能否发现。它没有检查算法是否一开始就漏掉了一个应该保存的风险区间。

## 修正方向

在把 K1 写成“关键边界证明已验证”之前，需要满足下面一种条件：

1. 仍然逐边界判定停车安全，只压缩最终保存的尾迹；或
2. 给出可执行检查，证明两个 seed 之间停车安全具有所需的单调性；或
3. 使用保守包络覆盖 seed 之间全部边界，并用变异测试证明短风险一定会使门禁失败。

只扩大冻结样本数量不能消除这个结构缺口。
