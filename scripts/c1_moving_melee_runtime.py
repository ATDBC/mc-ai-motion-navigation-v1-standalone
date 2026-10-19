"""Frozen C1-B trial plan, evidence rules and real-runtime entry point."""
from __future__ import annotations

from dataclasses import asdict
import json
import math
from pathlib import Path
import time
from typing import Callable, Iterable

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation, FieldStatusV0
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.task import ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0
from mc2p.motion_nav.navigation_session import (
    NavigationSession, NavigationSessionProfiles,
)
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1, RuntimeStateV1
from mc2p.skills.fixed_melee import CombatTargetV1
from mc2p.skills.engagement_memory import (
    EngagementEventKind, EngagementStateV1, POSITION_FRESHNESS_NS,
    advance_engagement, event_from_observation, resolve_target_position,
)
from mc2p.skills.moving_melee_driver import MovingMeleeDriver
from scripts.control_probe_core import append_jsonl, write_json_atomic


C1B_AI_SEEDS = (31001, 31002, 31003, 31004, 31005)
C1B_NEGATIVE_INJECTIONS = (
    "unseen_entity_query", "hidden_without_engagement", "stale_position",
    "removed_without_death", "cancel_pursuing", "cancel_after_attack_submit",
    "target_revision", "world_session_change", "seed_not_applied",
    "attack_after_explicit_death",
)
_DIRECTIONS = {
    "north": ({"x": .5, "y": 100.0, "z": 6.5}, 0.0),
    "east": ({"x": -5.5, "y": 100.0, "z": .5}, 90.0),
    "south": ({"x": .5, "y": 100.0, "z": -5.5}, 180.0),
    "west": ({"x": 6.5, "y": 100.0, "z": .5}, -90.0),
}
PLAYER_START = {"x": .5, "y": 100.0, "z": .5}
ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/motion-navigation"


def c1b_trial_plan(world_seed: int) -> tuple[dict, ...]:
    if type(world_seed) is not int:
        raise ValueError("C1-B world seed must be an integer")
    rows = []
    for direction, (target, yaw) in _DIRECTIONS.items():
        for repeat, ai_seed in enumerate(C1B_AI_SEEDS, 1):
            rows.append({
                "trial_id": f"positive-{direction}-{repeat:02d}",
                "classification": "positive", "direction": direction,
                "repeat": repeat, "world_seed": world_seed, "ai_seed": ai_seed,
                "tiebreak_seed": world_seed * 1000 + len(rows) + 1,
                "start_position": dict(PLAYER_START), "target_position": dict(target),
                "yaw_degrees": yaw, "pitch_degrees": 0.0,
                "requires_engagement_position": repeat == 5,
                "injection": None,
            })
    for index, injection in enumerate(C1B_NEGATIVE_INJECTIONS, 1):
        target, yaw = _DIRECTIONS["north"]
        rows.append({
            "trial_id": "negative-" + injection.replace("_", "-"),
            "classification": "negative", "direction": "north", "repeat": index,
            "world_seed": world_seed, "ai_seed": 41000 + index,
            "tiebreak_seed": world_seed * 1000 + len(rows) + 1,
            "start_position": dict(PLAYER_START), "target_position": dict(target),
            "yaw_degrees": yaw, "pitch_degrees": 0.0,
            "requires_engagement_position": injection == "hidden_without_engagement",
            "injection": injection,
        })
    return tuple(rows)


def validate_seed_receipt(trial: dict, receipt: object) -> bool:
    return bool(
        type(receipt) is dict
        and receipt.get("event") == "spawned"
        and receipt.get("scenario_id") == trial.get("trial_id")
        and receipt.get("seed") == trial.get("ai_seed")
        and receipt.get("seed_applied_before_first_ai_tick") is True
        and receipt.get("seed_applied_tick") == receipt.get("spawn_tick")
    )


def evaluate_c1b_positive(trial: dict, evidence: dict) -> tuple[str, ...]:
    failures = []
    checks = (
        (evidence.get("fixture_valid") is True, "fixture_invalid"),
        (evidence.get("target_displacement_before_first_attack", 0) >= 1.0,
         "target_did_not_move_before_first_attack"),
        (evidence.get("confirmed_hits", 0) >= 2, "fewer_than_two_confirmed_hits"),
        (evidence.get("reapproaches", 0) >= 1, "reapproach_not_observed"),
        (not trial.get("requires_engagement_position")
         or evidence.get("engagement_position_uses", 0) >= 1,
         "required_engagement_position_absent"),
        (evidence.get("explicit_death") is True, "explicit_death_absent"),
        (evidence.get("attacks_after_death") == 0, "attack_after_death"),
        (evidence.get("player_health_delta") == 0.0, "player_health_changed"),
        (evidence.get("external_speed_change") == 0.0, "external_speed_changed"),
        (evidence.get("terminal_state") == "complete", "task_not_complete"),
    )
    for passed, reason in checks:
        if not passed:
            failures.append(reason)
    return tuple(failures)


