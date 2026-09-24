"""Frozen real-Fabric C1-A trials; fixture commands never enter actor inputs."""
from __future__ import annotations

from dataclasses import asdict
import json
import math
from pathlib import Path
import time
from typing import Callable

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, AttackEntityV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import FieldStatusV0
from mc2p.contracts.intent_source import OrderedIntentV1, ordered_intent_id
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.task import ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1, RuntimeStateV1
from mc2p.skills.fixed_melee import CombatTargetV1
from mc2p.skills.fixed_melee_driver import FixedMeleeDriver, FixedMeleeReportV1
from mc2p.skills.navigation_state import NavigationState
from mc2p.skills.normal_control_capabilities import ControlCapabilities, SCHEMA
from mc2p.skills.point_goal_policy import PointGoalPolicy
from scripts.control_probe_core import append_jsonl, write_json_atomic


TARGET = {"x": 0.5, "y": 100.0, "z": 0.5}
ROOT = Path(__file__).resolve().parents[1]
CONTROL_CAPABILITIES = ROOT / "artifacts/normal-navigation/control-capability-v2.json"
_DIRECTIONS = {
    "north": ({"x": 0.5, "y": 100.0, "z": -3.0}, 0.0),
    "east": ({"x": 4.0, "y": 100.0, "z": 0.5}, 90.0),
    "south": ({"x": 0.5, "y": 100.0, "z": 4.0}, 180.0),
    "west": ({"x": -3.0, "y": 100.0, "z": 0.5}, -90.0),
}
_NEGATIVES = (
    "approach_cancel", "aim_cancel", "after_submit_cancel", "target_revision",
    "wrong_crosshair", "known_wall", "confirmation_timeout",
)


def c1_acceptance_control_capabilities() -> ControlCapabilities:
    """Open the old sealed input set only inside this controlled batch.

    The historical J3 artifact cannot certify today's body after unrelated
    runtime additions, so C1 does not publish the resulting object as a
    general navigation capability.  The frozen run records which controls are
    actually selected; that evidence can then support a smaller current gate.
    """
    document = json.loads(CONTROL_CAPABILITIES.read_text("utf-8"))
    published = tuple(tuple(item) for item in document.get("allowed_controls", ()))
    if document.get("schema_version") != SCHEMA or not published:
        raise ValueError("C1 acceptance controls are absent from the sealed J3 profile")
    look_rate = document.get("measurements", {}).get("look_rate_degrees_per_second")
    return ControlCapabilities(
        published,
        (),
        ROOT,
        measured_look_degrees_per_second=look_rate,
    )


def _trial(*, trial_id: str, classification: str, direction: str, repeat: int,
           scenario_seed: int, injection: str | None) -> dict:
    start, yaw = _DIRECTIONS[direction]
    if injection == "aim_cancel":
        yaw = (yaw + 45.0 + 180.0) % 360.0 - 180.0
    if injection in {"aim_cancel", "after_submit_cancel", "wrong_crosshair", "known_wall"}:
        start = {"x": 0.5, "y": 100.0, "z": -2.5}
    return {
        "trial_id": trial_id,
        "classification": classification,
        "direction": direction,
        "repeat": repeat,
        "scenario_seed": scenario_seed,
        "start_position": dict(start),
        "target_position": dict(TARGET),
        "yaw_degrees": yaw,
        "pitch_degrees": 0.0,
        "equipment": "minecraft:stone_sword",
        "confirmation_timeout_ns": 1_000_000_000,
        "injection": injection,
    }


def c1_trial_plan(world_seed: int) -> tuple[dict, ...]:
    if type(world_seed) is not int:
        raise ValueError("C1 world seed must be an integer")
    rows = []
    ordinal = 0
    for direction in ("north", "east", "south", "west"):
        for repeat in range(10):
            ordinal += 1
            rows.append(_trial(
                trial_id=f"positive-{direction}-{repeat + 1:02d}",
                classification="positive", direction=direction, repeat=repeat + 1,
                scenario_seed=world_seed * 100 + ordinal, injection=None,
            ))
    for injection in _NEGATIVES:
        ordinal += 1
        rows.append(_trial(
            trial_id="negative-" + injection,
            classification="negative", direction="north", repeat=1,
            scenario_seed=world_seed * 100 + ordinal, injection=injection,
        ))
    return tuple(rows)


