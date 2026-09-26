"""Run the B12-A negative cases that require real Minecraft entity facts."""
from __future__ import annotations

from dataclasses import asdict
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1, RuntimeStateV1
from mc2p.skills.attack_evidence import AttackAttemptKeyV1
from mc2p.skills.attack_evidence_replay import replay_attack_attempt
from mc2p.skills.fixed_melee import CombatTargetV1
from mc2p.skills.melee_strike_driver import MeleeStrikeDriver
from scripts.b12_attack_evidence_runtime import (
    b12a_trial_plan, evaluate_b12a_trial,
)
from scripts.c1_fixed_melee_runtime import (
    TARGET, _fixture_commands, _refresh_navigation,
)
from scripts.control_probe_core import write_json_atomic


_FABRIC_ADDITIONS = (
    "confirmation_timeout",
    "health_decline_only",
    "unattributed_death",
    "target_revision_after_submit",
)


def unattributed_death_fixture_commands() -> tuple[str, ...]:
    """Create a death fact without adding Minecraft's damage animation."""
    return (
        "data merge entity @e[type=minecraft:zombie,tag=mc2p_c1,limit=1] {Health:0.0f}",
    )


def b12a_fabric_trial_plan(world_seed: int) -> tuple[dict, ...]:
    """Select the game-dependent additions not already proved by C1 gates."""
    return tuple(
        row for row in b12a_trial_plan(world_seed)
        if row.get("classification") == "negative"
        and row.get("injection") in _FABRIC_ADDITIONS
    )


def _fixture_trial(trial: Mapping) -> dict:
    injection = trial["injection"]
    return {
        **dict(trial),
        "start_position": {"x": 0.5, "y": 100.0, "z": -2.5},
        "target_position": dict(TARGET),
        "yaw_degrees": 0.0,
        "pitch_degrees": 0.0,
        "equipment": "minecraft:stone_sword",
        "confirmation_timeout_ns": 1_000_000_000,
        # Prevent the player's attack from creating the fact that the trial
        # intends to inject independently.
        "injection": (
            "confirmation_timeout"
            if injection in {
                "confirmation_timeout", "health_decline_only",
                "unattributed_death",
            }
            else None
        ),
    }


def _attempt_key_json(key: AttackAttemptKeyV1) -> dict:
    return {
        "episode_id": key.episode_id,
        "task_id": key.task_id,
        "goal_id": key.goal_id,
        "target_revision": key.target_revision,
        "track_id": key.track_id,
        "attempt_sequence": key.attempt_sequence,
    }


def _run_trial(
    runtime: PlayerRuntimeV1,
    episode: str,
    trial: dict,
    deadline_ns: int,
    fixture_writer: Callable[[tuple[str, ...], dict], None],
) -> dict:
    fixture = _fixture_trial(trial)
    fixture_writer(_fixture_commands(fixture), trial)
    entity = _refresh_navigation(runtime, fixture, deadline_ns)
    target = CombatTargetV1(
        "b12a-task-" + trial["trial_id"],
        "b12a-goal-" + trial["trial_id"],
        1,
        episode,
        entity.track_id,
    )
    driver = MeleeStrikeDriver(runtime, clock_ns=time.perf_counter_ns)
    driver.start(target, time.perf_counter_ns())
    profile = BehaviorProfileV0()
    injected = False
    for _ in range(160):
        if time.perf_counter_ns() >= deadline_ns:
            raise TimeoutError("B12-A Fabric batch deadline expired")
        if driver.report.terminal:
            break
        now = time.perf_counter_ns()
        driver.tick(profile, min(deadline_ns, now + 3_000_000_000))
        if driver.report.attack_submitted and not injected:
            injection = trial["injection"]
            if injection == "health_decline_only":
                fixture_writer((
                    "data merge entity @e[type=minecraft:zombie,tag=mc2p_c1,limit=1] {Health:18.0f}",
                ), trial)
            elif injection == "unattributed_death":
                fixture_writer(unattributed_death_fixture_commands(), trial)
            elif injection == "target_revision_after_submit":
                driver.replace_target(CombatTargetV1(
                    target.task_id, target.goal_id, 2,
                    target.episode_id, target.track_id,
                ), time.perf_counter_ns())
            injected = True
        if not driver.report.terminal:
            time.sleep(0.01)
    if not driver.report.terminal:
        raise TimeoutError("B12-A Fabric trial exceeded bounded control cycles")
    attempt = driver.attempt_report
    return {
        "schema_version": "mc2p.b12a-fabric-trial.v1",
        **dict(trial),
        "target_track_id": entity.track_id,
        "attempt_key": _attempt_key_json(attempt.key),
        "attempt_outcome": None if attempt.outcome is None else attempt.outcome.value,
        "evidence_grade": attempt.evidence_grade.value,
        "target_damaged_unattributed": attempt.target_damaged_unattributed,
        "target_dead": attempt.target_dead,
        "driver_report": asdict(driver.report),
        "runtime_state": runtime.state.value,
        "injection_applied": injected or trial["injection"] == "confirmation_timeout",
    }


def evaluate_b12a_fabric_records(
    rows: Iterable[Mapping],
    records: Iterable[Mapping],
) -> tuple[list[dict], list[dict]]:
    records = tuple(records)
    evaluated: list[dict] = []
    checks: list[dict] = []
    for source in rows:
        row = dict(source)
        key = AttackAttemptKeyV1(**row["attempt_key"])
        replayed = replay_attack_attempt(records, key)
        evidence = {
            "fixture_valid": row.get("injection_applied") is True,
            "online_outcomes": [row.get("attempt_outcome")],
            "replayed_outcomes": [
                None if replayed.outcome is None else replayed.outcome.value
            ],
            "task_outcome": None,
            "terminal_state": "complete",
            "attempt_count": 1,
            "runtime_state": row.get("runtime_state"),
            "engagement_grants": 0,
            "unattributed_damage_facts": (
                1 if row.get("target_damaged_unattributed") is True else 0
            ),
        }
        failures = evaluate_b12a_trial(row, evidence)
        row["replayed_outcome"] = evidence["replayed_outcomes"][0]
        row["failures"] = list(failures)
        row["passed"] = not failures
        evaluated.append(row)
        checks.append({
            "name": "b12a_fabric_" + row["trial_id"],
            "passed": not failures,
            "failures": list(failures),
        })
    return evaluated, checks


def run_b12a_fabric_runtime(
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
        raise ValueError("B12-A Fabric runtime requires ready PlayerRuntimeV1")
    trials = b12a_fabric_trial_plan(world_seed)
    manifest = directory / "b12a-fabric-manifest.json"
    write_json_atomic(manifest, {
        "schema_version": "mc2p.b12a-fabric-manifest.v1",
        "world_seed": world_seed,
        "code_hashes": dict(sorted(code_hashes.items())),
        "trials": list(trials),
    })
    rows = [
        _run_trial(runtime, episode, dict(trial), deadline_ns, fixture_writer)
        for trial in trials
    ]
    stages = {
        "schema_version": "mc2p.b12a-fabric-evidence.v1",
        "world_seed": world_seed,
        "manifest": manifest.name,
        "trial_count": len(rows),
        "trials": rows,
    }
    checks = [{
        "name": "b12a_fabric_plan_complete",
        "passed": len(rows) == 8,
    }]
    write_json_atomic(directory / "b12a-fabric-online.json", stages)
    return stages, rows, checks