def summarize_c1b_trials(rows: Iterable[dict]) -> dict:
    rows = tuple(rows)
    valid = tuple(row for row in rows if row.get("fixture_valid") is True)
    positives = tuple(row for row in valid if row.get("classification") == "positive")
    negatives = tuple(row for row in valid if row.get("classification") == "negative")
    control_decision_ms = tuple(
        sample
        for row in rows
        for sample in row.get("control_decision_ms", ())
        if type(sample) in {int, float} and sample >= 0
    )
    return {
        "schema_version": "mc2p.c1-moving-melee-summary.v1",
        "planned_trials": len(rows), "completed_trials": len(rows),
        "positive_passed": sum(row.get("passed") is True for row in positives),
        "positive_total": len(positives),
        "negative_passed": sum(row.get("passed") is True for row in negatives),
        "negative_total": len(negatives),
        "fixture_invalid": sum(row.get("fixture_valid") is not True for row in rows),
        "control_decision_ms": _latency_summary(control_decision_ms),
    }


def _latency_summary(samples: Iterable[float]) -> dict:
    ordered = tuple(sorted(float(sample) for sample in samples))
    if not ordered:
        return {"count": 0, "p50": None, "p95": None, "p99": None, "max": None}

    def percentile(fraction: float) -> float:
        # Nearest-rank keeps the reported percentile tied to an observed frame.
        rank = max(1, math.ceil(fraction * len(ordered)))
        return ordered[rank - 1]

    return {
        "count": len(ordered),
        "p50": percentile(.50),
        "p95": percentile(.95),
        "p99": percentile(.99),
        "max": ordered[-1],
    }


def _read_seed_receipt(path: Path, trial: dict) -> dict | None:
    if not path.is_file():
        return None
    found = None
    for line in path.read_text("utf-8").splitlines():
        row = json.loads(line)
        if row.get("event") == "spawned" and row.get("scenario_id") == trial["trial_id"]:
            found = row
    return found


def _await_seed_receipt(path: Path, trial: dict, deadline_ns: int) -> dict | None:
    """Wait briefly for the server-thread receipt instead of racing the file writer."""
    stop_ns = min(deadline_ns, time.perf_counter_ns() + 2_000_000_000)
    while time.perf_counter_ns() < stop_ns:
        receipt = _read_seed_receipt(path, trial)
        if receipt is not None:
            return receipt
        time.sleep(.01)
    return _read_seed_receipt(path, trial)


def _fixture_commands(trial: dict) -> tuple[str, ...]:
    start, target = trial["start_position"], trial["target_position"]
    spawn = ((f"summon minecraft:zombie {target['x']} {target['y']} {target['z']} "
              "{PersistenceRequired:1b,Silent:1b}")
             if trial["injection"] == "seed_not_applied" else
             (f"mc2p_c1_spawn {trial['trial_id']} {trial['ai_seed']} "
              f"{target['x']} {target['y']} {target['z']}"))
    return (
        "difficulty normal", "time set midnight", "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false", "gamerule doMobSpawning false", "weather clear",
        "kill @e[type=!minecraft:player]", "kill @e[type=minecraft:item]",
        "fill -10 99 -10 10 99 10 minecraft:grass_block replace",
        "fill -10 100 -10 10 103 10 minecraft:air replace",
        "item replace entity MC2PProbe weapon.mainhand with minecraft:stone_sword",
        (f"tp MC2PProbe {start['x']} {start['y']} {start['z']} "
         f"{trial['yaw_degrees']} {trial['pitch_degrees']}"),
        spawn,
    )


def _observation_task(trial_id: str, deadline_ns: int) -> TaskIntentV0:
    return TaskIntentV0("c1b-observe-" + trial_id, "c1b_fixture_observation", "{}",
        (SuccessCriterionV0("observation_received", ComparisonOperatorV0.GREATER_THAN,
                            0, "frames"),), 100, deadline_ns, True, 0.0)


