"""Frozen C1-C real-damage trials and runtime result accounting."""
from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import time
from typing import Callable, Iterable

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation import Vec3V0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1, RuntimeStateV1
from mc2p.motion_nav.external_motion import DamageKnockbackDetector
from mc2p.motion_nav.external_motion_recovery import (
    ExternalMotionRecoveryController, RecoveryDirective,
)
from mc2p.motion_nav.navigation_session import (
    NavigationSession, NavigationSessionProfiles,
)
from mc2p.skills.fixed_melee import CombatTargetV1
from mc2p.skills.moving_melee_driver import MovingMeleeDriver
from scripts.c1_moving_melee_runtime import (
    PLAYER_START,
    _DIRECTIONS,
    _absolute_target,
    _await_seed_receipt,
    _await_target_motion,
    _distance,
    _latency_summary,
    _read_seed_receipt,
    _refresh_target,
)
from scripts.control_probe_core import append_jsonl, write_json_atomic


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/motion-navigation"
C1C_AI_SEEDS = tuple(range(51001, 51021))
C1C_NEGATIVE_INJECTIONS = (
    "route_deviation_without_damage",
    "missing_or_regressed_hurt_evidence",
    "late_old_plan",
    "cancel_during_recovery",
    "target_revision_during_recovery",
    "world_change_during_recovery",
    "target_death_during_recovery",
    "recovery_tick_budget_exhausted",
    "fifth_damage_event",
    "duplicate_attack_after_damage",
)


def wait_after_fast_poll(result) -> None:
    """Yield until the next 20 Hz driver window after a non-control poll."""
    if result is None:
        time.sleep(.01)


def selected_retired_route(
    selected_movement: str | None,
    retired_movement_sources: Iterable[str],
) -> bool:
    """Return whether this frame selected a route retired before the frame."""
    return bool(
        selected_movement is not None
        and any(
            selected_movement.startswith(source_id + "/")
            for source_id in retired_movement_sources
        )
    )


_POSITIVE_STAGES = (
    "pursuing", "pursuing", "aim_or_cooldown",
    "attack_submitted", "post_recovery_rehit",
)


def c1c_trial_plan(world_seed: int) -> tuple[dict, ...]:
    if type(world_seed) is not int:
        raise ValueError("C1-C world seed must be an integer")
    rows: list[dict] = []
    seed_index = 0
    for direction, (target, yaw) in _DIRECTIONS.items():
        for repeat, stage in enumerate(_POSITIVE_STAGES, 1):
            rows.append({
                "trial_id": f"positive-{stage.replace('_', '-')}-{direction}-{repeat:02d}",
                "classification": "positive",
                "direction": direction,
                "repeat": repeat,
                "world_seed": world_seed,
                "ai_seed": C1C_AI_SEEDS[seed_index],
                "tiebreak_seed": world_seed * 1000 + len(rows) + 1,
                "start_position": dict(PLAYER_START),
                "target_position": dict(target),
                "yaw_degrees": yaw,
                "pitch_degrees": 0.0,
                "damage_stage": stage,
                "damage_mode": "real",
                "attack_trigger": "controlled_vanilla_attack",
                "maximum_recovery_ticks": 40,
                "maximum_external_motion_events": 4,
                "injection": None,
            })
            seed_index += 1
    target, yaw = _DIRECTIONS["north"]
    for index, injection in enumerate(C1C_NEGATIVE_INJECTIONS, 1):
        rows.append({
            "trial_id": "negative-" + injection.replace("_", "-"),
            "classification": "negative",
            "direction": "north",
            "repeat": index,
            "world_seed": world_seed,
            "ai_seed": 61000 + index,
            "tiebreak_seed": world_seed * 1000 + len(rows) + 1,
            "start_position": dict(PLAYER_START),
            "target_position": dict(target),
            "yaw_degrees": yaw,
            "pitch_degrees": 0.0,
            "damage_stage": None,
            "damage_mode": "real",
            "attack_trigger": "controlled_vanilla_attack",
            "maximum_recovery_ticks": 40,
            "maximum_external_motion_events": 4,
            "injection": injection,
        })
    return tuple(rows)