def write_c1_manifest(directory: Path, *, world_seed: int,
                      code_hashes: dict[str, str]) -> Path:
    if type(code_hashes) is not dict or any(type(k) is not str or type(v) is not str
                                            for k, v in code_hashes.items()):
        raise ValueError("C1 code hashes must be a string mapping")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "c1-fixed-melee-manifest.json"
    write_json_atomic(path, {
        "schema_version": "mc2p.c1-fixed-melee-manifest.v1",
        "world_seed": world_seed,
        "code_hashes": dict(sorted(code_hashes.items())),
        "trials": list(c1_trial_plan(world_seed)),
    })
    return path


def _fixture_commands(trial: dict) -> tuple[str, ...]:
    start, target = trial["start_position"], trial["target_position"]
    invulnerable = ",Invulnerable:1b" if trial["injection"] == "confirmation_timeout" else ""
    commands = [
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
        (f"tp MC2PProbe {start['x']:.1f} {start['y']:.1f} {start['z']:.1f} "
         f"{trial['yaw_degrees']:.1f} {trial['pitch_degrees']:.1f}"),
        (f"summon minecraft:zombie {target['x']:.1f} {target['y']:.1f} {target['z']:.1f} "
         "{NoAI:1b,PersistenceRequired:1b,Silent:1b,Tags:[\"mc2p_c1\"]"
         + invulnerable + "}"),
    ]
    if trial["injection"] == "wrong_crosshair":
        commands.append("summon minecraft:armor_stand 0.5 100 -1.0 {NoGravity:1b,Tags:[\"mc2p_c1_decoy\"]}")
    return tuple(commands)


def _observation_task(trial_id: str, deadline_ns: int) -> TaskIntentV0:
    return TaskIntentV0(
        "c1-observe-" + trial_id,
        "c1_fixture_observation",
        "{}",
        (SuccessCriterionV0(
            "formal_observation_received", ComparisonOperatorV0.GREATER_THAN, 0, "frames",
        ),),
        100, deadline_ns, True, 0.0,
    )


def _zombies(runtime: PlayerRuntimeV1):
    observation = runtime.observation
    if (observation is None or observation.perception.status is not FieldStatusV0.VALID
            or observation.perception.value is None):
        return ()
    return tuple(entity for entity in observation.perception.value.visible_entities
                 if entity.entity_type == "minecraft:zombie")


def _refresh_navigation(runtime: PlayerRuntimeV1, trial: dict,
                        deadline_ns: int) -> object:
    task = _observation_task(trial["trial_id"], deadline_ns)
    profile = BehaviorProfileV0()
    for _ in range(40):
        now = time.perf_counter_ns()
        result = runtime.step(
            task, profile, min(deadline_ns, now + 500_000_000),
            observation_request=ObservationRequestV3("navigation_v1"),
        )
        if result.report.failure is not None:
            raise RuntimeError("C1 fixture observation failed: " + result.report.failure.reason)
        zombies = _zombies(runtime)
        observation = runtime.observation
        own = (None if observation is None
               or observation.self_state.status is not FieldStatusV0.VALID
               else observation.self_state.value)
        if (own is not None and own.is_on_ground
                and len(zombies) == 1 and zombies[0].hurt_animation_ticks == 0):
            candidate = zombies[0]
            # Entity removal and spawn packets can cross at a trial boundary.
            # Bind the visible identity to a fresh loaded/living fact before
            # allowing it to become the formal combat target.
            check_now = time.perf_counter_ns()
            checked = runtime.step(
                task, profile, min(deadline_ns, check_now + 500_000_000),
                observation_request=ObservationRequestV3(
                    "navigation_v1", entity_track_id=candidate.track_id,
                ),
            )
            if checked.report.failure is not None:
                raise RuntimeError(
                    "C1 fixture identity check failed: " + checked.report.failure.reason
                )
            tracked = runtime.observation.tracked_entity.value
            refreshed = _zombies(runtime)
            if (tracked is not None and tracked.track_id == candidate.track_id
                    and not tracked.is_dead and tracked.health_points is not None
                    and tracked.health_points > 0
                    and any(entity.track_id == candidate.track_id for entity in refreshed)):
                return next(entity for entity in refreshed
                            if entity.track_id == candidate.track_id)
    raise RuntimeError("C1 fixture did not yield one clean visible zombie")


