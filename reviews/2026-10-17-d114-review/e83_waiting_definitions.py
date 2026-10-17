"""E83：同一组件场景里，两种“等待帧”口径的数值。

复用项目测试 D114B10FormalDriverTests._run（capture_evidence=True）。
  - 组件测试组装 trial 时用的 result.waiting_frame_count：本帧提议里没有移动意图；
  - Fabric 外层的 waiting_polls：本帧 submit_input=false。
"""
from tests.motion_nav.test_d114_b10_formal_driver import D114B10FormalDriverTests


def main() -> int:
    print("delay | 控制帧 | waiting_frame_count（组件口径） | submit_input=false 帧（Fabric 口径）")
    for delay in (0, 5, 10, None):
        case = D114B10FormalDriverTests("test_component_runs_the_v3_evidence_and_formal_gates")
        try:
            result, _runtime, _backend = case._run(turns=0, delay=delay, capture_evidence=True)
            rows = case.last_formal_evidence["buffer"].rows("b10-coordinator-control-frames.jsonl")
            print(f"{delay} | {len(rows)} | {result.waiting_frame_count} | "
                  f"{sum(not row['submit_input'] for row in rows)}")
        finally:
            case.doCleanups()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