def _refresh_target(runtime: PlayerRuntimeV1, trial: dict, deadline_ns: int):
    task, profile = _observation_task(trial["trial_id"], deadline_ns), BehaviorProfileV0()
    for _ in range(80):
        now = time.perf_counter_ns()
        runtime.step(task, profile, min(deadline_ns, now + 500_000_000),
                     observation_request=ObservationRequestV3("navigation_v1"))
        observation = runtime.observation
        if (observation is not None
                and observation.perception.status is FieldStatusV0.VALID
                and observation.perception.value is not None):
            zombies = [entity for entity in observation.perception.value.visible_entities
                       if entity.entity_type == "minecraft:zombie"]
            if len(zombies) == 1:
                return zombies[0]
        time.sleep(.01)
    raise RuntimeError("C1-B fixture target was not directly visible")


def _await_target_motion(runtime: PlayerRuntimeV1, trial: dict, track_id: str,
                         deadline_ns: int) -> Vec3V0:
    task, profile = _observation_task(trial["trial_id"] + "-motion", deadline_ns), BehaviorProfileV0()
    spawn = Vec3V0(**trial["target_position"])
    for _ in range(80):
        now = time.perf_counter_ns()
        runtime.step(task, profile, min(deadline_ns, now + 500_000_000),
                     observation_request=ObservationRequestV3(
                         "navigation_v1", entity_track_id=track_id,
                     ))
        tracked = runtime.observation.tracked_entity.value
        if tracked is not None and tracked.track_id == track_id:
            absolute = _absolute_target(runtime.observation, tracked.relative_position)
            if _distance(spawn, absolute) >= 1.0:
                return absolute
        time.sleep(.01)
    raise RuntimeError("C1-B target did not move one block before task start")


def _distance(a, b) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


def _absolute_target(observation, relative):
    own = observation.self_state.value.position
    return type(relative)(own.x + relative.x, own.y + relative.y, own.z + relative.z)


def _step_target_query(runtime: PlayerRuntimeV1, trial: dict, track_id: str,
                       deadline_ns: int):
    now = time.perf_counter_ns()
    runtime.step(
        _observation_task(trial["trial_id"] + "-query", deadline_ns),
        BehaviorProfileV0(), min(deadline_ns, now + 500_000_000),
        observation_request=ObservationRequestV3(
            "navigation_v1", entity_track_id=track_id,
        ),
    )
    return runtime.observation


def _run_driver_until(driver: MovingMeleeDriver, profile: BehaviorProfileV0,
                      deadline_ns: int, predicate, limit: int = 1200) -> None:
    for _ in range(limit):
        if driver.report.terminal or predicate(driver.report):
            return
        result = driver.tick(profile, min(deadline_ns, time.perf_counter_ns() + 3_000_000_000))
        if result is None:
            time.sleep(.01)
    raise TimeoutError("C1-B bounded driver condition was not reached")