def _run_driver(runtime: PlayerRuntimeV1, trial: dict, target: CombatTargetV1,
                deadline_ns: int, control_capabilities) -> FixedMeleeDriver:
    driver = FixedMeleeDriver(
        runtime,
        NavigationState("c1-" + trial["trial_id"]),
        PointGoalPolicy("D", control_capabilities=control_capabilities),
    )
    driver.start(target, time.perf_counter_ns())
    profile = BehaviorProfileV0()
    injection = trial["injection"]
    if injection == "approach_cancel":
        driver.cancel(profile, "negative_approach_cancel")
        return driver
    for cycle in range(700):
        if time.perf_counter_ns() >= deadline_ns:
            raise TimeoutError("C1 batch deadline expired")
        if injection == "aim_cancel" and driver.report.state == "aligning":
            driver.cancel(profile, "negative_aim_cancel")
            return driver
        if injection == "after_submit_cancel" and driver.report.attack_submitted:
            driver.cancel(profile, "negative_after_submit_cancel")
            return driver
        if injection == "target_revision" and cycle == 1 and not driver.report.attack_submitted:
            driver.replace_target(CombatTargetV1(
                target.task_id, target.goal_id, 2, target.episode_id, target.track_id,
            ), time.perf_counter_ns())
            driver.cancel(profile, "negative_target_revision")
            return driver
        if injection in {"wrong_crosshair", "known_wall"} and cycle == 20:
            driver.cancel(profile, "negative_" + injection)
            return driver
        if driver.report.terminal:
            return driver
        now = time.perf_counter_ns()
        result = driver.tick(profile, min(deadline_ns, now + 3_000_000_000))
        if result is None:
            # PointGoalDriver is paced at 20 Hz.  A fast acceptance loop must
            # not turn "not due yet" into hundreds of attempts or lease logs.
            time.sleep(0.01)
    raise TimeoutError("C1 trial exceeded bounded control cycles")


def _run_guard_negative(runtime: PlayerRuntimeV1, trial: dict, target: CombatTargetV1,
                        deadline_ns: int) -> tuple[FixedMeleeReportV1, dict]:
    """Submit one deliberately invalid target operation through the real client guard."""
    task = _observation_task(trial["trial_id"] + "-guard", deadline_ns)
    profile = BehaviorProfileV0()
    targeting = None
    for _ in range(20):
        now = time.perf_counter_ns()
        if now >= deadline_ns:
            raise TimeoutError("C1 guard observation deadline expired")
        observed = runtime.step(
            task, profile, min(deadline_ns, now + 500_000_000),
            observation_request=ObservationRequestV3("interaction_v1"),
        )
        if observed.report.failure is not None:
            raise RuntimeError("C1 guard observation failed: " + observed.report.failure.reason)
        observation = runtime.observation
        targeting = None if observation is None else observation.targeting.value
        if _guard_target_ready(trial["injection"], targeting, target.track_id):
            break
    else:
        raise RuntimeError("C1 guard fixture did not reach its declared crosshair state")
    source = runtime.register_ordered_source("c1-guard-" + trial["injection"])
    try:
        now = time.perf_counter_ns()
        intent_id = ordered_intent_id(source, 1)
        runtime.submit_ordered_intent(OrderedIntentV1(
            source,
            1,
            ActionIntentV1(
                intent_id,
                source.source_id,
                target.episode_id,
                observation.sequence_id,
                ActionPriorityV0.TASK,
                now,
                min(deadline_ns, now + 250_000_000),
                operation=AttackEntityV1(target.track_id),
            ),
        ))
        result = runtime.step(
            task, profile, min(deadline_ns, now + 500_000_000),
            observation_request=ObservationRequestV3("interaction_v1"),
        )
    finally:
        if runtime.state is RuntimeStateV1.READY:
            runtime.cancel_source(source.source_id)
            runtime.unregister_ordered_source(source)
    receipt = None if result.backend_result is None else result.backend_result.receipt
    selected = {} if result.decision is None else dict(result.decision.selected_intents)
    reason = "missing_receipt" if receipt is None else receipt.reason
    evidence = {
        "selected_by_arbiter": selected.get("operation") == intent_id,
        "receipt_status": None if receipt is None else receipt.status,
        "receipt_reason": reason,
        "targeting_hit_kind": None if targeting is None else targeting.hit_kind,
        "targeting_entity_ref": None if targeting is None else targeting.entity_ref,
    }
    return FixedMeleeReportV1(
        "failed", "client_rejected/" + reason, target.revision,
        False, False, 1, True,
    ), evidence