def validate_c1c_seed_receipt(trial: dict, receipt: object) -> bool:
    return bool(
        type(receipt) is dict
        and receipt.get("event") == "spawned"
        and receipt.get("scenario_id") == trial.get("trial_id")
        and receipt.get("seed") == trial.get("ai_seed")
        and receipt.get("seed_applied_before_first_ai_tick") is True
        and receipt.get("seed_applied_tick") == receipt.get("spawn_tick")
        and receipt.get("damage_mode") == "real"
    )


def evaluate_c1c_positive(trial: dict, evidence: dict) -> tuple[str, ...]:
    failures: list[str] = []
    minimum_events = 2 if trial.get("damage_stage") == "post_recovery_rehit" else 1
    checks = (
        (evidence.get("fixture_valid") is True, "fixture_invalid"),
        (evidence.get("controlled_attack_requests", 0) == minimum_events,
         "controlled_attack_request_mismatch"),
        (evidence.get("controlled_attack_successes", 0) == minimum_events,
         "controlled_attack_failed"),
        (evidence.get("real_damage_events", 0) >= minimum_events,
         "real_damage_missing"),
        (evidence.get("observed_damage_stage") == trial.get("damage_stage"),
         "damage_stage_mismatch"),
        (evidence.get("damage_transitions", 0) >= minimum_events,
         "damage_transition_missing"),
        (evidence.get("external_motion_events", 0) >= minimum_events,
         "external_motion_event_missing"),
        (evidence.get("external_recoveries_completed", 0) >= 1,
         "recovery_not_completed"),
        (evidence.get("maximum_recovery_elapsed_ticks", 41) <= 40,
         "recovery_tick_budget_exceeded"),
        (evidence.get("parallel_recovery_owners", 1) == 0,
         "parallel_recovery_owner"),
        (evidence.get("stale_route_takeovers", 1) == 0,
         "stale_route_takeover"),
        (evidence.get("duplicate_attacks", 1) == 0,
         "duplicate_attack"),
        (evidence.get("reanchors", 0) >= 1, "reanchor_missing"),
        (evidence.get("explicit_death") is True, "explicit_death_absent"),
        (evidence.get("terminal_state") == "complete", "task_not_complete"),
    )
    for passed, reason in checks:
        if not passed:
            failures.append(reason)
    return tuple(failures)


def summarize_c1c_trials(rows: Iterable[dict]) -> dict:
    rows = tuple(rows)
    valid = tuple(row for row in rows if row.get("fixture_valid") is True)
    positives = tuple(row for row in valid if row.get("classification") == "positive")
    negatives = tuple(row for row in valid if row.get("classification") == "negative")
    control_ms = tuple(
        value for row in rows for value in row.get("control_decision_ms", ())
        if type(value) in {int, float} and value >= 0
    )
    response_ms = tuple(
        value for row in rows for value in row.get("recovery_response_ms", ())
        if type(value) in {int, float} and value >= 0
    )
    return {
        "schema_version": "mc2p.c1-external-motion-summary.v1",
        "planned_trials": len(rows),
        "completed_trials": len(rows),
        "positive_passed": sum(row.get("passed") is True for row in positives),
        "positive_total": len(positives),
        "negative_passed": sum(row.get("passed") is True for row in negatives),
        "negative_total": len(negatives),
        "fixture_invalid": sum(row.get("fixture_valid") is not True for row in rows),
        "control_decision_ms": _latency_summary(control_ms),
        "recovery_response_ms": _latency_summary(response_ms),
    }


def _fixture_commands(trial: dict) -> tuple[str, ...]:
    start, target = trial["start_position"], trial["target_position"]
    return (
        "difficulty normal", "time set midnight", "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false", "gamerule doMobSpawning false", "weather clear",
        "kill @e[type=!minecraft:player]", "kill @e[type=minecraft:item]",
        "fill -10 99 -10 10 99 10 minecraft:grass_block replace",
        "fill -10 100 -10 10 103 10 minecraft:air replace",
        "effect clear MC2PProbe", "attribute MC2PProbe minecraft:generic.max_health base set 20",
        "effect give MC2PProbe minecraft:instant_health 1 10 true",
        # C1-C isolates recovery rather than repeating C1-B's long pursuit
        # endurance test. Three genuine sword hits still exercise cadence and
        # post-recovery re-engagement while leaving deadline headroom.
        "item replace entity MC2PProbe weapon.mainhand with minecraft:diamond_sword",
        (f"tp MC2PProbe {start['x']} {start['y']} {start['z']} "
         f"{trial['yaw_degrees']} {trial['pitch_degrees']}"),
        (f"mc2p_c1_spawn {trial['trial_id']} {trial['ai_seed']} "
         f"{target['x']} {target['y']} {target['z']} real"),
    )


