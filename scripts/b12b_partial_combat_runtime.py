"""Frozen B12-B plan and evidence checks for combat under a navigation look."""
from __future__ import annotations

from dataclasses import asdict
import math
from pathlib import Path
import time
from typing import Callable, Iterable, Mapping

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import FieldStatusV0
from mc2p.contracts.intent_source import ControlFrameProposalV1
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.task import (
    ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0,
)
from mc2p.motion_nav.movement_transition import (
    GoalState, GoalSupport, MovementMode,
)
from mc2p.motion_nav.navigation_session import (
    NavigationSession, NavigationSessionProfiles,
)
from mc2p.motion_nav.world_model import Aabb
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1, RuntimeStateV1
from mc2p.skills.fixed_melee import CombatTargetV1
from mc2p.skills.melee_strike_driver import MeleeStrikeDriver
from mc2p.skills.moving_melee_driver import MovingMeleeDriver
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
from scripts.control_probe_core import append_jsonl, write_json_atomic


B12B_RUNTIME_INJECTIONS = (
    "observation_gap",
    "hidden_without_engagement",
    "target_or_world_revision",
)
_FABRIC_BOUNDARIES = (
    "large_combat_turn",
    "engaged_occlusion_navigation",
    "occluded_attack_reacquire",
)
_MOVEMENTS = {
    "forward": {"forward": 1, "strafe": 0},
    "backward": {"forward": -1, "strafe": 0},
    "left": {"forward": 0, "strafe": 1},
    "right": {"forward": 0, "strafe": -1},
}
_POSITIVE_REPEATS = {
    "forward": 8,
    "backward": 8,
    "left": 4,
    "right": 4,
}
_ROOT = Path(__file__).resolve().parents[1]
_CONFIG = _ROOT / "config/motion-navigation"
_PLAYER_START = {"x": .5, "y": 100.0, "z": -2.5}
_TARGET = {"x": .5, "y": 100.0, "z": -.25}
_TURN_TARGET = {"x": 2.091, "y": 100.0, "z": -.909}
_ROUTE_OFFSETS = {
    "forward": (0.0, 2.0),
    "backward": (0.0, -2.0),
    "left": (2.0, 0.0),
    "right": (-2.0, 0.0),
}


def _latency_summary(samples: Iterable[float]) -> dict:
    ordered = tuple(sorted(float(sample) for sample in samples))
    if not ordered:
        return {"count": 0, "p50": None, "p95": None, "p99": None,
                "max": None}

    def percentile(fraction: float) -> float:
        rank = max(1, math.ceil(fraction * len(ordered)))
        return ordered[rank - 1]

    return {
        "count": len(ordered),
        "p50": percentile(.50),
        "p95": percentile(.95),
        "p99": percentile(.99),
        "max": ordered[-1],
    }


def _timed_control(runtime: PlayerRuntimeV1, samples: list[float], operation):
    started = time.perf_counter_ns()
    blocking_started = runtime.backend_blocking_io_ns_total
    try:
        return operation()
    finally:
        wall_elapsed = time.perf_counter_ns() - started
        blocking_elapsed = (
            runtime.backend_blocking_io_ns_total - blocking_started
        )
        samples.append(max(0, wall_elapsed - blocking_elapsed) / 1_000_000)


def b12b_trial_plan(world_seed: int) -> tuple[dict, ...]:
    if type(world_seed) is not int:
        raise ValueError("B12-B world seed must be an integer")
    rows: list[dict] = []
    for direction, repeat_count in _POSITIVE_REPEATS.items():
        for repeat in range(1, repeat_count + 1):
            rows.append({
                "trial_id": f"positive-{direction}-{repeat:02d}",
                "classification": "positive",
                "direction": direction,
                "repeat": repeat,
                "world_seed": world_seed,
                "scenario_seed": world_seed * 1000 + len(rows) + 1,
                "movement": dict(_MOVEMENTS[direction]),
                "injection": None,
                "evidence_source": "fabric",
            })
    for evidence_source, injections in (
        ("fabric", _FABRIC_BOUNDARIES),
        ("runtime", B12B_RUNTIME_INJECTIONS),
    ):
        for injection in injections:
            for repeat in (1, 2):
                rows.append({
                    "trial_id": f"boundary-{injection.replace('_', '-')}-{repeat:02d}",
                    "classification": "boundary",
                    "direction": "forward",
                    "repeat": repeat,
                    "world_seed": world_seed,
                    "scenario_seed": world_seed * 1000 + len(rows) + 1,
                    "movement": dict(_MOVEMENTS["forward"]),
                    "injection": injection,
                    "evidence_source": evidence_source,
                })
    return tuple(rows)


