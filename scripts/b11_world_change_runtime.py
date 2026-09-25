"""Frozen real-Fabric B11 placement and bounded bridge trials."""
from __future__ import annotations

from dataclasses import asdict, replace
import math
from pathlib import Path
import time
from typing import Callable

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import FieldStatusV0
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.task import (
    ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0,
)
from mc2p.motion_nav.bridge_planner import BridgePlacementPolicy
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles
from mc2p.motion_nav.world_interaction import (
    BlockPlacementTransaction, InteractionKind, PlacementState,
    RequiredInteraction,
)
from mc2p.motion_nav.world_model import Aabb, CellKnowledge
from mc2p.skills.block_placement_driver import RuntimeBlockPlacementDriver
from mc2p.skills.world_change_navigation_driver import RuntimeWorldChangeNavigationDriver
from scripts.control_probe_core import append_jsonl, write_json_atomic


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/motion-navigation"
FEET_Y = 100


def b11_trial_plan() -> tuple[dict, ...]:
    rows: list[dict] = []
    for index in range(20):
        rows.append({
            "trial_id": f"fixed-placement-{index + 1:02d}",
            "kind": "fixed_placement",
            "gap_count": 1,
            "initial_items": 1,
        })
    for gap_count, repeats in ((1, 20), (2, 10), (3, 10)):
        for index in range(repeats):
            rows.append({
                "trial_id": f"bridge-{gap_count}-{index + 1:02d}",
                "kind": "bridge",
                "gap_count": gap_count,
                "initial_items": gap_count,
            })
    return tuple(rows)


def b11_negative_trial_plan() -> tuple[dict, ...]:
    cases = (
        "no_authorization",
        "wrong_item",
        "insufficient_items",
        "misaligned_target",
        "destination_occupied",
        "cancel_before_submit",
        "cancel_after_first_confirmation",
        "confirmation_timeout",
        "goal_revision_changed",
        "goal_revision_at_edge_before_dispatch",
        "goal_revision_after_dispatch",
        "bridge_cell_claimed",
    )
    return tuple(
        {
            "trial_id": f"negative-{case}-{repetition}",
            "kind": "negative",
            "case": case,
            "gap_count": (
                2 if case in {
                    "insufficient_items",
                    "cancel_after_first_confirmation",
                } else 1
            ),
            "initial_items": (
                2 if case in {
                    "cancel_after_first_confirmation",
                    "confirmation_timeout",
                } else 1
            ),
            "item_id": "minecraft:stone" if case == "wrong_item" else "minecraft:dirt",
        }
        for case in cases
        for repetition in range(1, 3)
    )


def _fixture_commands(trial: dict) -> tuple[str, ...]:
    gap_count = trial["gap_count"]
    goal_x = gap_count + 1
    commands = [
        "difficulty peaceful",
        "time set midnight",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "weather clear",
        "kill @e[type=!minecraft:player]",
        "kill @e[type=minecraft:item]",
        "gamemode survival MC2PProbe",
        "clear MC2PProbe",
        f"fill -2 {FEET_Y - 2} -2 {goal_x + 2} {FEET_Y + 3} 2 minecraft:air replace",
        f"setblock 0 {FEET_Y - 1} 0 minecraft:stone replace",
        f"setblock {goal_x} {FEET_Y - 1} 0 minecraft:stone replace",
        ("item replace entity MC2PProbe weapon.mainhand with "
         f"{trial.get('item_id', 'minecraft:dirt')} {trial['initial_items']}"),
        f"tp MC2PProbe 0.5 {FEET_Y:.1f} 0.5 -90.0 28.0",
    ]
    if trial.get("case") == "destination_occupied":
        commands.insert(-2, f"setblock 1 {FEET_Y - 1} 0 minecraft:stone replace")
    return tuple(commands)


def _task(trial_id: str, deadline_ns: int) -> TaskIntentV0:
    return TaskIntentV0(
        "b11-" + trial_id,
        "b11_world_change",
        "{}",
        (SuccessCriterionV0(
            "confirmed_world_change", ComparisonOperatorV0.GREATER_THAN, 0,
            "blocks",
        ),),
        100,
        deadline_ns,
        True,
        0.0,
    )