def _tick_until(
    driver: MovingMeleeDriver,
    profile: BehaviorProfileV0,
    deadline_ns: int,
    predicate,
    limit: int = 1200,
) -> bool:
    for _ in range(limit):
        if predicate() or driver.report.terminal or time.perf_counter_ns() >= deadline_ns:
            return predicate()
        driver.tick(profile, min(deadline_ns, time.perf_counter_ns() + 3_000_000_000))
    return predicate()


def controlled_attack_due(
    stage: str,
    report: object,
    own: object,
    requests_sent: int,
) -> bool:
    if getattr(own, "hurt_animation_ticks", None) != 0:
        return False
    if stage == "pursuing":
        return requests_sent == 0 and getattr(report, "state", None) == "pursuing"
    if stage == "aim_or_cooldown":
        return bool(
            requests_sent == 0
            and getattr(report, "state", None) == "striking"
            and getattr(report, "attack_submissions", 0) == 0
        )
    if stage == "attack_submitted":
        return requests_sent == 0 and getattr(report, "attack_submissions", 0) >= 1
    if stage == "post_recovery_rehit":
        if requests_sent == 0:
            return getattr(report, "state", None) == "pursuing"
        return bool(
            requests_sent == 1
            and getattr(report, "external_recoveries_completed", 0) >= 1
            and getattr(report, "state", None) != "recovering_external_motion"
        )
    raise ValueError("unknown controlled attack stage")


def recovery_application_latency_ms(event_client_ns: int, result: object) -> float | None:
    """Measure application latency entirely on the client JVM monotonic clock."""
    if type(event_client_ns) is not int or event_client_ns < 0 or result is None:
        return None
    decision = getattr(result, "decision", None)
    backend_result = getattr(result, "backend_result", None)
    if decision is None or backend_result is None:
        return None
    request_sequence = decision.action.request_sequence_id
    applications = getattr(backend_result.receipt, "input_applications", ())
    application = next((item for item in applications
                        if item.request_sequence_id == request_sequence), None)
    if application is None or application.sampled_at_jvm_ns < event_client_ns:
        return None
    return (application.sampled_at_jvm_ns - event_client_ns) / 1_000_000


def _fixture_events_for_trial(path: Path, trial_id: str) -> tuple[dict, ...]:
    if not path.is_file():
        return ()
    rows = []
    for line in path.read_text("utf-8").splitlines():
        row = json.loads(line)
        if row.get("scenario_id") == trial_id:
            rows.append(row)
    return tuple(rows)


def _await_controlled_attack(
    path: Path,
    trial_id: str,
    expected_count: int,
    deadline_ns: int,
) -> dict | None:
    """Wait for the server to apply an injected attack before advancing combat.

    The console command is asynchronous.  Advancing the melee driver
    immediately after writing it can submit the player's own attack first and
    make the recorded damage stage depend on scheduler timing.
    """
    if expected_count <= 0:
        raise ValueError("expected controlled attack count must be positive")
    local_deadline = min(deadline_ns, time.perf_counter_ns() + 2_000_000_000)
    while time.perf_counter_ns() < local_deadline:
        attacks = tuple(
            row for row in _fixture_events_for_trial(path, trial_id)
            if row.get("event") == "controlled_attack"
        )
        if len(attacks) >= expected_count:
            return attacks[expected_count - 1]
        time.sleep(.005)
    return None