def _matches_movement(event: Mapping, expected: Mapping) -> bool:
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


def _ordered_intents(records: Iterable[Mapping]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for record in records:
        if record.get("record_type") != "ordered_intent":
            continue
        intent = record.get("payload", {}).get("envelope", {}).get("intent")
        if type(intent) is dict and type(intent.get("intent_id")) is str:
            result[intent["intent_id"]] = intent
    return result


def evaluate_b12b_positive_evidence(
    trials: Iterable[Mapping],
    trace_records: Iterable[Mapping],
    control_events: Iterable[Mapping],
) -> tuple[list[dict], list[dict]]:
    """Join each positive attack to the route movement applied in that tick."""
    all_trials = tuple(dict(row) for row in trials)
    trials = tuple(
        row for row in all_trials if row.get("classification") == "positive"
    )
    intents = _ordered_intents(trace_records)
    events = tuple(dict(row) for row in control_events)
    attacks = tuple(row for row in events if row.get("event") == "attack_dispatched")
    inputs = tuple(row for row in events if row.get("event") == "input_consumed")
    evaluated: list[dict] = []
    for row in trials:
        track_id = row.get("target_track_id")
        intent = intents.get(row.get("movement_intent_id"))
        target_attacks = tuple(
            event for event in attacks
            if event.get("actual_operation", {}).get("entity_ref") == track_id
        )
        attack = target_attacks[0] if len(target_attacks) == 1 else None
        same_tick_inputs = () if attack is None else tuple(
            event for event in inputs
            if event.get("episode_id") == attack.get("episode_id")
            and event.get("request_sequence_id") == attack.get("request_sequence_id")
            and event.get("client_ticks") == attack.get("client_ticks")
            and _matches_movement(event, row["movement"])
        )
        report = row.get("report", {})
        failures = []
        checks = (
            (len(target_attacks) == 1, "attack_count_mismatch"),
            (len(same_tick_inputs) == 1, "same_tick_route_movement_missing"),
            (type(intent) is dict, "movement_intent_missing"),
            (type(intent) is dict
             and intent.get("movement_observed_yaw_limit_degrees") == 5.0,
             "observed_yaw_limit_missing"),
            (type(intent) is dict and intent.get("valid_for_ticks") == 1,
             "movement_intent_not_one_tick"),
            (report.get("state") == "complete"
             and report.get("reason") == "hit_confirmed"
             and report.get("attack_submissions") == 1
             and report.get("hit_observed") is True,
             "attack_not_confirmed"),
            (row.get("runtime_ready") is True, "runtime_not_ready"),
        )
        for passed, reason in checks:
            if not passed:
                failures.append(reason)
        evaluated.append({
            **row,
            "attack_event_count": len(target_attacks),
            "same_tick_movement_event_count": len(same_tick_inputs),
            "failures": failures,
            "passed": not failures,
        })
    checks = [
        {
            "name": "b12b_every_positive_has_bounded_same_tick_walk_and_attack",
            "passed": bool(evaluated) and all(row["passed"] for row in evaluated),
        },
        {
            "name": "b12b_no_wrong_entity_attack",
            "passed": not tuple(
                event for event in attacks
                if event.get("actual_operation", {}).get("entity_ref")
                not in {row.get("target_track_id") for row in all_trials}
            ),
        },
    ]
    return evaluated, checks


def evaluate_b12b_boundary_evidence(
    rows: Iterable[Mapping],
) -> tuple[list[dict], list[dict]]:
    """Check the concrete safety effect declared for each Fabric boundary."""
    evaluated = []
    for source in rows:
        row = dict(source)
        injection = row.get("injection")
        if injection == "large_combat_turn":
            conditions = (
                (row.get("movement_suppressed_count", 0) >= 1,
                 "old_heading_movement_not_suppressed"),
                (row.get("turn_selected_count", 0) >= 1,
                 "combat_turn_missing"),
                (row.get("resumed_movement_count", 0) >= 1,
                 "movement_did_not_resume"),
            )
        elif injection == "engaged_occlusion_navigation":
            conditions = (
                (row.get("engagement_position_uses", 0) >= 1,
                 "engagement_position_not_used"),
                (row.get("hidden_attack_submissions") == 0,
                 "attack_submitted_through_wall"),
                (row.get("hidden_movement_events", 0) >= 1,
                 "hidden_target_navigation_missing"),
            )
        elif injection == "occluded_attack_reacquire":
            conditions = (
                (row.get("engagement_position_uses", 0) >= 1,
                 "engagement_position_not_used"),
                (row.get("hidden_attack_submissions") == 0,
                 "attack_submitted_through_wall"),
                (row.get("reacquire_look_count", 0) >= 1,
                 "reacquire_look_missing"),
                (row.get("post_reveal_confirmed_hit") is True,
                 "attack_did_not_resume_after_reveal"),
            )
        else:
            conditions = ((False, "unknown_boundary_injection"),)
        failures = [reason for passed, reason in conditions if not passed]
        if row.get("runtime_ready") is not True:
            failures.append("runtime_not_ready")
        evaluated.append({**row, "failures": failures, "passed": not failures})
    checks = [{
        "name": "b12b_every_fabric_boundary_has_declared_effect",
        "passed": bool(evaluated) and all(row["passed"] for row in evaluated),
    }]
    return evaluated, checks


def _fixture_commands_at(
    start: Mapping[str, float],
    target: Mapping[str, float],
    *,
    yaw_degrees: float = 0.0,
) -> tuple[str, ...]:
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
         f"{yaw_degrees} 0"),
        (f"summon minecraft:zombie {target['x']} {target['y']} {target['z']} "
         "{NoAI:1b,PersistenceRequired:1b,Silent:1b,Tags:[\"mc2p_b12b\"]}"),
    )