def _air_positions(gap_count: int) -> tuple[tuple[int, int, int], ...]:
    goal_x = gap_count + 1
    supports = {(0, FEET_Y - 1, 0), (goal_x, FEET_Y - 1, 0)}
    return tuple(sorted(
        (x, y, z)
        for x in range(-1, goal_x + 2)
        for y in range(FEET_Y - 2, FEET_Y + 3)
        for z in range(-1, 2)
        if (x, y, z) not in supports
    ))


def _ready_fixture(runtime, trial: dict, deadline_ns: int,
                   diagnostic: Callable[[], None]):
    request = ObservationRequestV3(
        "interaction_v1", _air_positions(trial["gap_count"]),
    )
    task = _task(trial["trial_id"], deadline_ns)
    profile = BehaviorProfileV0()
    goal_x = trial["gap_count"] + 1
    for _ in range(40):
        now = time.perf_counter_ns()
        result = runtime.step(
            task, profile, min(deadline_ns, now + 500_000_000),
            observation_request=request,
        )
        diagnostic()
        if result.report.failure is not None:
            raise RuntimeError("B11 fixture observation failed: " + result.report.failure.reason)
        frame = runtime.navigation_observation_adapter.latest_frame
        observation = runtime.observation
        inventory = observation.inventory
        held = None if inventory.value is None else inventory.value.main_hand
        occupied = trial.get("case") == "destination_occupied"
        gap_ready = all(
            frame.world.cell((x, FEET_Y - 1, 0)).knowledge
                is (CellKnowledge.BLOCK if occupied and x == 1 else CellKnowledge.AIR)
            for x in range(1, goal_x)
        ) if frame is not None else False
        if (frame is not None
                and math.dist(frame.body.position, (0.5, float(FEET_Y), 0.5)) <= 0.04
                and frame.body.is_on_ground
                and math.hypot(
                    frame.body.velocity_blocks_per_second[0],
                    frame.body.velocity_blocks_per_second[2],
                ) <= 0.04
                and frame.world.cell((0, FEET_Y - 1, 0)).knowledge is CellKnowledge.BLOCK
                and frame.world.cell((goal_x, FEET_Y - 1, 0)).knowledge is CellKnowledge.BLOCK
                and gap_ready
                and inventory.status is FieldStatusV0.VALID
                and held is not None and not held.empty
                and held.item_id == trial.get("item_id", "minecraft:dirt")
                and held.count == trial["initial_items"]):
            return task, profile, frame
    raise RuntimeError("B11 fixture did not become a complete known state")


def _run_fixed(runtime, trial: dict, profile: BehaviorProfileV0,
               frame, deadline_ns: int,
               profiles: NavigationSessionProfiles,
               diagnostic: Callable[[], None]) -> dict:
    requirement = RequiredInteraction(
        interaction_id=trial["trial_id"] + "/place/1/99/0",
        request_id=trial["trial_id"] + "/request",
        goal_id=trial["trial_id"] + "/goal",
        goal_revision=1,
        world_session=frame.session.value,
        kind=InteractionKind.PLACE_BLOCK,
        support=(0, FEET_Y - 1, 0),
        face="east",
        destination=(1, FEET_Y - 1, 0),
        expected_item_id="minecraft:dirt",
        expected_block_id="minecraft:dirt",
        work_position=(1.12, float(FEET_Y), 0.5),
        dependencies=((0, FEET_Y - 1, 0), (1, FEET_Y - 1, 0)),
        requires_sneak=True,
        work_position_tolerance=0.08,
    )
    transaction = BlockPlacementTransaction(requirement)
    assert profiles.ground_modes is not None
    driver = RuntimeBlockPlacementDriver(
        runtime, transaction,
        approach_mode=profiles.ground_modes.require(MovementMode.CROUCH),
    )
    driver.start()
    ticks = 0
    try:
        while ticks < 40 and not transaction.report.terminal:
            ticks += 1
            result = driver.tick(
                profile,
                min(deadline_ns, time.perf_counter_ns() + 500_000_000),
            )
            if result is not None:
                diagnostic()
        if transaction.report.state is not PlacementState.COMPLETE:
            raise RuntimeError(f"B11 fixed placement failed: {transaction.report}")
    finally:
        driver.release()
    return {
        "ticks": ticks,
        "confirmed_placements": 1,
        "reason": transaction.report.reason,
    }