def _append_motion_observation(observations: list[dict], snapshot: object) -> None:
    """Keep the compact causal samples ordered even when a child tick advances twice."""
    own = snapshot.self_state.value
    fact = {
        "sequence_id": snapshot.sequence_id,
        "movement_tick_id": own.movement_tick_id,
        "hurt": own.hurt_animation_ticks,
        "health": own.health_points,
    }
    if any(row["sequence_id"] == fact["sequence_id"] for row in observations):
        return
    observations.append(fact)
    observations.sort(key=lambda row: row["sequence_id"])


def _cleanup_trial(
    driver: MovingMeleeDriver,
    profile: BehaviorProfileV0,
    trial: dict,
    fixture_writer: Callable[[tuple[str, ...], dict], None],
) -> None:
    """Release the movement owner and retire the server fixture for every trial."""
    try:
        if not driver.report.terminal:
            driver.cancel(profile, "c1c_trial_end")
    finally:
        fixture_writer((f"mc2p_c1_remove {trial['trial_id']}",), trial)


def _run_negative(
    runtime: PlayerRuntimeV1,
    driver: MovingMeleeDriver,
    trial: dict,
    profile: BehaviorProfileV0,
    deadline_ns: int,
    fixture_writer: Callable[[tuple[str, ...], dict], None],
) -> tuple[bool, str]:
    injection = trial["injection"]
    if injection == "route_deviation_without_damage":
        detector = DamageKnockbackDetector()
        detector.observe(runtime.observation)
        driver.tick(profile, min(deadline_ns, time.perf_counter_ns() + 3_000_000_000))
        found = detector.observe(runtime.observation)
        passed = found.event is None and driver.report.external_motion_events == 0
        driver.cancel(profile, "negative_route_deviation_complete")
        return passed, found.reason
    if injection == "missing_or_regressed_hurt_evidence":
        own = runtime.observation.self_state.value
        missing = replace(
            runtime.observation,
            self_state=replace(
                runtime.observation.self_state,
                value=replace(own, hurt_animation_ticks=None, movement_tick_id=None),
            ),
        )
        result = DamageKnockbackDetector().observe(missing)
        passed = result.event is None and result.reason == "motion_evidence_unavailable"
        driver.cancel(profile, "negative_missing_evidence_complete")
        return passed, result.reason

    fixture_writer((
        f"mc2p_c1_strike {trial['trial_id']} negative_setup",
    ), trial)
    old_approach = driver.approach_driver
    reached = _tick_until(
        driver, profile, deadline_ns,
        lambda: driver.recovery_driver is not None,
    )
    if not reached or driver.recovery_driver is None:
        return False, "recovery_not_reached"
    recovery = driver.recovery_driver
    if injection == "late_old_plan":
        if old_approach is not None:
            old_approach.state = "success"
        driver.tick(profile, min(deadline_ns, time.perf_counter_ns() + 3_000_000_000))
        passed = driver.recovery_driver is recovery \
            and driver.report.state == "recovering_external_motion"
        driver.cancel(profile, "negative_late_plan_complete")
        return passed, "old_plan_rejected" if passed else "old_plan_took_over"
    if injection == "cancel_during_recovery":
        driver.cancel(profile, "negative_cancel_during_recovery")
        return driver.report.state == "cancelled", driver.report.reason
    if injection == "target_revision_during_recovery":
        revised = replace(driver._target, revision=driver.report.target_revision + 1)
        driver.replace_target(revised, time.perf_counter_ns())
        passed = driver.recovery_driver is recovery \
            and driver.report.target_revision == revised.revision
        driver.cancel(profile, "negative_revision_complete")
        return passed, driver.report.reason
    if injection == "world_change_during_recovery":
        changed = replace(runtime.observation, episode_id=runtime.observation.episode_id + "-other")
        rejected = False
        try:
            recovery.controller.decide(changed)
        except ContractViolation:
            rejected = True
        driver.cancel(profile, "negative_world_change_complete")
        return rejected, "world_session_changed" if rejected else "world_change_accepted"
    if injection == "target_death_during_recovery":
        fixture_writer((
            f"kill @e[tag=mc2p-c1-fixture-{trial['trial_id']}]",
        ), trial)
        passed = _tick_until(
            driver, profile, deadline_ns,
            lambda: driver.report.terminal,
        ) and driver.report.reason == "target_dead"
        return passed, driver.report.reason
    if injection == "recovery_tick_budget_exhausted":
        own = runtime.observation.self_state.value
        first_tick = recovery.controller._first_tick
        forced = replace(
            runtime.observation,
            self_state=replace(
                runtime.observation.self_state,
                value=replace(own, movement_tick_id=first_tick + 41),
            ),
        )
        decision = recovery.controller.decide(forced)
        passed = decision.directive is RecoveryDirective.EXHAUSTED \
            and decision.reason == "recovery_tick_budget_exhausted"
        driver.cancel(profile, "negative_tick_budget_complete")
        return passed, decision.reason
    if injection == "fifth_damage_event":
        event = recovery._event
        # This is a bounded component-injection negative, not a claim that
        # four additional server damage events were observed. Keep synthetic
        # generations out of the formal recovery driver's event trace.
        injected_controller = ExternalMotionRecoveryController()
        injected_controller.start(event)
        for generation in range(event.generation + 1, event.generation + 5):
            event = replace(
                event,
                event_id=f"{event.episode_id}/damage-knockback/{generation}",
                generation=generation,
                observation_sequence_id=event.observation_sequence_id + 1,
                movement_tick_id=event.movement_tick_id + 1,
            )
            injected_controller.observe_event(event)
        own = runtime.observation.self_state.value
        forced = replace(
            runtime.observation,
            sequence_id=max(runtime.observation.sequence_id, event.observation_sequence_id),
            self_state=replace(
                runtime.observation.self_state,
                value=replace(own, movement_tick_id=event.movement_tick_id),
            ),
        )
        decision = injected_controller.decide(forced)
        passed = decision.directive is RecoveryDirective.NEUTRAL_AIR \
            and injected_controller.task_limit_reason == "event_budget_exhausted"
        driver.cancel(profile, "negative_event_budget_complete")
        return passed, injected_controller.task_limit_reason or decision.reason
    if injection == "duplicate_attack_after_damage":
        before = driver.report.attack_submissions
        driver.tick(profile, min(deadline_ns, time.perf_counter_ns() + 3_000_000_000))
        passed = driver.report.attack_submissions == before
        driver.cancel(profile, "negative_duplicate_attack_complete")
        return passed, "submitted_attack_not_repeated" if passed else "attack_repeated"
    driver.cancel(profile, "unsupported_negative")
    return False, "unsupported_negative"


