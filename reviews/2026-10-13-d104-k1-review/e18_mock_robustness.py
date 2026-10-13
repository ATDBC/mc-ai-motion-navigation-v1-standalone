"""E18：用 K1 审查同类的 `_tail` 篡改，检验 E17 的 A 模式（只跳过 `_risks` 不看的边界）。

对 28 个样本，每个样本 40 次：随机挑 1—4 个边界，把 `_tail` 结果强行翻转（SAFE↔UNSAFE），
旧 scan_commitment 与 A 模式在同一篡改下运行，比较接纳／拒绝和风险区间。
A 模式若只是 `_risks` 的等价变换，任何篡改下两者都应完全一致。
运行：PYTHONPATH=<项目根> python e18_mock_robustness.py（需与 e17 同目录）
"""
import random, sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from experiments.motion_navigation.trajectory_proto import commitment as C
from experiments.motion_navigation.trajectory_proto.measurement import _set_a, _set_c, _set_d
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence
import e17_sound_sparse_scan as E  # noqa: E402  (only helpers; experiments run under __main__)

rng = random.Random(104)
cases = list(_set_a(4)) + list(_set_c(6)) + list(_set_d(18))
original = C._tail
same = differ = 0
outcomes = {"both_accept": 0, "both_reject": 0}
for set_id, key, request, world, inputs, *_ in cases:
    evidence = _boundary_evidence(request, inputs)
    for _ in range(40):
        flips = set(rng.sample(range(len(inputs) + 1), rng.randint(1, 4)))

        def flipped(*args, **kwargs):
            tail = original(*args, **kwargs)
            if args[1] in flips:
                status = (C.TailStatus.UNSAFE if tail.status is C.TailStatus.SAFE_STOP else C.TailStatus.SAFE_STOP)
                return replace(tail, status=status)
            return tail

        with patch.object(C, "_tail", side_effect=flipped):
            old = C.scan_commitment(request, inputs, world, evidence)
            try:
                mine = E.sparse_scan(request, inputs, world, evidence, use_certificate=False)[0]
            except C._Incomplete:
                mine = None
        reference = E.old_intervals(old) if old.status is C.ScanStatus.VERIFIED_CANDIDATE else None
        if reference == mine:
            same += 1
            outcomes["both_accept" if mine is not None else "both_reject"] += 1
        else:
            differ += 1
            print("DIFF", key, sorted(flips), reference, mine)
print(f"mutated runs: {same + differ}; identical accept/reject and intervals: {same}; different: {differ}; {outcomes}")
