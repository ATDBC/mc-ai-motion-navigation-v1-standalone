"""E117：D121 真实场景（1 格间隙—1 格平台—4 格间隙）的两条证明，用现行正式消息各有多大。

S2 的体积止损用的是 D102 的一格 gap -> gap 夹具。这里换成 D121 自己的场景，只调用项目函数：
  - 世界：scripts/d118_momentum_calibration_runtime.trial_fixture_rows 声明的朝南固定地形，
    按 ActionHandoffTests 的方式写进 WorldKnowledge 并封存成 SealedSnapshot；
  - 第一跳：D120 冻结的 26 条第一跳命令（scripts/d120_momentum_entry_calibration 的 trial.commands[:26]），
    从冻结静止起点出发；落地 post-state 必须等于 D120 的 nominal_entry；
  - 第二跳：D120 冻结的 landing-immediate 后缀（L+1 起跳，空中前进+疾跑，落地后不按键到停稳）；
  - 两条都交给项目的 prepare_fixed_motion（现行 prepared 证明器，D121-S1 的 fallback 声明），
    再用 seal_bundle 得到现行正式线上消息。第一跳声明 neutral 与 sneak（SNEAK_EDGE_STABLE）两个
    fallback，第二跳只用默认 neutral。

第一跳在生产里会是 VerifiedMotionResult，不是 prepared 证明；这里用 prepared 证明器只为得到同一
场景、同一 fallback 规则下的体积量级，不代表生产会这样准备第一跳。
报告每条证明的状态、tick 数、尾段个数与状态数、证明规范字节、正式消息字节和占 2 MiB 的比例，
以及尾段状态里有多少与主轨迹或其它尾段逐值相同（可被引用而不必复制的部分）。
用法：python e117_momentum_chain_message_size.py
"""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from mc2p.contracts.action_v1 import MovementV1  # noqa: E402
from mc2p.motion_nav.async_work import AsyncComputationScope, AsyncWorkIdentity, AsyncWorkKind  # noqa: E402
from mc2p.motion_nav.motion_chain import (  # noqa: E402
    FallbackCandidate, FallbackTerminalMode, preserve_current_neutral_fallback,
)
from mc2p.motion_nav.motion_risk import TaskDamageBudget  # noqa: E402
from mc2p.motion_nav.motion_solver import MotionCommandTick  # noqa: E402
from mc2p.motion_nav.online_motion import CandidateExecutionWindow  # noqa: E402
from mc2p.motion_nav.physics_1_21 import step  # noqa: E402
from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds, PhysicsWorldView  # noqa: E402
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET, TickInput  # noqa: E402
from mc2p.motion_nav.prepared_motion import (  # noqa: E402
    PreparedComputeStatus, PreparedMotionRequest, PreparedScanBudget, StreamGeneration,
)
from mc2p.motion_nav.prepared_motion_proof import (  # noqa: E402
    MAXIMUM_MESSAGE_BYTES, _safe_support, canonical_motion_bytes, canonical_motion_digest,
    prepare_fixed_motion, seal_bundle,
)
from mc2p.motion_nav.world_model import BlockGeometry, ObservationStamp, WorldKnowledge  # noqa: E402
from mc2p.motion_nav.world_snapshot import seal_snapshot_version  # noqa: E402
from scripts import d118_momentum_calibration_runtime as frozen  # noqa: E402
from scripts import d120_momentum_entry_calibration as calibration  # noqa: E402

RUN = (1., 0., False, False, True)
JUMP = (1., 0., True, False, True)
NEUTRAL = (0., 0., False, False, False)