def _run_bridge(runtime, trial: dict, profile: BehaviorProfileV0,
                deadline_ns: int, profiles: NavigationSessionProfiles,
                diagnostic: Callable[[], None]) -> dict:
    gap_count = trial["gap_count"]
    goal_x = gap_count + 1
    session = NavigationSession(
        trial["trial_id"] + "/session",
        profiles,
        bridge_policy=BridgePlacementPolicy(maximum_blocks=gap_count),
    )
    driver = RuntimeWorldChangeNavigationDriver(runtime, session)
    goal = GoalState(
        Aabb(goal_x + 0.4, FEET_Y - 0.05, 0.4,
             goal_x + 0.6, FEET_Y + 0.05, 0.6),
        GoalSupport.SOLID,
        frozenset({MovementMode.WALK}),
        frozenset({"standing"}),
        0.6,
    )
    ticks = 0
    driver.start(trial["trial_id"] + "/goal", 1, goal, time.perf_counter_ns())
    try:
        while ticks < 240 and not driver.report.terminal:
            ticks += 1
            result = driver.tick(
                profile,
                min(deadline_ns, time.perf_counter_ns() + 500_000_000),
            )
            if result is not None:
                diagnostic()
        if driver.report.state != "success":
            raise RuntimeError(f"B11 bridge failed: {driver.report}; {session.report}")
        report = driver.report
    finally:
        if driver.report.terminal:
            driver.release()
        else:
            driver.cancel("b11_trial_limit")
        session.close()
    return {
        "ticks": ticks,
        "confirmed_placements": report.confirmed_placements,
        "reason": report.reason,
    }


def _goal_for_gap(gap_count: int) -> GoalState:
    goal_x = gap_count + 1
    return GoalState(
        Aabb(goal_x + 0.4, FEET_Y - 0.05, 0.4,
             goal_x + 0.6, FEET_Y + 0.05, 0.6),
        GoalSupport.SOLID,
        frozenset({MovementMode.WALK}),
        frozenset({"standing"}),
        0.6,
    )


def _fixed_requirement(trial: dict, frame, *, edge: bool) -> RequiredInteraction:
    return RequiredInteraction(
        interaction_id=trial["trial_id"] + "/place/1/99/0",
        request_id=trial["trial_id"] + "/request",
        goal_id=trial["trial_id"] + "/goal",
        goal_revision=1,
        world_session=frame.session.value,
        kind=InteractionKind.PLACE_BLOCK,
        support=(0, FEET_Y - 1, 0),
        face="east",
        destination=(1, FEET_Y - 1, 0),
        expected_item_id="minecraft:dirt",
        expected_block_id="minecraft:dirt",
        work_position=(1.12 if edge else 0.5, float(FEET_Y), 0.5),
        dependencies=((0, FEET_Y - 1, 0), (1, FEET_Y - 1, 0)),
        requires_sneak=edge,
        work_position_tolerance=0.08,
    )


