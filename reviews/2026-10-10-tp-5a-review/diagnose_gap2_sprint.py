"""TP Task 5A 审查探针：2 格跨隙加疾跑时，参考搜索的预算花在哪里。

运行：在仓库根目录 PYTHONPATH=.:<本目录> python <本文件>
只读运行。统计每次承诺扫描的结果，以及被扫描候选的前三条命令。
"""
from dataclasses import replace
import collections

from experiments.motion_navigation.trajectory_proto import reference_search as rs
from experiments.motion_navigation.trajectory_proto.scenarios import P0_BUDGET

import scale_probe as sp

NAMES = {sp.WALK: "W", sp.WALK_JUMP: "WJ", sp.NEUTRAL: "N", sp.SPRINT: "S", sp.SPRINT_JUMP: "SJ"}

if __name__ == "__main__":
    original_scan = rs.scan_commitment
    results, prefixes = collections.Counter(), collections.Counter()

    def counted_scan(request, inputs, *args, **kwargs):
        scan = original_scan(request, inputs, *args, **kwargs)
        results[(scan.status.value, str(scan.reason))] += 1
        prefixes[" ".join(NAMES[command] for command in inputs[:3])] += 1
        return scan

    rs.scan_commitment = counted_scan
    request, world, goal = sp.gap_case(2)
    request = replace(request, goal=goal, supported_inputs=sp.ALPHABETS["A5 +sprint/sprint_jump"],
                      budget=P0_BUDGET)
    outcome = rs.reference_search(request, world)
    print("outcome", outcome.status.value, outcome.reason.value, outcome.counts,
          "expanded", outcome.expanded_nodes)
    print("scan results", results.most_common())
    print("first three commands of scanned candidates", prefixes.most_common())