def sealed_world(fixture, session):
    rows = {row["phase"]: row for row in frozen.trial_fixture_rows(fixture.trial, origin=(0, 0), feet_y=64)}
    air = tuple(tuple(p) for p in rows["clear"]["positions"])
    support = tuple(tuple(p) for p in rows["support"]["positions"])
    world = WorldKnowledge(session)
    stamp = ObservationStamp(session, 1, 1, "test", 0)
    world.confirm_air(stamp, air)
    world.observe_blocks(stamp, {p: BlockGeometry.full_cube("minecraft:grass_block") for p in support})
    cells = air + support
    bounds = PhysicsWorldBounds(min(p[0] for p in cells), max(p[0] for p in cells),
                                min(p[1] for p in cells), max(p[1] for p in cells),
                                min(p[2] for p in cells), max(p[2] for p in cells))
    version = world.borrow_snapshot_version(bounds, ruleset=JAVA_1_21_RULESET)
    snapshot = seal_snapshot_version(version, JAVA_1_21_RULESET)
    if not world.publish_sealed_snapshot(version, snapshot):
        raise AssertionError("fixture world must publish its sealed snapshot")
    return snapshot, PhysicsWorldView.from_snapshot(snapshot)


def rollout(state, inputs, world):
    states = [state]
    for tick_input in inputs:
        result = step(states[-1], tick_input, world, JAVA_1_21_RULESET)
        if result.status is not CalculationStatus.OK:
            raise AssertionError(f"rollout stopped: {result.status}")
        states.append(result.next_state)
    return states


def successor_inputs(entry, world, limit=40):
    yaw = entry.yaw_radians
    inputs, state, took_off, landed = [], entry, False, False
    for index in range(limit):
        values = JUMP if index == 0 else (NEUTRAL if landed else RUN)
        tick_input = TickInput(*values, yaw)
        result = step(state, tick_input, world, JAVA_1_21_RULESET)
        inputs.append(tick_input)
        state = result.next_state
        took_off = took_off or not state.on_ground
        landed = landed or (took_off and state.on_ground)
        if landed and math.hypot(state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2]) < .003:
            break
    return tuple(inputs)


def request_for(name, entry, inputs, exit_state, snapshot, action_index, fallbacks, window):
    stream = StreamGeneration(f"{name}-generation", f"{name}-owner", f"{name}-request", f"{name}-route",
                              action_index, inputs, (entry,), window, (f"{name}-suffix",),
                              TickInput(*RUN, entry.yaw_radians))
    commands = tuple(MotionCommandTick(MovementV1(int(i.forward), int(i.strafe), i.jump, i.sneak, i.sprint),
                                       i.movement_yaw_radians) for i in inputs)
    session = entry.session.value
    return PreparedMotionRequest(
        f"{name}-job",
        AsyncWorkIdentity(AsyncComputationScope(session, f"{name}-task", 1), f"{name}-worker-owner",
                          AsyncWorkKind.MOTION_SOLVE, stream.request_id, 1),
        stream, 1, 1, 1, commands, inputs, canonical_motion_digest(inputs), entry, exit_state,
        window, window.earliest_start_tick, f"{name}-ledger", session, (), snapshot.identity,
        f"{name}-lease", (), JAVA_1_21_RULESET, TaskDamageBudget(), 0.,
        PreparedScanBudget(65_536, 65_536, 1), stream.suffix_identity, fallbacks)