def _run_negative(
    runtime,
    trial: dict,
    profile: BehaviorProfileV0,
    frame,
    deadline_ns: int,
    profiles: NavigationSessionProfiles,
    diagnostic: Callable[[], None],
    fixture_writer: Callable[[tuple[str, ...], dict], None],
) -> dict:
    case = trial["case"]
    ticks = 0
    operations = 0

    def record(result) -> None:
        nonlocal ticks, operations
        if result is None:
            return
        ticks += 1
        diagnostic()
        if result.decision is not None and result.decision.action.operation is not None:
            operations += 1

    if case in {
        "wrong_item",
        "misaligned_target",
        "destination_occupied",
        "cancel_before_submit",
    }:
        transaction = BlockPlacementTransaction(
            _fixed_requirement(trial, frame, edge=False),
        )
        if case == "cancel_before_submit":
            transaction.cancel("negative_cancel_before_submit")
        proposal = transaction.propose(runtime.observation, frame)
        expected = {
            "wrong_item": "expected_item_not_in_main_hand",
            "misaligned_target": "target_not_aligned",
            "destination_occupied": "destination_not_known_air",
            "cancel_before_submit": "negative_cancel_before_submit",
        }[case]
        if proposal.operation is not None or proposal.reason != expected:
            raise RuntimeError(
                f"B11 negative {case} was not rejected: {proposal}"
            )
        return {
            "ticks": ticks,
            "operations": operations,
            "reason": proposal.reason,
            "passed": True,
        }

    if case == "confirmation_timeout":
        transaction = BlockPlacementTransaction(
            _fixed_requirement(trial, frame, edge=True),
        )
        assert profiles.ground_modes is not None
        placement = RuntimeBlockPlacementDriver(
            runtime,
            transaction,
            approach_mode=profiles.ground_modes.require(MovementMode.CROUCH),
        )
        placement.start()
        try:
            pending = None
            for _ in range(60):
                owner_deadline = min(
                    deadline_ns, time.perf_counter_ns() + 500_000_000,
                )
                proposal = placement.prepare_proposal(owner_deadline)
                pending = placement.prepared_placement
                if pending is None:
                    raise RuntimeError("B11 placement lost its prepared proposal")
                if pending.operation is not None:
                    placement.discard_prepared()
                    break
                result = runtime.control_frame(
                    _task(trial["trial_id"], owner_deadline),
                    profile,
                    owner_deadline,
                    proposals=(proposal,),
                )
                placement.adopt_result(result)
                record(result)
            if pending is None or pending.operation is None:
                raise RuntimeError("B11 timeout injection never reached a valid dispatch")

            for attempt in range(2):
                if attempt:
                    owner_deadline = min(
                        deadline_ns, time.perf_counter_ns() + 500_000_000,
                    )
                    placement.prepare_proposal(owner_deadline)
                    pending = placement.prepared_placement
                    if pending.operation is None:
                        raise RuntimeError(
                            f"B11 timeout retry lost its preconditions: {pending}"
                        )
                    placement.discard_prepared()
                operations += 1
                transaction.register_dispatch(
                    pending,
                    selected=True,
                    receipt_status="pending_confirmation",
                    receipt_reason="injected_no_world_result",
                    control_sequence=runtime.observation.sequence_id + 1,
                )
                while transaction.report.state is PlacementState.AWAITING_CONFIRMATION:
                    owner_deadline = min(
                        deadline_ns, time.perf_counter_ns() + 500_000_000,
                    )
                    proposal = placement.prepare_proposal(owner_deadline)
                    if transaction.report.terminal:
                        placement.discard_prepared()
                        break
                    result = runtime.control_frame(
                        _task(trial["trial_id"], owner_deadline),
                        profile,
                        owner_deadline,
                        proposals=(proposal,),
                    )
                    placement.adopt_result(result)
                    record(result)
            if (transaction.report.state is not PlacementState.FAILED
                    or transaction.report.reason != "confirmation_timeout"
                    or operations != 2):
                raise RuntimeError(
                    f"B11 confirmation timeout was not bounded: "
                    f"{transaction.report}; operations={operations}"
                )
            return {
                "ticks": ticks,
                "operations": operations,
                "reason": transaction.report.reason,
                "passed": True,
            }
        finally:
            placement.release()

    policy = None if case == "no_authorization" else BridgePlacementPolicy(
        maximum_blocks=trial["gap_count"],
    )
    session = NavigationSession(
        trial["trial_id"] + "/session",
        profiles,
        bridge_policy=policy,
    )
    driver = RuntimeWorldChangeNavigationDriver(runtime, session)
    goal = _goal_for_gap(trial["gap_count"])
    driver.start(trial["trial_id"] + "/goal", 1, goal, time.perf_counter_ns())
    try:
        if case == "goal_revision_changed":
            for _ in range(40):
                record(driver.tick(
                    profile,
                    min(deadline_ns, time.perf_counter_ns() + 500_000_000),
                ))
                if driver.report.state == "interaction_required":
                    break
            interaction = session.required_interaction
            if interaction is None:
                raise RuntimeError("B11 goal revision case never produced an interaction")
            old_id = interaction.requirement.interaction_id
            driver.replace_goal(
                trial["trial_id"] + "/goal", 2, goal, time.perf_counter_ns(),
            )
            replacement = session.required_interaction
            if (replacement is not None
                    and replacement.requirement.interaction_id == old_id):
                raise RuntimeError("B11 retained an interaction from an old goal revision")
            driver.cancel("negative_goal_revision_checked")
            return {
                "ticks": ticks,
                "operations": operations,
                "reason": "old_goal_interaction_retired",
                "passed": operations == 0,
            }

        if case == "goal_revision_at_edge_before_dispatch":
            for _ in range(120):
                record(driver.tick(
                    profile,
                    min(deadline_ns, time.perf_counter_ns() + 500_000_000),
                ))
                placement = driver.placement
                if (placement is not None
                        and placement.transaction.report.reason
                        == "target_not_aligned"):
                    break
            placement = driver.placement
            if (placement is None
                    or placement.transaction.report.state is not PlacementState.READY
                    or operations != 0):
                raise RuntimeError(
                    "B11 edge goal revision did not reach the pre-dispatch edge"
                )
            driver.replace_goal(
                trial["trial_id"] + "/goal", 2, goal,
                time.perf_counter_ns(),
            )
            while ticks < 240 and not driver.report.terminal:
                record(driver.tick(
                    profile,
                    min(deadline_ns, time.perf_counter_ns() + 500_000_000),
                ))
            passed = (
                driver.report.state == "success"
                and driver.report.confirmed_placements == 1
                and operations == 1
                and session.report.goal_revision == 2
            )
            return {
                "ticks": ticks,
                "operations": operations,
                "reason": "edge_revision_replanned",
                "passed": passed,
            }

        if case == "goal_revision_after_dispatch":
            for _ in range(160):
                record(driver.tick(
                    profile,
                    min(deadline_ns, time.perf_counter_ns() + 500_000_000),
                ))
                placement = driver.placement
                if (placement is not None
                        and placement.transaction.report.state
                        is PlacementState.AWAITING_CONFIRMATION):
                    break
            placement = driver.placement
            if (placement is None
                    or placement.transaction.report.state
                    is not PlacementState.AWAITING_CONFIRMATION
                    or operations != 1):
                raise RuntimeError(
                    "B11 post-dispatch revision did not retain its placement"
                )
            driver.replace_goal(
                trial["trial_id"] + "/goal", 2, goal,
                time.perf_counter_ns(),
            )
            while ticks < 240 and not driver.report.terminal:
                record(driver.tick(
                    profile,
                    min(deadline_ns, time.perf_counter_ns() + 500_000_000),
                ))
            passed = (
                driver.report.state == "success"
                and driver.report.confirmed_placements == 1
                and operations == 1
                and session.report.goal_revision == 2
            )
            return {
                "ticks": ticks,
                "operations": operations,
                "reason": "dispatched_revision_confirmed_then_replanned",
                "passed": passed,
            }

        if case == "bridge_cell_claimed":
            for _ in range(40):
                record(driver.tick(
                    profile,
                    min(deadline_ns, time.perf_counter_ns() + 500_000_000),
                ))
                if driver.report.state == "interaction_required":
                    break
            if driver.report.state != "interaction_required":
                raise RuntimeError("B11 claimed-cell case had no pending interaction")
            fixture_writer((
                f"setblock 1 {FEET_Y - 1} 0 minecraft:stone replace",
            ), trial)
            time.sleep(.1)

        while ticks < 160 and not driver.report.terminal:
            record(driver.tick(
                profile,
                min(deadline_ns, time.perf_counter_ns() + 500_000_000),
            ))
            if (case == "cancel_after_first_confirmation"
                    and driver.report.confirmed_placements == 1):
                driver.cancel("negative_cancel_after_first_confirmation")
                break

        expected_reason = {
            "no_authorization": "no_route_within_complete_scope",
            "insufficient_items": "insufficient_bridge_materials",
            "cancel_after_first_confirmation": (
                "negative_cancel_after_first_confirmation"
            ),
            "bridge_cell_claimed": "goal_state_satisfied",
        }.get(case)
        if expected_reason is None or driver.report.reason != expected_reason:
            raise RuntimeError(f"B11 negative {case} ended as {driver.report}")
        if (case in {"no_authorization", "insufficient_items", "bridge_cell_claimed"}
                and operations != 0):
            raise RuntimeError(f"B11 negative {case} dispatched an operation")
        if (case == "cancel_after_first_confirmation"
                and (driver.report.confirmed_placements != 1 or operations != 1)):
            raise RuntimeError("B11 cancellation did not stop after one confirmation")
        return {
            "ticks": ticks,
            "operations": operations,
            "reason": driver.report.reason,
            "passed": True,
        }
    finally:
        if not driver.report.terminal:
            driver.cancel("negative_trial_cleanup")
        if driver.report.terminal:
            driver.release()
        session.close()