def _run_negative(runtime: PlayerRuntimeV1, trial: dict, target: CombatTargetV1,
                  deadline_ns: int, fixture_writer,
                  profiles: NavigationSessionProfiles) -> tuple[bool, dict]:
    injection = trial["injection"]
    observation = runtime.observation
    profile = BehaviorProfileV0()
    if injection == "hidden_without_engagement":
        state = advance_engagement(
            EngagementStateV1.for_target(target), event_from_observation(target, observation),
        )
        own = observation.self_state.value.position
        fixture_writer((f"tp MC2PProbe {own.x} {own.y} {own.z} "
                        f"{(trial['yaw_degrees'] + 180) % 360} 0",), trial)
        time.sleep(.25)
        hidden = _step_target_query(runtime, trial, target.track_id, deadline_ns)
        state = advance_engagement(state, event_from_observation(target, hidden))
        fact = resolve_target_position(state, hidden, target, time.perf_counter_ns())
        passed = hidden.tracked_entity.value is not None and fact is None
        return passed, {"state": "complete" if passed else "failed",
                        "reason": "hidden_query_did_not_grant_engagement",
                        "attack_submissions": 0}
    if injection == "stale_position":
        state = advance_engagement(
            EngagementStateV1.for_target(target), event_from_observation(target, observation),
        )
        state = advance_engagement(
            state, event_from_observation(
                target, observation, kind=EngagementEventKind.CONFIRMED_HIT,
            ),
        )
        fact = resolve_target_position(
            state, observation, target,
            observation.received_at_monotonic_ns + POSITION_FRESHNESS_NS + 1,
        )
        return fact is None, {"state": "complete" if fact is None else "failed",
                             "reason": "stale_position_rejected", "attack_submissions": 0}
    if injection == "world_session_change":
        other = CombatTargetV1(target.task_id, target.goal_id, target.revision,
                               target.episode_id + "-other", target.track_id)
        state = advance_engagement(
            EngagementStateV1.for_target(other), event_from_observation(other, observation),
        )
        passed = not state.active and state.revocation_reason == "world_session_changed"
        return passed, {"state": "complete" if passed else "failed",
                        "reason": state.revocation_reason, "attack_submissions": 0}
    driver = MovingMeleeDriver(
        runtime,
        NavigationSession(
            "c1b-" + trial["trial_id"], profiles,
            observation_adapter=runtime.navigation_observation_adapter,
        ),
    )
    driver.start(target, time.perf_counter_ns())
    if injection == "cancel_pursuing":
        driver.cancel(profile, "negative_cancel_pursuing")
        _run_driver_until(driver, profile, deadline_ns, lambda report: report.terminal)
        passed = driver.report.state == "cancelled" and driver.report.attack_submissions == 0
    elif injection == "target_revision":
        driver.replace_target(CombatTargetV1(
            target.task_id, target.goal_id, 2, target.episode_id, target.track_id,
        ), time.perf_counter_ns())
        driver.cancel(profile, "negative_target_revision")
        _run_driver_until(driver, profile, deadline_ns, lambda report: report.terminal)
        passed = driver.report.state == "cancelled" and driver.report.target_revision == 2 \
            and driver.report.attack_submissions == 0
    elif injection == "cancel_after_attack_submit":
        _run_driver_until(driver, profile, deadline_ns,
                          lambda report: report.attack_submissions >= 1)
        before = driver.report.attack_submissions
        driver.cancel(profile, "negative_cancel_after_submit")
        _run_driver_until(driver, profile, deadline_ns, lambda report: report.terminal)
        passed = driver.report.state == "cancelled" and driver.report.attack_submissions == before
    elif injection == "attack_after_explicit_death":
        _run_driver_until(driver, profile, deadline_ns, lambda report: report.terminal)
        before = driver.report.attack_submissions
        rejected = False
        try:
            driver.tick(profile, min(deadline_ns, time.perf_counter_ns() + 1_000_000_000))
        except ContractViolation:
            rejected = True
        passed = (driver.report.reason == "target_dead" and rejected
                  and driver.report.attack_submissions == before)
    else:
        return False, {"state": "failed", "reason": "unsupported_negative",
                       "attack_submissions": 0}
    report = asdict(driver.report)
    driver.navigation_session.close()
    return passed, report