def describe(result):
    if result.status is not PreparedComputeStatus.PREPARED or result.bundle is None:
        return {"status": result.status.value, "missing_cells": len(result.missing_cells)}
    bundle = result.bundle
    wire = seal_bundle(bundle)
    proof = bundle.proof
    main = {s for branch in proof.branches for s in branch.trajectory}
    tails = [tail for branch in proof.branches for tail in branch.safety_tails]
    tail_states = [s for tail in tails for s in tail.trajectory]
    return {
        "status": "prepared",
        "branches": len(proof.branches),
        "ticks_per_branch": [len(branch.trajectory) - 1 for branch in proof.branches],
        "safety_tails": len(tails),
        "safety_tail_states": len(tail_states),
        "tail_states_equal_to_main_trajectory": sum(s in main for s in tail_states),
        "distinct_tail_states_not_in_main": len(set(tail_states) - main),
        "selected_fallbacks": sorted({tail.fallback.candidate.candidate_id for tail in tails}),
        # 引用方案里要点名的“精确出口 fallback”：出口边界那一条尾段本身有多大。
        "exit_tail_candidate": proof.branches[0].safety_tails[-1].fallback.candidate.candidate_id,
        "exit_tail_inputs": len(proof.branches[0].safety_tails[-1].inputs),
        "exit_tail_canonical_bytes": len(canonical_motion_bytes(proof.branches[0].safety_tails[-1])),
        "proof_digest_hex_chars": len(bundle.proof_digest),
        "proof_canonical_bytes": len(canonical_motion_bytes(proof)),
        "wire_payload_bytes": len(wire.payload),
        "sealed_bytes_inside_payload": len(bundle.sealed_bytes),
        "wire_payload_limit_ratio": round(len(wire.payload) / MAXIMUM_MESSAGE_BYTES, 4),
    }


def main():
    fixture = calibration.build_fixture(0)
    entry = calibration.nominal_entry(fixture)
    snapshot, world = sealed_world(fixture, entry.session)
    from scripts.d121_momentum_handoff_probe import initial_state  # 项目 S0 探针的冻结起点
    start = initial_state(fixture)
    first_inputs = tuple(TickInput(c.forward, c.strafe, c.jump, c.sneak, c.sprint, start.yaw_radians)
                         for c in fixture.trial.commands[:26])
    first_states = rollout(start, first_inputs, world)
    report = {
        "scenario": "D120 south: W1=1 P=1 W2=4",
        "message_limit_bytes": MAXIMUM_MESSAGE_BYTES,
        "first_jump_exit_equals_D120_nominal_entry": first_states[-1] == entry,
        "first_jump_exit_tick": first_states[-1].movement_tick_id,
    }
    sneak = FallbackCandidate("sneak", TickInput(0., 0., False, True, False, entry.yaw_radians),
                              maximum_ticks=30, terminal_mode=FallbackTerminalMode.SNEAK_EDGE_STABLE)
    first_window = CandidateExecutionWindow(start.movement_tick_id + 1, start.movement_tick_id + 1)
    for label, fallbacks in (("first_jump_neutral_only", (preserve_current_neutral_fallback(),)),
                             ("first_jump_neutral_then_sneak", (preserve_current_neutral_fallback(), sneak))):
        request = request_for("d121-review-first", start, first_inputs, first_states[-1], snapshot, 0,
                              fallbacks, first_window)
        report[label] = describe(prepare_fixed_motion(request, snapshot))
    second_inputs = successor_inputs(entry, world)
    second_states = rollout(entry, second_inputs, world)
    for label, window in (("second_jump_on_time_only", CandidateExecutionWindow(entry.movement_tick_id + 1,
                                                                               entry.movement_tick_id + 1)),
                          ("second_jump_on_time_and_late1", CandidateExecutionWindow(entry.movement_tick_id + 1,
                                                                                    entry.movement_tick_id + 2))):
        request = request_for("d121-review-second", entry, second_inputs, second_states[-1], snapshot, 1,
                              None, window)
        report[label] = describe(prepare_fixed_motion(request, snapshot))
    report["second_jump_ticks"] = len(second_inputs)
    # 晚一拍分支先按 hold 输入（前进+疾跑）走一拍；证明器要求这一拍仍有标准支撑。
    hold = step(entry, TickInput(*RUN, entry.yaw_radians), world, JAVA_1_21_RULESET).next_state
    report["late1_hold_state"] = {
        "entry_position": list(entry.position), "hold_position": list(hold.position),
        "hold_on_ground": hold.on_ground,
        "entry_standard_support": _safe_support(entry, world, set()),
        "hold_standard_support": _safe_support(hold, world, set()),
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