def run_b11_world_change_runtime(
    runtime,
    backend,
    episode: str,
    directory: Path,
    deadline_ns: int,
    fixture_writer: Callable[[tuple[str, ...], dict], None],
) -> tuple[dict, list[dict], list[dict]]:
    """Run the 60 frozen positive B11 trials against one real Fabric client."""
    profiles = replace(NavigationSessionProfiles.load(CONFIG), air=())
    diagnostic_rows: list[dict] = []
    trial_rows: list[dict] = []
    last_diagnostic_sequence: int | None = None

    def diagnostic() -> None:
        nonlocal last_diagnostic_sequence
        observation = runtime.observation
        if observation.sequence_id == last_diagnostic_sequence:
            return
        row = {
            "episode_id": episode,
            "observation_sequence_id": observation.sequence_id,
            "diagnostics": backend.last_diagnostics,
        }
        diagnostic_rows.append(row)
        append_jsonl(directory / "diagnostics.jsonl", row)
        last_diagnostic_sequence = observation.sequence_id

    diagnostic()
    trials = b11_trial_plan()
    for trial in trials:
        fixture_writer(_fixture_commands(trial), trial)
        _, profile, frame = _ready_fixture(
            runtime, trial, deadline_ns, diagnostic,
        )
        started = time.perf_counter_ns()
        if trial["kind"] == "fixed_placement":
            result = _run_fixed(
                runtime, trial, profile, frame, deadline_ns, profiles,
                diagnostic,
            )
        else:
            result = _run_bridge(
                runtime, trial, profile, deadline_ns, profiles, diagnostic,
            )
        finished = time.perf_counter_ns()
        final_frame = runtime.navigation_observation_adapter.latest_frame
        if final_frame is None:
            raise RuntimeError("B11 trial lost its final navigation frame")
        expected = set(range(1, trial["gap_count"] + 1))
        confirmed = {
            x for x in expected
            if (final_frame.world.cell((x, FEET_Y - 1, 0)).knowledge is CellKnowledge.BLOCK
                and final_frame.world.cell((x, FEET_Y - 1, 0)).block is not None
                and final_frame.world.cell((x, FEET_Y - 1, 0)).block.material_key
                    == "minecraft:dirt")
        }
        if confirmed != expected:
            raise RuntimeError(f"B11 final world confirmation mismatch: {confirmed}")
        row = {
            **trial,
            **result,
            "episode_id": episode,
            "elapsed_ns": finished - started,
            "final_observation_sequence": runtime.observation.sequence_id,
            "passed": True,
        }
        trial_rows.append(row)
        append_jsonl(directory / "b11-trials.jsonl", row)
    negative_rows: list[dict] = []
    for trial in b11_negative_trial_plan():
        fixture_writer(_fixture_commands(trial), trial)
        _, profile, frame = _ready_fixture(
            runtime, trial, deadline_ns, diagnostic,
        )
        started = time.perf_counter_ns()
        result = _run_negative(
            runtime,
            trial,
            profile,
            frame,
            deadline_ns,
            profiles,
            diagnostic,
            fixture_writer,
        )
        finished = time.perf_counter_ns()
        row = {
            **trial,
            **result,
            "episode_id": episode,
            "elapsed_ns": finished - started,
            "final_observation_sequence": runtime.observation.sequence_id,
        }
        if not row["passed"]:
            raise RuntimeError(f"B11 negative trial failed: {row}")
        negative_rows.append(row)
        append_jsonl(directory / "b11-negative-trials.jsonl", row)
    stages = {
        "schema_version": "mc2p.b11-world-change-summary.v1",
        "positive_trials": len(trial_rows),
        "passed_trials": sum(bool(row["passed"]) for row in trial_rows),
        "confirmed_placements": sum(
            row["confirmed_placements"] for row in trial_rows
        ),
        "all_passed": (
            len(trial_rows) == 60
            and all(row["passed"] for row in trial_rows)
        ),
        "negative_trials": len(negative_rows),
        "negative_passed_trials": sum(
            bool(row["passed"]) for row in negative_rows
        ),
        "negative_all_passed": (
            len(negative_rows) == 24
            and all(row["passed"] for row in negative_rows)
        ),
    }
    write_json_atomic(directory / "b11-summary.json", stages)
    return stages, diagnostic_rows, [
        {"name": "b11_positive_trials_complete", "passed": stages["all_passed"]},
        {
            "name": "b11_negative_trials_complete",
            "passed": stages["negative_all_passed"],
        },
    ]


if __name__ == "__main__":
    print([asdict(row) if hasattr(row, "__dataclass_fields__") else row
           for row in b11_trial_plan()])
