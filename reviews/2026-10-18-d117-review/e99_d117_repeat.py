"""E99：在 Linux 上复核 D117 的端到端声明。

每次单独起一个解释器，调用项目脚本 scripts/d116_handoff_timeline.py 的
run_representative_timeline(max_ticks=80, snapshot_borrow_mode=<模式>)。该函数在进程内恢复
prepare_upcoming_action()（现行生产不调用），其余是正式链；快照模式是 D117 自带的 typed 注入：
  normal               不注入；
  busy_1/busy_3/busy_5 前 1/3/5 次借快照返回 BUSY；
  always_busy          每次都返回 BUSY；
  prepared_always_busy 第一次以后都返回 BUSY。
统计：终态与原因、完成 tick、出现 jump 输入的 tick、第二跳是否由 prepared 链应用、
prepared 生命周期事件、监视器违规。在哪棵树里运行，就统计哪棵树。
用法：python e99_d117_repeat.py <模式> <次数>
"""
import collections
import json
import subprocess
import sys

CHILD = r'''
import json, sys
from scripts.d116_handoff_timeline import run_representative_timeline
r = run_representative_timeline(max_ticks=80, snapshot_borrow_mode=sys.argv[1])
rows = r["timeline"]
jumps = [row["movement_tick"] for row in rows
         if (row.get("applied_movement_at_observation") or {}).get("jump")]
life = r.get("prepared_apply_lifecycle") or {}
print(json.dumps({
    "outcome": [r["outcome"], r["reason"]],
    "finish_tick": rows[-1]["movement_tick"] if rows else None,
    "jumps": jumps,
    "prepared_second_jump_applied": r["milestones"].get("second_jump_applied"),
    "lifecycle": life.get("events") if isinstance(life, dict) else life,
    "violations": sorted({code for _tick, code, _detail in r["violations"]}),
    "damage": r["damage"],
}, sort_keys=True, default=str))
'''


def main():
    mode, repeats = sys.argv[1], int(sys.argv[2])
    counts = collections.Counter()
    for _ in range(repeats):
        done = subprocess.run([sys.executable, "-c", CHILD, mode], capture_output=True, text=True, timeout=600)
        lines = done.stdout.strip().splitlines()
        counts[lines[-1] if done.returncode == 0 and lines else "ERROR " + done.stderr.strip()[-300:]] += 1
    print(f"mode={mode}，{repeats} 次")
    for line, count in counts.most_common():
        print(f"{count} 次：{line}")


if __name__ == "__main__":
    main()