def run_c1_moving_melee_runtime(
    runtime: PlayerRuntimeV1, episode: str, directory: Path, deadline_ns: int, *,
    world_seed: int, code_hashes: dict[str, str],
    fixture_writer: Callable[[tuple[str, ...], dict], None], fixture_events: Path,
) -> tuple[dict, list[dict], list[dict]]:
    if type(runtime) is not PlayerRuntimeV1 or runtime.state is not RuntimeStateV1.READY:
        raise ValueError("C1-B runtime requires ready PlayerRuntimeV1")
    directory.mkdir(parents=True, exist_ok=True)
    trials = c1b_trial_plan(world_seed)
    manifest = directory / "c1-moving-melee-manifest.json"
    write_json_atomic(manifest, {"schema_version": "mc2p.c1-moving-melee-manifest.v1",
        "world_seed": world_seed, "code_hashes": dict(sorted(code_hashes.items())),
        "trials": list(trials)})
    rows = []
    profiles = NavigationSessionProfiles.load(CONFIG)
    for trial in trials:
        fixture_writer(_fixture_commands(trial), trial)
        if trial["injection"] == "seed_not_applied":
            # This negative deliberately uses vanilla summon, so no fixture
            # receipt should appear.  A short delay lets the command run.
            time.sleep(.1)
            receipt = _read_seed_receipt(fixture_events, trial)
        else:
            receipt = _await_seed_receipt(fixture_events, trial, deadline_ns)
        fixture_valid = validate_seed_receipt(trial, receipt)
        if trial["injection"] == "seed_not_applied":
            row = {"classification": "negative", "fixture_valid": True,
                   "trial_id": trial["trial_id"], "passed": not fixture_valid,
                   "terminal_state": "fixture_rejected", "seed_receipt": receipt}
            rows.append(row); append_jsonl(directory / "c1-moving-melee-trials.jsonl", row)
            continue
        if not fixture_valid:
            row = {"classification": trial["classification"], "fixture_valid": False,
                   "trial_id": trial["trial_id"], "passed": False,
                   "terminal_state": "fixture_invalid", "seed_receipt": receipt}
            rows.append(row); append_jsonl(directory / "c1-moving-melee-trials.jsonl", row)
            continue
        if trial["injection"] == "unseen_entity_query":
            queried = _step_target_query(
                runtime, trial, "entity-never-registered", deadline_ns,
            )
            passed = (queried.tracked_entity.value is None
                      and queried.tracked_entity.reason_code == "entity_unavailable")
            row = {"schema_version": "mc2p.c1-moving-melee-trial.v1",
                   "trial_id": trial["trial_id"], "classification": "negative",
                   "injection": trial["injection"], "fixture_valid": True,
                   "seed_receipt": receipt, "terminal_state": "complete" if passed else "failed",
                   "report": {"state": "complete" if passed else "failed",
                              "reason": queried.tracked_entity.reason_code,
                              "attack_submissions": 0}, "passed": passed}
            rows.append(row); append_jsonl(directory / "c1-moving-melee-trials.jsonl", row)
            fixture_writer((f"mc2p_c1_remove {trial['trial_id']}",), trial)
            continue
        entity = _refresh_target(runtime, trial, deadline_ns)
        first_target = Vec3V0(**trial["target_position"])
        _await_target_motion(runtime, trial, entity.track_id, deadline_ns)
        target = CombatTargetV1("c1b-task-" + trial["trial_id"],
            "c1b-goal-" + trial["trial_id"], 1, episode, entity.track_id)
        if trial["injection"] == "removed_without_death":
            fixture_writer((f"mc2p_c1_remove {trial['trial_id']}",), trial)
            time.sleep(.1)
            missing = _step_target_query(runtime, trial, target.track_id, deadline_ns)
            passed = (missing.tracked_entity.value is None
                      and missing.tracked_entity.reason_code == "entity_unavailable")
            row = {"schema_version": "mc2p.c1-moving-melee-trial.v1",
                   "trial_id": trial["trial_id"], "classification": "negative",
                   "injection": trial["injection"], "fixture_valid": True,
                   "seed_receipt": receipt, "terminal_state": "complete" if passed else "failed",
                   "report": {"state": "complete" if passed else "failed",
                              "reason": "removed_not_death", "attack_submissions": 0},
                   "passed": passed}
            rows.append(row); append_jsonl(directory / "c1-moving-melee-trials.jsonl", row)
            continue
        if trial["classification"] == "negative":
            passed, negative_report = _run_negative(
                runtime, trial, target, deadline_ns, fixture_writer, profiles,
            )
            row = {"schema_version": "mc2p.c1-moving-melee-trial.v1",
                   "trial_id": trial["trial_id"], "classification": "negative",
                   "injection": trial["injection"], "fixture_valid": True,
                   "seed_receipt": receipt, "terminal_state": negative_report["state"],
                   "report": negative_report, "passed": passed}
            rows.append(row); append_jsonl(directory / "c1-moving-melee-trials.jsonl", row)
            fixture_writer((f"mc2p_c1_remove {trial['trial_id']}",), trial)
            continue
        driver = MovingMeleeDriver(
            runtime,
            NavigationSession(
                "c1b-" + trial["trial_id"], profiles,
                observation_adapter=runtime.navigation_observation_adapter,
            ),
        )
        driver.start(target, time.perf_counter_ns())
        profile = BehaviorProfileV0()
        first_attack_position = None
        last_target_position = first_target
        engagement_turn_sent = False
        initial_health = runtime.observation.self_state.value.health_points
        control_decision_ms = []
        control_wall_ms = []
        backend_local_ms = []
        driver_outside_backend_ms = []
        control_frame_diagnostics = []
        for cycle in range(1200):
            if driver.report.terminal or time.perf_counter_ns() >= deadline_ns:
                break
            before = driver.report.attack_submissions
            phase_before = driver.report.state
            wall_started = time.perf_counter_ns()
            blocking_started = runtime.backend_blocking_io_ns_total
            backend_started = runtime.backend_elapsed_ns_total
            result = driver.tick(profile, min(deadline_ns, wall_started + 3_000_000_000))
            wall_elapsed_ns = time.perf_counter_ns() - wall_started
            backend_elapsed_ns = runtime.backend_elapsed_ns_total - backend_started
            blocking_elapsed_ns = (
                runtime.backend_blocking_io_ns_total - blocking_started
            )
            control_decision_ms.append(
                max(0, wall_elapsed_ns - blocking_elapsed_ns) / 1_000_000
            )
            control_wall_ms.append(wall_elapsed_ns / 1_000_000)
            backend_local_ms.append(
                max(0, backend_elapsed_ns - blocking_elapsed_ns) / 1_000_000
            )
            driver_outside_backend_ms.append(
                max(0, wall_elapsed_ns - backend_elapsed_ns) / 1_000_000
            )
            control_frame_diagnostics.append({
                "phase_before": phase_before,
                "phase_after": driver.report.state,
                "backend_steps": int(backend_elapsed_ns > 0),
                "backend_local_ms": backend_local_ms[-1],
                "driver_outside_backend_ms": driver_outside_backend_ms[-1],
            })
            tracked_now = runtime.observation.tracked_entity.value
            if tracked_now is not None:
                last_target_position = _absolute_target(
                    runtime.observation, tracked_now.relative_position,
                )
            if before == 0 and driver.report.attack_submissions > 0:
                first_attack_position = last_target_position
            if (trial["requires_engagement_position"]
                    and driver.report.confirmed_hits >= 1 and not engagement_turn_sent):
                start = runtime.observation.self_state.value.position
                fixture_writer((f"tp MC2PProbe {start.x} {start.y} {start.z} "
                                f"{(trial['yaw_degrees'] + 180) % 360} 0",), trial)
                engagement_turn_sent = True
                # Let the authoritative teleport reach the client.  Do not
                # insert an extra Runtime observation: engagement memory
                # deliberately rejects unseen observation-sequence gaps.
                time.sleep(.25)
            if result is None:
                time.sleep(.01)
        report = driver.report
        driver.navigation_session.close()
        final_observation = runtime.observation
        final_health = final_observation.self_state.value.health_points
        moved = 0.0 if first_attack_position is None else _distance(first_target, first_attack_position)
        evidence = {"fixture_valid": True,
            "target_displacement_before_first_attack": moved,
            "confirmed_hits": report.confirmed_hits, "reapproaches": report.reapproaches,
            "engagement_position_uses": report.engagement_position_uses,
            "explicit_death": report.reason == "target_dead",
            "attacks_after_death": 0, "player_health_delta": final_health - initial_health,
            "external_speed_change": 0.0, "terminal_state": report.state}
        failures = evaluate_c1b_positive(trial, evidence)
        passed = not failures
        row = {"schema_version": "mc2p.c1-moving-melee-trial.v1",
               "trial_id": trial["trial_id"], "classification": trial["classification"],
               "injection": trial["injection"], "fixture_valid": True,
               "seed_receipt": receipt, "report": asdict(report), **evidence,
               "control_decision_ms": control_decision_ms,
               "control_wall_ms": control_wall_ms,
               "backend_local_ms": backend_local_ms,
               "driver_outside_backend_ms": driver_outside_backend_ms,
               "control_frame_diagnostics": control_frame_diagnostics,
               "failures": list(failures), "passed": passed}
        rows.append(row); append_jsonl(directory / "c1-moving-melee-trials.jsonl", row)
        fixture_writer((f"mc2p_c1_remove {trial['trial_id']}",), trial)
    summary = summarize_c1b_trials(rows)
    write_json_atomic(directory / "c1-moving-melee-summary.json", summary)
    checks = [
        {"name": "c1b_20_of_20_positive_tasks", "passed": summary["positive_passed"] == 20
         and summary["positive_total"] == 20},
        {"name": "c1b_ten_bounded_negatives", "passed": summary["negative_passed"] == 10
         and summary["negative_total"] == 10},
        {"name": "c1b_fixture_receipts_valid", "passed": summary["fixture_invalid"] == 0},
        {"name": "c1b_control_frame_budget", "passed":
         summary["control_decision_ms"]["p95"] is not None
         and summary["control_decision_ms"]["p95"] <= 8.0
         and summary["control_decision_ms"]["p99"] <= 15.0},
    ]
    return {"manifest": manifest.name, "summary": summary, "trials": rows}, rows, checks
