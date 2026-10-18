"""E85：同一 D116 代表正式链重复运行，统计结果分布。

每次单独起一个解释器调用 scripts/d116_handoff_timeline.py 的
run_representative_timeline()（可选把 max_ticks 从 25 放长），输出：
第二跳 prepared 证明的登记 tick、tick 20 的 entry 状态与批准结果、
tick 21 是否应用第二跳、出现 jump 的 tick、终态、监视器违规代码、tick 21 之后出现过的会话原因、
第二跳落地 tick 和 tick 21 之后的有效输入帧数。
在哪棵树里运行，就统计哪棵树。
用法：python e85_timeline_repeat.py <次数> [max_ticks]
"""
import collections
import json
import subprocess
import sys

CHILD = r'''
import json, sys
from dataclasses import replace
from scripts import d116_handoff_timeline as t
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
max_ticks = int(sys.argv[1])
orig = t._representative
if max_ticks != 25:
    t._representative = lambda: replace(orig(), max_ticks=max_ticks)
registered = []
od = MotionRouteCoordinator.decide
def dec(self, frame, anchor, ledger, world, **kw):
    out = od(self, frame, anchor, ledger, world, **kw)
    if not registered and len(self.result_inbox.registered_handles) and self.executor.action_index == 1:
        registered.append(anchor.movement_tick_id)
    return out
MotionRouteCoordinator.decide = dec
r = t.run_representative_timeline()
rows = {row["movement_tick"]: row for row in r["timeline"]}
jumps = [k for k, row in sorted(rows.items()) if (row.get("applied_movement_at_observation") or {}).get("jump")]
after = [rows[k].get("session_reason_after_effect") for k in sorted(rows) if k >= 21]
reasons_after_21 = sorted({reason for reason in after if reason is not None})
second_landing = next((k for k in sorted(rows) if k > 21 and rows[k].get("landing_observed")
                       and rows.get(k - 1, {}).get("landing_observed") is False), None)
inputs_after_21 = [k for k in sorted(rows) if k > 21
                   and any((rows[k].get("applied_movement_at_observation") or {}).get(key)
                           for key in ("forward", "jump", "sprint"))]
print(json.dumps({
    "registered_tick": registered[0] if registered else None,
    "tick20": (rows.get(20, {}).get("prepared_entry_status"), rows.get(20, {}).get("supervisor_status"),
               rows.get(20, {}).get("supervisor_reason")),
    "tick21_jump": r["milestones"]["tick21_second_jump_applied"],
    "jumps": jumps,
    "outcome": [r["outcome"], r["reason"]],
    "violations": sorted({code for _tick, code, _detail in r["violations"]}),
    "final_z": round(r["final_position"][2], 3),
    "reasons_after_21": reasons_after_21,
    "inputs_after_21": len(inputs_after_21),
    "second_landing_tick": second_landing,
}, sort_keys=True))
'''


def main() -> int:
    repeats = int(sys.argv[1])
    max_ticks = sys.argv[2] if len(sys.argv) > 2 else "25"
    counter = collections.Counter()
    examples = {}
    for _ in range(repeats):
        done = subprocess.run([sys.executable, "-c", CHILD, max_ticks], capture_output=True, text=True, timeout=900)
        line = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else json.dumps({"error": done.stderr[-300:]})
        row = json.loads(line)
        key = json.dumps({k: row[k] for k in row if k not in ("registered_tick", "final_z", "second_landing_tick",
                                                              "inputs_after_21")}, sort_keys=True,
                         ensure_ascii=False)
        counter[key] += 1
        examples.setdefault(key, []).append((row.get("registered_tick"), row.get("final_z"),
                                             row.get("second_landing_tick"), row.get("inputs_after_21")))
    print(f"max_ticks={max_ticks}，{repeats} 次")
    for key, count in counter.most_common():
        print(f"{count} 次：{key}")
        print(f"    （第二跳登记 tick, 终点 z, 第二跳落地 tick, tick 21 之后的有效输入帧数）→ 次数："
              f"{sorted(collections.Counter(examples[key]).items(), key=str)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
