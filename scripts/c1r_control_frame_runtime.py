"""R1 real-Fabric proof that movement and one melee attack share a control frame."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import time
from typing import Callable, Iterable

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import FieldStatusV0
from mc2p.contracts.intent_source import (
    ControlFrameProposalV1, OrderedIntentV1, ordered_intent_id,
)
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.task import ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1, RuntimeStateV1
from mc2p.skills.fixed_melee import CombatTargetV1
from mc2p.skills.melee_strike_driver import MeleeStrikeDriver
from scripts.control_probe_core import append_jsonl, write_json_atomic


PLAYER_START = {"x": 0.5, "y": 100.0, "z": -1.75}
TARGET = {"x": 0.5, "y": 100.0, "z": 0.5}
_MOVEMENTS = {
    "forward": {"forward": 1, "strafe": 0},
    "backward": {"forward": -1, "strafe": 0},
    "left": {"forward": 0, "strafe": 1},
    "right": {"forward": 0, "strafe": -1},
}


def c1r_control_frame_trial_plan(world_seed: int) -> tuple[dict, ...]:
    if type(world_seed) is not int:
        raise ValueError("R1 world seed must be an integer")
    rows = []
    for direction, movement in _MOVEMENTS.items():
        for repeat in range(1, 11):
            rows.append({
                "trial_id": f"positive-{direction}-{repeat:02d}",
                "classification": "positive",
                "direction": direction,
                "repeat": repeat,
                "scenario_seed": world_seed * 100 + len(rows) + 1,
                "start_position": dict(PLAYER_START),
                "target_position": dict(TARGET),
                "yaw_degrees": 0.0,
                "pitch_degrees": 0.0,
                "movement": dict(movement),
            })
    return tuple(rows)


def _matches_movement(event: dict, expected: dict) -> bool:
    actual = event.get("actual_input")
    return bool(
        event.get("event") == "input_consumed"
        and event.get("input_state") == "leased"
        and type(actual) is dict
        and float(actual.get("forward", 9.0)) == expected["forward"]
        and float(actual.get("strafe", 9.0)) == expected["strafe"]
        and actual.get("jump") is False
        and actual.get("sneak") is False
        and actual.get("sprint") is False
    )


def evaluate_c1r_control_frame_evidence(
    trials: Iterable[dict], control_events: Iterable[dict],
) -> tuple[list[dict], list[dict]]:
    """Join each real attack to movement consumed by the same client tick/request."""
    trials, events = tuple(trials), tuple(control_events)
    if any(type(row) is not dict for row in (*trials, *events)):
        raise ValueError("R1 control-frame evidence rows must be objects")
    attacks = tuple(row for row in events if row.get("event") == "attack_dispatched")
    inputs = tuple(row for row in events if row.get("event") == "input_consumed")
    evaluated = []
    for trial in trials:
        track_id = trial.get("target_track_id")
        target_attacks = tuple(
            row for row in attacks
            if row.get("actual_operation", {}).get("entity_ref") == track_id
        )
        attack = target_attacks[0] if len(target_attacks) == 1 else None
        matching_inputs = () if attack is None else tuple(
            row for row in inputs
            if row.get("episode_id") == attack.get("episode_id")
            and row.get("request_sequence_id") == attack.get("request_sequence_id")
            and row.get("client_ticks") == attack.get("client_ticks")
            and _matches_movement(row, trial["movement"])
        )
        report = trial.get("report", {})
        passed = bool(
            len(target_attacks) == 1
            and len(matching_inputs) == 1
            and report.get("state") == "complete"
            and report.get("reason") == "hit_confirmed"
            and report.get("attack_submissions") == 1
            and report.get("hit_observed") is True
            and trial.get("runtime_ready") is True
        )
        evaluated.append({
            **trial,
            "attack_event_count": len(target_attacks),
            "same_tick_movement_event_count": len(matching_inputs),
            "attack_request_sequence_id": (
                None if attack is None else attack.get("request_sequence_id")
            ),
            "attack_client_tick": None if attack is None else attack.get("client_ticks"),
            "passed": passed,
        })
    expected_tracks = {row.get("target_track_id") for row in trials}
    foreign_attacks = tuple(
        row for row in attacks
        if row.get("actual_operation", {}).get("entity_ref") not in expected_tracks
    )
    checks = [
        {"name": "r1_every_trial_has_same_tick_movement_and_attack",
         "passed": bool(evaluated) and all(row["passed"] for row in evaluated)},
        {"name": "r1_no_wrong_entity_attack", "passed": not foreign_attacks},
        {"name": "r1_no_repeated_attack_dispatch",
         "passed": len(attacks) == len(trials)
         and all(row["attack_event_count"] == 1 for row in evaluated)},
        {"name": "r1_no_input_ownership_violation",
         "passed": bool(evaluated) and all(row.get("runtime_ready") is True
                                           for row in evaluated)},
    ]
    return evaluated, checks


def _fixture_commands(trial: dict) -> tuple[str, ...]:
    start, target = trial["start_position"], trial["target_position"]
    return (
        "difficulty normal",
        "time set midnight",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "weather clear",
        "kill @e[type=!minecraft:player]",
        "kill @e[type=minecraft:item]",
        "fill -10 99 -10 10 99 10 minecraft:grass_block replace",
        "fill -10 100 -10 10 103 10 minecraft:air replace",
        "item replace entity MC2PProbe weapon.mainhand with minecraft:stone_sword",
        (f"tp MC2PProbe {start['x']} {start['y']} {start['z']} "
         f"{trial['yaw_degrees']} {trial['pitch_degrees']}"),
        (f"summon minecraft:zombie {target['x']} {target['y']} {target['z']} "
         "{NoAI:1b,PersistenceRequired:1b,Silent:1b,Tags:[\"mc2p_c1r\"]}"),
    )


def _task(trial_id: str, deadline_ns: int) -> TaskIntentV0:
    return TaskIntentV0(
        "c1r-" + trial_id,
        "shared_control_frame_melee",
        "{}",
        (SuccessCriterionV0(
            "melee_hit_confirmed", ComparisonOperatorV0.EQUAL, 1, "boolean",
        ),),
        100,
        deadline_ns,
        True,
        0.0,
    )


def _visible_zombies(runtime: PlayerRuntimeV1):
    observation = runtime.observation
    if (observation is None
            or observation.perception.status is not FieldStatusV0.VALID
            or observation.perception.value is None):
        return ()
    return tuple(entity for entity in observation.perception.value.visible_entities
                 if entity.entity_type == "minecraft:zombie")


def _trial_attack_ready(own, zombies):
    if (own is None or not own.is_on_ground or own.attack_cooldown < 1.0
            or len(zombies) != 1):
        return None
    return zombies[0]


def _refresh_target(runtime: PlayerRuntimeV1, trial: dict, deadline_ns: int):
    profile = BehaviorProfileV0()
    task = _task(trial["trial_id"] + "-observe", deadline_ns)
    for _ in range(60):
        now = time.perf_counter_ns()
        result = runtime.control_frame(
            task,
            profile,
            min(deadline_ns, now + 500_000_000),
            proposals=(ControlFrameProposalV1(
                observation_request=ObservationRequestV3("navigation_v1"),
            ),),
        )
        if result.report.failure is not None:
            raise RuntimeError("R1 target observation failed: " + result.report.failure.reason)
        own = runtime.observation.self_state.value
        zombies = _visible_zombies(runtime)
        candidate = _trial_attack_ready(own, zombies)
        if candidate is not None:
            checked = runtime.control_frame(
                task,
                profile,
                min(deadline_ns, time.perf_counter_ns() + 500_000_000),
                proposals=(ControlFrameProposalV1(
                    observation_request=ObservationRequestV3(
                        "interaction_v1", entity_track_id=candidate.track_id,
                    ),
                ),),
            )
            tracked = runtime.observation.tracked_entity.value
            refreshed = _visible_zombies(runtime)
            if (checked.report.failure is None and tracked is not None
                    and tracked.track_id == candidate.track_id
                    and not tracked.is_dead
                    and any(entity.track_id == candidate.track_id for entity in refreshed)):
                return next(entity for entity in refreshed
                            if entity.track_id == candidate.track_id)
        time.sleep(0.01)
    raise RuntimeError("R1 fixture did not yield one grounded visible zombie")


def run_c1r_control_frame_runtime(
    runtime: PlayerRuntimeV1,
    episode: str,
    directory: Path,
    deadline_ns: int,
    *,
    world_seed: int,
    code_hashes: dict[str, str],
    fixture_writer: Callable[[tuple[str, ...], dict], None],
) -> tuple[dict, list[dict], list[dict]]:
    if type(runtime) is not PlayerRuntimeV1 or runtime.state is not RuntimeStateV1.READY:
        raise ValueError("R1 control-frame probe requires ready PlayerRuntimeV1")
    if type(code_hashes) is not dict:
        raise ValueError("R1 control-frame source hashes must be a mapping")
    directory = Path(directory)
    plan = c1r_control_frame_trial_plan(world_seed)
    write_json_atomic(directory / "c1r-control-frame-manifest.json", {
        "schema_version": "mc2p.c1r-control-frame-manifest.v1",
        "world_seed": world_seed,
        "code_hashes": dict(sorted(code_hashes.items())),
        "trials": list(plan),
    })
    profile = BehaviorProfileV0()
    rows = []
    for trial in plan:
        fixture_writer(_fixture_commands(trial), trial)
        entity = _refresh_target(runtime, trial, deadline_ns)
        movement_source = runtime.register_ordered_source(
            "c1r-movement-" + trial["direction"],
        )
        driver = None
        try:
            now = time.perf_counter_ns()
            intent_id = ordered_intent_id(movement_source, 1)
            movement = MovementV1(**trial["movement"])
            envelope = OrderedIntentV1(
                movement_source,
                1,
                ActionIntentV1(
                    intent_id,
                    movement_source.source_id,
                    episode,
                    runtime.observation.sequence_id,
                    ActionPriorityV0.TASK,
                    now,
                    min(deadline_ns, now + 2_000_000_000),
                    movement=movement,
                    valid_for_ticks=20,
                ),
            )
            runtime.control_frame(
                _task(trial["trial_id"] + "-movement", deadline_ns),
                profile,
                min(deadline_ns, now + 500_000_000),
                proposals=(ControlFrameProposalV1(
                    (envelope,),
                    ObservationRequestV3(
                        "interaction_v1", entity_track_id=entity.track_id,
                    ),
                ),),
            )
            target = CombatTargetV1(
                "c1r-task-" + trial["trial_id"],
                "c1r-goal-" + trial["trial_id"],
                1,
                episode,
                entity.track_id,
            )
            driver = MeleeStrikeDriver(runtime, task_deadline_ns=deadline_ns)
            driver.start(target, time.perf_counter_ns())
            for _ in range(80):
                if driver.report.terminal:
                    break
                if time.perf_counter_ns() >= deadline_ns:
                    raise TimeoutError("R1 control-frame batch deadline expired")
                result = driver.tick(
                    profile,
                    min(deadline_ns, time.perf_counter_ns() + 3_000_000_000),
                )
                if result is None and driver.report.state == "needs_approach":
                    break
            report = driver.report
            row = {
                **trial,
                "target_track_id": entity.track_id,
                "report": asdict(report),
                "runtime_ready": runtime.state is RuntimeStateV1.READY,
            }
            rows.append(row)
            append_jsonl(directory / "c1r-control-frame-trials.jsonl", row)
            if not (report.state == "complete" and report.reason == "hit_confirmed"):
                raise RuntimeError(
                    f"R1 control-frame trial {trial['trial_id']} failed: {report}"
                )
        finally:
            if runtime.state is RuntimeStateV1.READY:
                if driver is not None and not driver.report.terminal:
                    driver.cancel(profile, "r1_trial_cleanup")
                runtime.cancel_source(movement_source.source_id)
                runtime.unregister_ordered_source(movement_source)
                runtime.control_frame(
                    _task(trial["trial_id"] + "-release", deadline_ns),
                    profile,
                    min(deadline_ns, time.perf_counter_ns() + 500_000_000),
                    proposals=(ControlFrameProposalV1(
                        observation_request=ObservationRequestV3("navigation_v1"),
                    ),),
                )
    stages = {
        "schema_version": "mc2p.c1r-control-frame-runtime.v1",
        "planned_trials": len(plan),
        "completed_trials": len(rows),
        "trials": rows,
    }
    checks = [{
        "name": "r1_forty_driver_trials_completed",
        "passed": len(rows) == 40 and all(
            row["report"]["state"] == "complete"
            and row["report"]["reason"] == "hit_confirmed"
            and row["report"]["attack_submissions"] == 1
            for row in rows
        ),
    }]
    write_json_atomic(directory / "c1r-control-frame-runtime.json", stages)
    return stages, rows, checks
