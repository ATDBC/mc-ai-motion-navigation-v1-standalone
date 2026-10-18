"""E102：D116／D117 代表链在交接前后实际带了多少水平速度。

调用项目脚本 scripts/d116_handoff_timeline.py 的 run_representative_timeline(max_ticks=80)
（进程内恢复 prepare_upcoming_action()，其余是正式链），逐 tick 打印 tick 6–24 的观察位置、
水平速度（b/s）、是否已观察到着地、该 tick 实际生效的输入（f=前进 j=跳 s=疾跑 n=潜行）和会话原因。
用来对照 E98／E100：带速度接续需要落地时约 4.6–5.2 b/s。
"""
import math

from scripts.d116_handoff_timeline import run_representative_timeline


def main() -> int:
    result = run_representative_timeline(max_ticks=80)
    print(f"终态={result['outcome']} 原因={result['reason']} 违规={result['violations']}")
    print("tick | z | 水平速度 b/s | 已着地 | 实际输入 | 会话原因")
    for row in result["timeline"]:
        tick = row["movement_tick"]
        if not 6 <= tick <= 24:
            continue
        velocity = row.get("observed_velocity_blocks_per_second")
        moved = row.get("applied_movement_at_observation") or {}
        keys = "".join(k[0] for k in ("forward", "jump", "sprint") if moved.get(k))
        keys += "n" if moved.get("sneak") else ""
        speed = round(math.hypot(velocity[0], velocity[2]), 3) if velocity else None
        print(f"{tick} | {round(row['observed_position'][2], 3)} | {speed} | {row.get('landing_observed')} | "
              f"{keys or '-'} | {row.get('session_route_reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
