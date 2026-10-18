"""E97：tick 21 的路线复核与 prepared 状态比较，逐字段读出。

复用项目脚本 scripts/d116_handoff_timeline.py 的 run_representative_timeline()（进程内恢复
prepare_upcoming_action()，max_ticks=25）。第二跳准备偶尔因 (d) 不登记，所以最多跑 8 次，
取第一次 tick 20 为 READY_MATCH 的运行，打印 tick 21 的：
  - 激活后的路线复核（incumbent_validation、原因、是否因此取消）；
  - prepared 控制器的预测与实际状态是否相等、预测速度、实际速度、控制器原因与状态；
  - 监视器违规。
E84 表里的“路线复核”列取的是 route_validation.incumbent（激活前对旧动作的复核），
不是这里的 incumbent_validation。在哪棵树里运行，就读哪棵树。
"""
from scripts.d116_handoff_timeline import run_representative_timeline

FIELDS = ("incumbent_validation", "incumbent_validation_reason", "route_cancel_after_validation",
          "prepared_expected_state_matches", "prepared_controller_reason",
          "prepared_controller_state_after", "prepared_expected_velocity", "prepared_actual_velocity")


def main() -> int:
    for attempt in range(8):
        result = run_representative_timeline()
        rows = {row["movement_tick"]: row for row in result["timeline"]}
        if rows.get(20, {}).get("prepared_entry_status") == "ready_match":
            break
    else:
        print("8 次都没有在 tick 20 得到 READY_MATCH")
        return 1
    row = rows[21]
    print(f"第 {attempt + 1} 次运行 tick 20 为 READY_MATCH；监视器违规:",
          [(tick, code) for tick, code, _ in result["violations"]])
    for key in FIELDS:
        print(f"  {key}: {row.get(key)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