def _fixture_commands(trial: Mapping) -> tuple[str, ...]:
    del trial
    return _fixture_commands_at(_PLAYER_START, _TARGET)


def _task(trial_id: str, deadline_ns: int) -> TaskIntentV0:
    return TaskIntentV0(
        "b12b-" + trial_id,
        "bounded_ground_combat_motion",
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
    return tuple(
        entity for entity in observation.perception.value.visible_entities
        if entity.entity_type == "minecraft:zombie"
    )


def _refresh_target(
    runtime: PlayerRuntimeV1,
    trial: Mapping,
    deadline_ns: int,
    control_decision_ms: list[float],
):
    profile = BehaviorProfileV0()
    task = _task(str(trial["trial_id"]) + "-observe", deadline_ns)
    for _ in range(60):
        now = time.perf_counter_ns()
        result = _timed_control(
            runtime, control_decision_ms,
            lambda: runtime.control_frame(
                task,
                profile,
                min(deadline_ns, now + 500_000_000),
                proposals=(ControlFrameProposalV1(
                    observation_request=ObservationRequestV3("navigation_v1"),
                ),),
            ),
        )
        if result.report.failure is not None:
            raise RuntimeError(
                "B12-B target observation failed: " + result.report.failure.reason
            )
        zombies = _visible_zombies(runtime)
        own = runtime.observation.self_state.value
        if own is not None and own.is_on_ground and len(zombies) == 1:
            candidate = zombies[0]
            checked = _timed_control(
                runtime, control_decision_ms,
                lambda: runtime.control_frame(
                    task,
                    profile,
                    min(deadline_ns, time.perf_counter_ns() + 500_000_000),
                    proposals=(ControlFrameProposalV1(
                        observation_request=ObservationRequestV3(
                            "interaction_v1", entity_track_id=candidate.track_id,
                        ),
                    ),),
                ),
            )
            tracked = runtime.observation.tracked_entity.value
            refreshed = _visible_zombies(runtime)
            if (checked.report.failure is None and tracked is not None
                    and tracked.track_id == candidate.track_id
                    and not tracked.is_dead
                    and any(entity.track_id == candidate.track_id
                            for entity in refreshed)):
                return next(
                    entity for entity in refreshed
                    if entity.track_id == candidate.track_id
                )
        time.sleep(.01)
    raise RuntimeError("B12-B fixture did not yield one visible zombie")


def _scan_flat_ground(
    runtime: PlayerRuntimeV1,
    trial: Mapping,
    deadline_ns: int,
    fixture_writer: Callable[[tuple[str, ...], dict], None],
    control_decision_ms: list[float],
) -> None:
    profile = BehaviorProfileV0()
    start = _PLAYER_START
    for yaw in (0, 90, 180, -90, 0):
        fixture_writer((
            f"tp MC2PProbe {start['x']} {start['y']} {start['z']} {yaw} 0",
        ), dict(trial))
        time.sleep(.06)
        now = time.perf_counter_ns()
        result = _timed_control(
            runtime, control_decision_ms,
            lambda: runtime.control_frame(
                _task(str(trial["trial_id"]) + f"-scan-{yaw}", deadline_ns),
                profile,
                min(deadline_ns, now + 500_000_000),
                proposals=(ControlFrameProposalV1(
                    observation_request=ObservationRequestV3("navigation_v1"),
                ),),
            ),
        )
        if result.report.failure is not None:
            raise RuntimeError(
                "B12-B ground scan failed: " + result.report.failure.reason
            )


def _goal(direction: str) -> GoalState:
    dx, dz = _ROUTE_OFFSETS[direction]
    x = _PLAYER_START["x"] + dx
    y = _PLAYER_START["y"]
    z = _PLAYER_START["z"] + dz
    return GoalState(
        Aabb(x - .1, y - .1, z - .1, x + .1, y + .1, z + .1),
        GoalSupport.SOLID,
        frozenset({MovementMode.WALK}),
        frozenset({"standing"}),
        .6,
    )


def _cleanup_navigation(
    navigation: RuntimeNavigationDriver,
    session: NavigationSession,
    profile: BehaviorProfileV0,
) -> None:
    try:
        for _ in range(30):
            if navigation.source is None:
                break
            navigation.stop(profile, "b12b_trial_cleanup")
    finally:
        session.close()


def _run_positive(
    runtime: PlayerRuntimeV1,
    episode: str,
    trial: dict,
    deadline_ns: int,
    profiles: NavigationSessionProfiles,
    fixture_writer: Callable[[tuple[str, ...], dict], None],
    control_decision_ms: list[float],
) -> dict:
    fixture_writer(_fixture_commands(trial), trial)
    _scan_flat_ground(
        runtime, trial, deadline_ns, fixture_writer, control_decision_ms,
    )
    entity = _refresh_target(
        runtime, trial, deadline_ns, control_decision_ms,
    )
    session = NavigationSession(
        "b12b-" + trial["trial_id"],
        profiles,
        observation_adapter=runtime.navigation_observation_adapter,
    )
    navigation = RuntimeNavigationDriver(runtime, session)
    profile = BehaviorProfileV0()
    strike = None
    movement_id = None
    try:
        navigation.start(
            "b12b-route-" + trial["trial_id"],
            1,
            _goal(trial["direction"]),
            time.perf_counter_ns(),
        )
        expected = MovementV1(**trial["movement"])
        for _ in range(80):
            result = _timed_control(
                runtime, control_decision_ms,
                lambda: navigation.tick(
                    profile,
                    min(deadline_ns, time.perf_counter_ns() + 500_000_000),
                ),
            )
            if (result.decision is not None
                    and result.decision.action.movement == expected):
                movement_id = dict(result.decision.selected_intents).get("movement")
                break
            if navigation.state in {"success", "failed", "cancelled"}:
                break
        if movement_id is None:
            raise RuntimeError(
                f"B12-B route did not produce {trial['direction']} movement: "
                f"{navigation.state}/{navigation.reason}"
            )

        strike = MeleeStrikeDriver(runtime, task_deadline_ns=deadline_ns)
        strike.start(CombatTargetV1(
            "b12b-task-" + trial["trial_id"],
            "b12b-combat-goal-" + trial["trial_id"],
            1,
            episode,
            entity.track_id,
        ), time.perf_counter_ns())
        shared_movement_id = None
        for _ in range(100):
            if strike.report.terminal:
                break
            if time.perf_counter_ns() >= deadline_ns:
                raise TimeoutError("B12-B positive batch deadline expired")
            owner_deadline = min(
                deadline_ns, time.perf_counter_ns() + 3_000_000_000,
            )
            result = _timed_control(
                runtime, control_decision_ms,
                lambda: strike.tick(
                    profile,
                    owner_deadline,
                    additional_proposal_supplier=(
                        None if navigation.source is None
                        else lambda: navigation.prepare_proposals(owner_deadline)
                    ),
                ),
            )
            if navigation.has_prepared_frame:
                if result is None:
                    navigation.discard_prepared()
                else:
                    navigation.adopt_result(result)
            if (result is not None and result.decision is not None
                    and result.decision.action.operation is not None):
                selected = dict(result.decision.selected_intents)
                if result.decision.action.movement == expected:
                    shared_movement_id = selected.get("movement")
            if result is None:
                time.sleep(.01)
        report = strike.report
        if shared_movement_id is None:
            raise RuntimeError("B12-B attack never shared the expected route movement")
        return {
            **trial,
            "target_track_id": entity.track_id,
            "movement_intent_id": shared_movement_id,
            "report": asdict(report),
            "runtime_ready": runtime.state is RuntimeStateV1.READY,
        }
    finally:
        if (strike is not None and not strike.report.terminal
                and runtime.state is RuntimeStateV1.READY):
            strike.cancel(profile, "b12b_trial_cleanup")
        if runtime.state is RuntimeStateV1.READY:
            _cleanup_navigation(navigation, session, profile)
        else:
            session.close()


def _nonneutral_movement(value: MovementV1 | None) -> bool:
    return value is not None and value != MovementV1()


def _navigation_until_moving(
    runtime: PlayerRuntimeV1,
    navigation: RuntimeNavigationDriver,
    profile: BehaviorProfileV0,
    deadline_ns: int,
    control_decision_ms: list[float],
    *,
    limit: int = 80,
) -> None:
    for _ in range(limit):
        result = _timed_control(
            runtime, control_decision_ms,
            lambda: navigation.tick(
                profile,
                min(deadline_ns, time.perf_counter_ns() + 500_000_000),
            ),
        )
        if result.decision is not None \
                and _nonneutral_movement(result.decision.action.movement):
            return
        if navigation.state in {"success", "failed", "cancelled"}:
            break
    raise RuntimeError(
        "B12-B boundary route did not produce movement: "
        f"{navigation.state}/{navigation.reason}"
    )


def _run_large_turn_boundary(
    runtime: PlayerRuntimeV1,
    episode: str,
    trial: dict,
    deadline_ns: int,
    profiles: NavigationSessionProfiles,
    fixture_writer: Callable[[tuple[str, ...], dict], None],
    control_decision_ms: list[float],
) -> dict:
    fixture_writer(
        _fixture_commands_at(_PLAYER_START, _TURN_TARGET), trial,
    )
    _scan_flat_ground(
        runtime, trial, deadline_ns, fixture_writer, control_decision_ms,
    )
    entity = _refresh_target(
        runtime, trial, deadline_ns, control_decision_ms,
    )
    session = NavigationSession(
        "b12b-" + trial["trial_id"], profiles,
        observation_adapter=runtime.navigation_observation_adapter,
    )
    navigation = RuntimeNavigationDriver(runtime, session)
    strike = None
    profile = BehaviorProfileV0()
    suppressed = turns = resumed = 0
    try:
        navigation.start(
            "b12b-route-" + trial["trial_id"], 1,
            _goal("forward"), time.perf_counter_ns(),
        )
        _navigation_until_moving(
            runtime, navigation, profile, deadline_ns, control_decision_ms,
        )
        strike = MeleeStrikeDriver(runtime, task_deadline_ns=deadline_ns)
        strike.start(CombatTargetV1(
            "b12b-task-" + trial["trial_id"],
            "b12b-combat-goal-" + trial["trial_id"],
            1, episode, entity.track_id,
        ), time.perf_counter_ns())
        for _ in range(30):
            owner_deadline = min(
                deadline_ns, time.perf_counter_ns() + 3_000_000_000,
            )
            captured: list[ControlFrameProposalV1] = []

            def proposals():
                prepared = navigation.prepare_proposals(owner_deadline)
                captured.extend(prepared)
                return prepared

            result = _timed_control(
                runtime, control_decision_ms,
                lambda: strike.tick(
                    profile, owner_deadline,
                    additional_proposal_supplier=(
                        None if navigation.source is None else proposals
                    ),
                ),
            )
            if navigation.has_prepared_frame:
                navigation.adopt_result(result)
            decision = result.decision
            if decision is None:
                continue
            navigation_ids = {
                ordered.intent.intent_id
                for proposal in captured
                for ordered in proposal.intents
                if _nonneutral_movement(ordered.intent.movement)
            }
            if (navigation_ids
                    and decision.action.look.yaw_delta_degrees != 0.0):
                turns += 1
            if any(
                intent_id in navigation_ids
                and reason == "observed_yaw_limit_exceeded"
                for intent_id, reason in decision.suppressed_intents
            ):
                suppressed += 1
            if suppressed and _nonneutral_movement(decision.action.movement):
                resumed += 1
                break
        return {
            **trial,
            "target_track_id": entity.track_id,
            "movement_suppressed_count": suppressed,
            "turn_selected_count": turns,
            "resumed_movement_count": resumed,
            "runtime_ready": runtime.state is RuntimeStateV1.READY,
        }
    finally:
        if (strike is not None and not strike.report.terminal
                and runtime.state is RuntimeStateV1.READY):
            strike.cancel(profile, "b12b_boundary_cleanup")
        if runtime.state is RuntimeStateV1.READY:
            _cleanup_navigation(navigation, session, profile)
        else:
            session.close()


def _cleanup_moving_melee(
    driver: MovingMeleeDriver,
    profile: BehaviorProfileV0,
) -> None:
    if not driver.report.terminal:
        driver.cancel(profile, "b12b_boundary_cleanup")
        for _ in range(30):
            if driver.report.terminal:
                break
            driver.tick(profile, time.perf_counter_ns() + 500_000_000)
    driver.navigation_session.close()


def _hidden_target_z(injection: str) -> float:
    if injection == "engaged_occlusion_navigation":
        return 3.5
    if injection == "occluded_attack_reacquire":
        return .5
    raise ValueError(f"unsupported B12-B occlusion injection: {injection}")


def _run_occlusion_boundary(
    runtime: PlayerRuntimeV1,
    episode: str,
    trial: dict,
    deadline_ns: int,
    profiles: NavigationSessionProfiles,
    fixture_writer: Callable[[tuple[str, ...], dict], None],
    control_decision_ms: list[float],
) -> dict:
    fixture_writer(_fixture_commands(trial), trial)
    _scan_flat_ground(
        runtime, trial, deadline_ns, fixture_writer, control_decision_ms,
    )
    entity = _refresh_target(
        runtime, trial, deadline_ns, control_decision_ms,
    )
    session = NavigationSession(
        "b12b-" + trial["trial_id"], profiles,
        observation_adapter=runtime.navigation_observation_adapter,
    )
    driver = MovingMeleeDriver(runtime, session)
    profile = BehaviorProfileV0()
    driver.start(CombatTargetV1(
        "b12b-task-" + trial["trial_id"],
        "b12b-combat-goal-" + trial["trial_id"],
        1, episode, entity.track_id,
    ), time.perf_counter_ns())
    hidden_attacks = hidden_movement = reacquire_looks = 0
    post_reveal_hit = False
    try:
        for _ in range(80):
            if driver.report.confirmed_hits >= 1:
                break
            if driver.report.terminal:
                break
            _timed_control(
                runtime, control_decision_ms,
                lambda: driver.tick(
                    profile,
                    min(deadline_ns, time.perf_counter_ns() + 3_000_000_000),
                ),
            )
        if driver.report.confirmed_hits < 1:
            raise RuntimeError("B12-B occlusion fixture did not establish engagement")
        attacks_before = driver.report.attack_submissions
        hidden_target_z = _hidden_target_z(trial["injection"])
        fixture_writer((
            f"tp @e[tag=mc2p_b12b,limit=1] 0.5 100 {hidden_target_z}",
            "fill 0 100 -1 0 102 -1 minecraft:stone replace",
        ), trial)
        time.sleep(.1)
        for _ in range(30):
            if driver.report.terminal:
                break
            result = _timed_control(
                runtime, control_decision_ms,
                lambda: driver.tick(
                    profile,
                    min(deadline_ns, time.perf_counter_ns() + 3_000_000_000),
                ),
            )
            if result is not None and result.decision is not None:
                decision = result.decision
                if _nonneutral_movement(decision.action.movement):
                    hidden_movement += 1
                if decision.action.look != type(decision.action.look)():
                    reacquire_looks += 1
            hidden_attacks = max(
                hidden_attacks,
                driver.report.attack_submissions - attacks_before,
            )
            if (trial["injection"] == "engaged_occlusion_navigation"
                    and driver.report.engagement_position_uses >= 1
                    and hidden_movement >= 1):
                break
            if (trial["injection"] == "occluded_attack_reacquire"
                    and driver.report.engagement_position_uses >= 1
                    and reacquire_looks >= 1):
                break
        if trial["injection"] == "occluded_attack_reacquire":
            fixture_writer((
                "fill 0 100 -1 0 102 -1 minecraft:air replace",
            ), trial)
            time.sleep(.1)
            for _ in range(80):
                if driver.report.confirmed_hits >= 2:
                    post_reveal_hit = True
                    break
                if driver.report.terminal:
                    break
                _timed_control(
                    runtime, control_decision_ms,
                    lambda: driver.tick(
                        profile,
                        min(deadline_ns, time.perf_counter_ns() + 3_000_000_000),
                    ),
                )
        return {
            **trial,
            "target_track_id": entity.track_id,
            "engagement_position_uses": driver.report.engagement_position_uses,
            "hidden_attack_submissions": hidden_attacks,
            "hidden_movement_events": hidden_movement,
            "reacquire_look_count": reacquire_looks,
            "post_reveal_confirmed_hit": post_reveal_hit,
            "runtime_ready": runtime.state is RuntimeStateV1.READY,
        }
    finally:
        if runtime.state is RuntimeStateV1.READY:
            _cleanup_moving_melee(driver, profile)
        else:
            session.close()


def _run_fabric_boundary(
    runtime: PlayerRuntimeV1,
    episode: str,
    trial: dict,
    deadline_ns: int,
    profiles: NavigationSessionProfiles,
    fixture_writer: Callable[[tuple[str, ...], dict], None],
    control_decision_ms: list[float],
) -> dict:
    if trial["injection"] == "large_combat_turn":
        return _run_large_turn_boundary(
            runtime, episode, trial, deadline_ns, profiles, fixture_writer,
            control_decision_ms,
        )
    return _run_occlusion_boundary(
        runtime, episode, trial, deadline_ns, profiles, fixture_writer,
        control_decision_ms,
    )


def run_b12b_partial_combat_runtime(
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
        raise ValueError("B12-B runtime requires ready PlayerRuntimeV1")
    directory = Path(directory)
    full_plan = b12b_trial_plan(world_seed)
    plan = tuple(
        row for row in full_plan
        if (row["classification"] == "positive"
            or row["evidence_source"] == "fabric")
    )
    write_json_atomic(directory / "b12b-partial-combat-manifest.json", {
        "schema_version": "mc2p.b12b-partial-combat-manifest.v1",
        "world_seed": world_seed,
        "code_hashes": dict(sorted(code_hashes.items())),
        "trials": list(plan),
    })
    profiles = NavigationSessionProfiles.load(_CONFIG)
    rows = []
    control_decision_ms: list[float] = []
    for trial in plan:
        runner = (_run_positive if trial["classification"] == "positive"
                  else _run_fabric_boundary)
        row = runner(
            runtime, episode, dict(trial), deadline_ns, profiles,
            fixture_writer, control_decision_ms,
        )
        rows.append(row)
        append_jsonl(directory / "b12b-partial-combat-trials.jsonl", row)
    stages = {
        "schema_version": "mc2p.b12b-partial-combat-runtime.v1",
        "planned_trials": len(plan),
        "completed_trials": len(rows),
        "control_decision_ms": _latency_summary(control_decision_ms),
        "trials": rows,
    }
    positive_rows = tuple(
        row for row in rows if row["classification"] == "positive"
    )
    boundary_rows = tuple(
        row for row in rows if row["classification"] == "boundary"
    )
    evaluated_boundaries, boundary_checks = evaluate_b12b_boundary_evidence(
        boundary_rows,
    )
    checks = [{
        "name": "b12b_twenty_four_positive_driver_trials_completed",
        "passed": len(positive_rows) == 24 and all(
            row["report"]["state"] == "complete"
            and row["report"]["reason"] == "hit_confirmed"
            and row["runtime_ready"] is True
            for row in positive_rows
        ),
    }, {
        "name": "b12b_six_fabric_boundaries_completed",
        "passed": len(boundary_rows) == 6
        and all(row["passed"] for row in evaluated_boundaries),
    }, {
        "name": "b12b_control_frame_budget",
        "passed": stages["control_decision_ms"]["p95"] is not None
        and stages["control_decision_ms"]["p95"] <= 8.0
        and stages["control_decision_ms"]["p99"] <= 15.0,
    }] + boundary_checks
    stages["evaluated_boundaries"] = evaluated_boundaries
    write_json_atomic(directory / "b12b-partial-combat-runtime.json", stages)
    return stages, rows, checks