def run_c1_external_motion_runtime(
    runtime: PlayerRuntimeV1,
    episode: str,
    directory: Path,
    deadline_ns: int,
    *,
    world_seed: int,
    code_hashes: dict[str, str],
    fixture_writer: Callable[[tuple[str, ...], dict], None],
    fixture_events: Path,
) -> tuple[dict, list[dict], list[dict]]:
    """Run the frozen real-damage batch.

    Negative fault injection is wired by the deployment probe in Task 7.  This
    runner still records every predeclared negative in the denominator instead
    of silently reclassifying an unsupported or timed-out case.
    """
    if type(runtime) is not PlayerRuntimeV1 or runtime.state is not RuntimeStateV1.READY:
        raise ValueError("C1-C runtime requires ready PlayerRuntimeV1")
    directory.mkdir(parents=True, exist_ok=True)
    trials = c1c_trial_plan(world_seed)
    manifest = directory / "c1-external-motion-manifest.json"
    write_json_atomic(manifest, {
        "schema_version": "mc2p.c1-external-motion-manifest.v1",
        "world_seed": world_seed,
        "code_hashes": dict(sorted(code_hashes.items())),
        "trials": list(trials),
    })
    rows: list[dict] = []
    profiles = NavigationSessionProfiles.load(CONFIG)
    for trial in trials:
        fixture_writer(_fixture_commands(trial), trial)
        receipt = _await_seed_receipt(fixture_events, trial, deadline_ns)
        fixture_valid = validate_c1c_seed_receipt(trial, receipt)
        if not fixture_valid:
            row = {
                "schema_version": "mc2p.c1-external-motion-trial.v1",
                "trial_id": trial["trial_id"],
                "classification": trial["classification"],
                "fixture_valid": False,
                "passed": False,
                "terminal_state": "fixture_invalid",
                "seed_receipt": receipt,
            }
            rows.append(row)
            append_jsonl(directory / "c1-external-motion-trials.jsonl", row)
            continue
        entity = _refresh_target(runtime, trial, deadline_ns)
        _await_target_motion(runtime, trial, entity.track_id, deadline_ns)
        target = CombatTargetV1(
            "c1c-task-" + trial["trial_id"],
            "c1c-goal-" + trial["trial_id"],
            1, episode, entity.track_id,
        )
        driver = MovingMeleeDriver(
            runtime,
            NavigationSession(
                "c1c-" + trial["trial_id"], profiles,
                observation_adapter=runtime.navigation_observation_adapter,
            ),
        )
        driver.start(target, time.perf_counter_ns())
        profile = BehaviorProfileV0()
        if trial["classification"] == "negative":
            try:
                passed, reason = _run_negative(
                    runtime, driver, trial, profile, deadline_ns, fixture_writer,
                )
                row = {
                    "schema_version": "mc2p.c1-external-motion-trial.v1",
                    "trial_id": trial["trial_id"],
                    "classification": "negative",
                    "injection": trial["injection"],
                    "fixture_valid": True,
                    "seed_receipt": receipt,
                    "terminal_state": "complete" if passed else "failed",
                    "report": asdict(driver.report),
                    "failures": [] if passed else [reason],
                    "passed": passed,
                }
            finally:
                _cleanup_trial(driver, profile, trial, fixture_writer)
                driver.navigation_session.close()
            rows.append(row)
            append_jsonl(directory / "c1-external-motion-trials.jsonl", row)
            continue
        initial_health = runtime.observation.self_state.value.health_points
        observed_stage = None
        first_attack_count = 0
        damage_transitions = 0
        previous_hurt = runtime.observation.self_state.value.hurt_animation_ticks or 0
        max_recovery_ticks = 0
        max_recovery_stable_ticks = 0
        maximum_parallel_owners = 0
        stale_route_takeovers = 0
        retired_movement_sources: set[str] = set()
        observed_reanchors = 0
        external_events_before = 0
        controlled_attack_requests = 0
        attacks_at_recovery_start = None
        duplicate_attacks = 0
        control_decision_ms: list[float] = []
        backend_local_ms: list[float] = []
        driver_outside_backend_ms: list[float] = []
        recovery_response_ms: list[float] = []
        pending_recovery_client_ns = None
        observations = []
        external_events = []
        _append_motion_observation(observations, runtime.observation)
        for _ in range(1200):
            if driver.report.terminal or time.perf_counter_ns() >= deadline_ns:
                break
            own_before = runtime.observation.self_state.value
            report_before = driver.report
            if controlled_attack_due(
                trial["damage_stage"], report_before, own_before,
                controlled_attack_requests,
            ):
                controlled_attack_requests += 1
                fixture_writer((
                    f"mc2p_c1_strike {trial['trial_id']} "
                    f"{trial['damage_stage']}_{controlled_attack_requests}",
                ), trial)
                applied_attack = _await_controlled_attack(
                    fixture_events, trial["trial_id"],
                    controlled_attack_requests, deadline_ns,
                )
                if applied_attack is not None and applied_attack.get("success") is True:
                    if (trial["damage_stage"] == "post_recovery_rehit"
                            and controlled_attack_requests >= 2):
                        observed_stage = "post_recovery_rehit"
                    elif observed_stage is None:
                        # No driver frame ran between the stage predicate and
                        # the confirmed server-side hit, so this is the actual
                        # controller stage in which damage was injected.
                        observed_stage = trial["damage_stage"]
            phase_before = driver.report.state
            attacks_before = driver.report.attack_submissions
            recoveries_before = driver.report.external_recoveries_completed
            approach_source_before = (
                None if driver.approach_driver is None
                else driver.approach_driver.source.source_id
            )
            started = time.perf_counter_ns()
            blocking_started = runtime.backend_blocking_io_ns_total
            backend_started = runtime.backend_elapsed_ns_total
            result = driver.tick(profile, min(deadline_ns, started + 3_000_000_000))
            wall_elapsed_ns = time.perf_counter_ns() - started
            backend_elapsed_ns = runtime.backend_elapsed_ns_total - backend_started
            blocking_elapsed_ns = (
                runtime.backend_blocking_io_ns_total - blocking_started
            )
            control_decision_ms.append(
                max(0, wall_elapsed_ns - blocking_elapsed_ns) / 1_000_000
            )
            backend_local_ms.append(
                max(0, backend_elapsed_ns - blocking_elapsed_ns) / 1_000_000
            )
            driver_outside_backend_ms.append(
                max(0, wall_elapsed_ns - backend_elapsed_ns) / 1_000_000
            )
            selected_movement = (
                None if result is None or result.decision is None
                else dict(result.decision.selected_intents).get("movement")
            )
            selected_route_was_retired = selected_retired_route(
                selected_movement, retired_movement_sources,
            )
            if selected_route_was_retired:
                stale_route_takeovers += 1
            # The returned decision was selected before its observation could
            # reveal damage. Retire that source only for later frames.
            if (approach_source_before is not None
                    and driver.report.state == "recovering_external_motion"):
                retired_movement_sources.add(approach_source_before)
            own = runtime.observation.self_state.value
            _append_motion_observation(observations, runtime.observation)
            hurt = own.hurt_animation_ticks or 0
            if previous_hurt == 0 and hurt > 0:
                damage_transitions += 1
                if (observed_stage is None
                        and trial["damage_stage"] == "post_recovery_rehit"
                        and damage_transitions >= 2
                        and driver.report.external_recoveries_completed >= 1):
                    observed_stage = "post_recovery_rehit"
                elif observed_stage is None:
                    if phase_before == "pursuing":
                        observed_stage = "pursuing"
                    elif attacks_before > 0:
                        observed_stage = "attack_submitted"
                    else:
                        observed_stage = "aim_or_cooldown"
            previous_hurt = hurt
            first_attack_count = max(first_attack_count, driver.report.attack_submissions)
            if driver.report.external_motion_events > external_events_before:
                event_observation = driver.last_external_motion_observation
                pending_recovery_client_ns = (
                    None if event_observation is None
                    else event_observation.client_sample.completed_at_monotonic_ns
                )
                event = None if driver.recovery_driver is None else driver.recovery_driver._event
                if event_observation is not None:
                    _append_motion_observation(observations, event_observation)
                if event is not None:
                    external_events.append({
                        "event_id": event.event_id,
                        "generation": event.generation,
                        "observation_sequence_id": event.observation_sequence_id,
                        "movement_tick_id": event.movement_tick_id,
                        "target_track_id": target.track_id,
                        "target_revision": target.revision,
                    })
                attacks_at_recovery_start = driver.report.attack_submissions
                external_events_before = driver.report.external_motion_events
                # Releasing the disrupted route submits a neutral movement
                # snapshot before the dedicated recovery source takes over.
                # It is already the first recovery-safe input, so measure its
                # real client application instead of waiting one more tick for
                # a source-id change.
                if (
                    pending_recovery_client_ns is not None
                    and result is not None
                    and result.decision is not None
                    and result.decision.action.movement == MovementV1()
                    and not selected_route_was_retired
                ):
                    latency_ms = recovery_application_latency_ms(
                        pending_recovery_client_ns, result,
                    )
                    if latency_ms is not None:
                        recovery_response_ms.append(latency_ms)
                        pending_recovery_client_ns = None
            if driver.report.external_recoveries_completed > recoveries_before:
                observed_reanchors += (
                    driver.report.external_recoveries_completed - recoveries_before
                )
            if pending_recovery_client_ns is not None and selected_movement is not None:
                recovery = driver.recovery_driver
                recovery_sources = set()
                if recovery is not None:
                    if recovery.source is not None:
                        recovery_sources.add(recovery.source.source_id)
                if any(selected_movement.startswith(source_id + "/")
                       for source_id in recovery_sources):
                    latency_ms = recovery_application_latency_ms(
                        pending_recovery_client_ns, result,
                    )
                    if latency_ms is not None:
                        recovery_response_ms.append(latency_ms)
                        pending_recovery_client_ns = None
            movement_owners = int(driver.approach_driver is not None) \
                + int(driver.recovery_driver is not None)
            maximum_parallel_owners = max(maximum_parallel_owners, movement_owners)
            if driver.recovery_driver is not None:
                if (attacks_at_recovery_start is not None
                        and driver.report.attack_submissions > attacks_at_recovery_start):
                    duplicate_attacks += (
                        driver.report.attack_submissions - attacks_at_recovery_start
                    )
                    attacks_at_recovery_start = driver.report.attack_submissions
                max_recovery_ticks = max(
                    max_recovery_ticks,
                    driver.recovery_driver.report.elapsed_ticks,
                )
                max_recovery_stable_ticks = max(
                    max_recovery_stable_ticks,
                    driver.recovery_driver.report.stable_ticks,
                )
            # A fast poll is not a control attempt and must not burn the
            # bounded cycle allowance before the next eligible frame.
            wait_after_fast_poll(result)
        report = driver.report
        _cleanup_trial(driver, profile, trial, fixture_writer)
        driver.navigation_session.close()
        fixture_rows = _fixture_events_for_trial(fixture_events, trial["trial_id"])
        attack_rows = tuple(
            row for row in fixture_rows if row.get("event") == "controlled_attack"
        )
        damage_rows = tuple(
            row for row in fixture_rows if row.get("event") == "damage_allowed"
        )
        evidence = {
            "fixture_valid": True,
            "controlled_attack_requests": controlled_attack_requests,
            "controlled_attack_successes": sum(
                row.get("success") is True for row in attack_rows
            ),
            "real_damage_events": len(damage_rows),
            "observed_damage_stage": observed_stage,
            "damage_transitions": damage_transitions,
            "external_motion_events": report.external_motion_events,
            "external_recoveries_completed": report.external_recoveries_completed,
            "maximum_recovery_elapsed_ticks": max_recovery_ticks,
            "parallel_recovery_owners": max(0, maximum_parallel_owners - 1),
            "stale_route_takeovers": stale_route_takeovers,
            "duplicate_attacks": duplicate_attacks,
            "reanchors": observed_reanchors,
            "explicit_death": report.reason == "target_dead",
            "terminal_state": report.state,
        }
        failures = evaluate_c1c_positive(trial, evidence)
        row = {
            "schema_version": "mc2p.c1-external-motion-trial.v1",
            "trial_id": trial["trial_id"],
            "classification": trial["classification"],
            "injection": trial["injection"],
            "fixture_valid": True,
            "seed_receipt": receipt,
            "report": asdict(report),
            "initial_health": initial_health,
            "control_decision_ms": control_decision_ms,
            "backend_local_ms": backend_local_ms,
            "driver_outside_backend_ms": driver_outside_backend_ms,
            "recovery_response_ms": recovery_response_ms,
            **evidence,
            "failures": list(failures),
            "passed": not failures,
        }
        rows.append(row)
        append_jsonl(directory / "c1-external-motion-trials.jsonl", row)
    summary = summarize_c1c_trials(rows)
    write_json_atomic(directory / "c1-external-motion-summary.json", summary)
    checks = [
        {"name": "c1c_20_of_20_positive_tasks",
         "passed": summary["positive_passed"] == summary["positive_total"] == 20},
        {"name": "c1c_ten_bounded_negatives",
         "passed": summary["negative_passed"] == summary["negative_total"] == 10},
        {"name": "c1c_fixture_receipts_valid", "passed": summary["fixture_invalid"] == 0},
        {"name": "c1c_control_frame_budget", "passed":
         summary["control_decision_ms"]["p95"] is not None
         and summary["control_decision_ms"]["p95"] <= 8.0
         and summary["control_decision_ms"]["p99"] <= 15.0},
        {"name": "c1c_recovery_input_application_budget", "passed":
         summary["recovery_response_ms"]["p99"] is not None
         and summary["recovery_response_ms"]["p99"] <= 100.0},
    ]
    return {"manifest": manifest.name, "summary": summary, "trials": rows}, rows, checks
