"""E76：夹具帧数上限截断后，D114 的正式门槛会怎样分类这一场。

复用项目测试 D114B10FormalDriverTests._run（正式 Runtime、NavigationSession、
RuntimeNavigationDriver、B10 延迟 wrapper 和 CalculatorBackend），只把
max_control_frames 调小，让场景在自然终态之前被夹具的帧数上限截断。
随后按项目测试 test_component_runs_the_v3_evidence_and_formal_gates 的方式
组装 trial，调用项目的门槛函数。
"""
import sys
import unittest

from scripts import b10_gap_solver_runtime as probe
from tests.motion_nav.test_d114_b10_formal_driver import D114B10FormalDriverTests


def trial_for(case: D114B10FormalDriverTests, result, backend):
    evidence = case.last_formal_evidence
    rows = evidence["buffer"].rows("b10-coordinator-control-frames.jsonl")
    receiver = probe._receiver_diagnostic_summary(list(
        evidence["buffer"].rows("b10-receiver-diagnostics.jsonl")))
    samples = tuple(c.sample for c in evidence["captured"] if c.sample is not None)
    target = evidence["case"].goal
    return {
        "scenario_kind": evidence["scenario"].kind,
        "samples": samples,
        **probe._b10_delivery_chain_fields(samples, receiver["deliveries"]),
        "landed": backend.state.on_ground,
        "horizontal_error_blocks": ((backend.state.position[0] - target[0]) ** 2
                                    + (backend.state.position[2] - target[2]) ** 2) ** .5,
        "level_error_blocks": abs(backend.state.position[1] - target[1]),
        "horizontal_collisions": sum(int(r["horizontal_collision"]) for r in rows),
        "damage_points": backend.damage_taken,
        "waiting_polls": result.waiting_frame_count,
        **probe._b10_formal_trial_fields(
            result, samples=samples, recorded_control_frames=len(rows),
            start_position=evidence["case"].start),
    }


def main() -> int:
    limits = [int(value) for value in sys.argv[1:]] or [160, 12, 6]
    print("delay | 帧数上限 | 实际帧数 | 终态 | 原因 | bounded | typed_safe | completed | never_return_safe")
    for delay in (10, None):
        for limit in limits:
            case = D114B10FormalDriverTests("test_component_runs_the_v3_evidence_and_formal_gates")
            try:
                try:
                    result, runtime, backend = case._run(
                        turns=0, delay=delay, max_control_frames=limit, capture_evidence=True)
                except RuntimeError as error:
                    print(f"{delay} | {limit} | - | 抛出 RuntimeError | {error} | - | - | - | -")
                    continue
                trial = trial_for(case, result, backend)
                print(f"{delay} | {limit} | {result.adopted_frame_count} | {result.terminal_state} | "
                      f"{result.terminal_reason} | {result.bounded_terminal} | "
                      f"{probe._b10_coordinator_typed_safe_terminal(trial)} | "
                      f"{probe._b10_coordinator_completed(trial)} | "
                      f"{probe._b10_coordinator_never_return_safe({**trial, 'scenario_kind': 'worker_never_return'}) if delay is None else '-'}")
            finally:
                case.doCleanups()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