def _guard_target_ready(injection: str, targeting, target_track_id: str) -> bool:
    if targeting is None:
        return False
    if injection == "wrong_crosshair":
        return targeting.hit_kind == "entity" and targeting.entity_ref != target_track_id
    if injection == "known_wall":
        return targeting.hit_kind == "block"
    return False


def _negative_passed(trial: dict, report, evidence: dict | None = None) -> bool:
    injection = trial["injection"]
    if injection in {"approach_cancel", "aim_cancel", "target_revision"}:
        return (report.state == "cancelled" and report.attack_submissions == 0)
    if injection in {"wrong_crosshair", "known_wall"}:
        expected = ("entity", "wrong_entity_target") if injection == "wrong_crosshair" \
            else ("block", "target_miss")
        return bool(
            evidence is not None
            and evidence.get("selected_by_arbiter") is True
            and evidence.get("receipt_status") == "rejected"
            and (evidence.get("targeting_hit_kind"), evidence.get("receipt_reason")) == expected
            and report.state == "failed"
            and report.reason == "client_rejected/" + expected[1]
            and report.attack_submissions == 1
            and not report.attack_submitted
            and not report.hit_observed
        )
    if injection == "after_submit_cancel":
        return (report.state == "cancelled" and report.reason == "cancelled_after_submit"
                and report.attack_submissions == 1)
    if injection == "confirmation_timeout":
        return (report.state == "failed" and report.attack_submissions == 1
                and not report.hit_observed)
    return False


def run_c1_fixed_melee_runtime(
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
        raise ValueError("C1 runtime requires ready PlayerRuntimeV1")
    manifest = write_c1_manifest(directory, world_seed=world_seed, code_hashes=code_hashes)
    rows = []
    control_capabilities = c1_acceptance_control_capabilities()
    for trial in c1_trial_plan(world_seed):
        fixture_writer(_fixture_commands(trial), trial)
        entity = _refresh_navigation(runtime, trial, deadline_ns)
        if entity.hurt_animation_ticks != 0:
            raise RuntimeError("C1 target had pre-attack hurt animation")
        target = CombatTargetV1(
            "c1-task-" + trial["trial_id"],
            "c1-goal-" + trial["trial_id"],
            1, episode, entity.track_id,
        )
        negative_evidence = None
        if trial["injection"] == "known_wall":
            fixture_writer(("fill 0 100 -1 0 102 -1 minecraft:stone replace",), trial)
        if trial["injection"] in {"wrong_crosshair", "known_wall"}:
            report, negative_evidence = _run_guard_negative(
                runtime, trial, target, deadline_ns,
            )
        else:
            driver = _run_driver(runtime, trial, target, deadline_ns, control_capabilities)
            report = driver.report
        passed = ((trial["classification"] == "positive"
                   and report.state == "complete"
                   and report.reason == "hit_confirmed"
                   and report.attack_submissions == 1)
                  or (trial["classification"] == "negative"
                      and _negative_passed(trial, report, negative_evidence)))
        row = {
            "schema_version": "mc2p.c1-fixed-melee-trial.v1",
            "trial_id": trial["trial_id"],
            "classification": trial["classification"],
            "injection": trial["injection"],
            "target_track_id": entity.track_id,
            "pre_attack_hurt_animation_ticks": 0,
            "report": asdict(report),
            "negative_evidence": negative_evidence,
            "passed": passed,
        }
        rows.append(row)
        append_jsonl(directory / "c1-fixed-melee-trials.jsonl", row)
    positives = [row for row in rows if row["classification"] == "positive"]
    negatives = [row for row in rows if row["classification"] == "negative"]
    summary = {
        "schema_version": "mc2p.c1-fixed-melee-summary.v1",
        "manifest": manifest.name,
        "planned_trials": 47,
        "completed_trials": len(rows),
        "positive_passed": sum(row["passed"] for row in positives),
        "positive_total": len(positives),
        "negative_passed": sum(row["passed"] for row in negatives),
        "negative_total": len(negatives),
    }
    write_json_atomic(directory / "c1-fixed-melee-summary.json", summary)
    checks = [
        {"name": "c1_40_of_40_positive_hits", "passed": summary["positive_passed"] == 40},
        {"name": "c1_seven_bounded_negatives", "passed": summary["negative_passed"] == 7},
        {"name": "c1_run_list_not_reclassified", "passed": len(rows) == 47},
    ]
    return {"manifest": manifest.name, "summary": summary, "trials": rows}, rows, checks
