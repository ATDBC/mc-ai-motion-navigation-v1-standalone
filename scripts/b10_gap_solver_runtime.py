"""Controlled Fabric acceptance for the B10-B one-cell-gap command solver."""
from __future__ import annotations

from dataclasses import asdict, replace
import math
from pathlib import Path
import time
from typing import Callable

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_receipt import ClientBehaviorReceiptV3
from mc2p.contracts.action_v1 import ActionIntentV1, LookV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.intent_source import (
    ControlFrameProposalV1, OrderedIntentV1, ordered_intent_id,
)
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.task import ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0
from mc2p.motion_nav.air_motion import load_air_motion_profiles
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.ground_motion import load_ground_motion_profile
from mc2p.motion_nav.jump_up import load_jump_up_profile
from mc2p.motion_nav.motion_solver import (
    SOLVER_ID, GapSolveRequest, LandingRegion, SolveStatus,
    load_gap_solver_policy, solve_one_cell_gap,
)
from mc2p.motion_nav.motion_worker import MotionSolverWorker
from mc2p.motion_nav.motion_candidate import (
    MotionCandidateAdmitter, MotionCandidateContext, MotionCandidateStatus,
    VerifiedMotionCandidate, VerifiedMotionExecutor,
    VerifiedMotionExecutorState,
)
from mc2p.motion_nav.online_motion import (
    CandidateExecutionWindow, InputApplicationLedger, MotionTickPhase,
    ProjectionStatus, StateAnchor, project_movement_command,
)
from mc2p.motion_nav.physics_1_21 import step as physics_step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView, build_physics_state
from mc2p.motion_nav.physics_types import (
    CalculationStatus, JAVA_1_21_RULESET, StateBuildStatus,
)
from mc2p.motion_nav.navigation_session import (
    NavigationSession, NavigationSessionProfiles, NavigationSessionState,
)
from mc2p.motion_nav.known_map_planner import SurfacePlanningRequest
from mc2p.motion_nav.planner_worker import PlannerWorker
from mc2p.motion_nav.movement_transition import ResourceState
from mc2p.motion_nav.step_transition import load_step_profile
from mc2p.motion_nav.support_surfaces import query_support_surfaces
from mc2p.motion_nav.world_model import CellKnowledge
from scripts.control_probe_core import append_jsonl, write_json_atomic


_SOURCE = "b10-gap-solver"
ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/motion-navigation"
_DIRECTIONS = ((0, 1, 0.0), (1, 0, -90.0), (0, -1, 180.0), (-1, 0, 90.0))
_STATE_ASSUMPTIONS = dict(
    jumping_cooldown_ticks=0,
    movement_speed_attribute=.1,
    step_height_blocks=.6,
    gravity_attribute=.08,
    jump_strength_attribute=.42,
)


def _percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _application_matches(application, movement: MovementV1) -> bool:
    return (
        application.state in {"leased", "neutral"}
        and math.isclose(application.forward, float(movement.forward), abs_tol=1e-9)
        and math.isclose(application.strafe, float(movement.strafe), abs_tol=1e-9)
        and application.jump is movement.jump
        and application.sneak is movement.sneak
        and application.sprint is movement.sprint
    )


def _runtime_navigation_frame(runtime):
    """Read the immutable navigation projection already owned by Runtime."""
    frame = runtime.navigation_observation_adapter.latest_frame
    observation = runtime.observation
    if frame is None or observation is None:
        raise RuntimeError("B10 requires a Runtime-owned navigation frame")
    if frame.body.sequence_id != observation.sequence_id:
        raise RuntimeError("B10 Runtime navigation frame is stale")
    return frame


def _runtime_input_ledger(runtime) -> InputApplicationLedger:
    """Use the ledger owned by Runtime's sole input output path."""
    ledger = runtime.input_ledger
    if type(ledger) is not InputApplicationLedger:
        raise RuntimeError("B10 requires the Runtime-owned input ledger")
    return ledger


def _verified_submission_window(decision) -> tuple[int, int, int] | None:
    """Return a complete proof window, while allowing neutral safety output."""
    values = (
        decision.verified_command_index,
        decision.expected_movement_tick,
        decision.latest_movement_tick,
    )
    if values == (None, None, None):
        return None
    if any(value is None for value in values):
        raise RuntimeError("B10 session returned incomplete verified command identity")
    return values


def _input_window_diagnostics(
    applications,
    verified_window: tuple[int, int, int] | None,
) -> dict:
    ticks = [item.movement_tick_id for item in applications]
    states = [item.state for item in applications]
    command_index = expected_tick = latest_tick = None
    if verified_window is not None:
        command_index, expected_tick, latest_tick = verified_window
    actual_tick = ticks[0] if ticks else None
    return {
        "verified_command_index": command_index,
        "expected_movement_tick": expected_tick,
        "latest_movement_tick": latest_tick,
        "actual_movement_ticks": ticks,
        "application_states": states,
        "actual_minus_expected_ticks": (
            None if actual_tick is None or expected_tick is None
            else actual_tick - expected_tick
        ),
        "actual_minus_latest_ticks": (
            None if actual_tick is None or latest_tick is None
            else actual_tick - latest_tick
        ),
    }


def run_b10_gap_solver_runtime(
    runtime,
    backend,
    episode: str,
    directory: Path,
    deadline_ns: int,
    fixture_writer: Callable[[tuple[tuple[int, int, int], ...], str], None],
    player_teleporter: Callable[[float, float, float, float, float], None],
) -> tuple[dict, list[dict], list[dict]]:
    """Keep background workers alive across the whole controlled probe."""
    with PlannerWorker() as planner_worker, MotionSolverWorker(
        max_pending=4,
    ) as motion_worker:
        return _run_b10_gap_solver_runtime(
            runtime,
            backend,
            episode,
            directory,
            deadline_ns,
            fixture_writer,
            player_teleporter,
            planner_worker=planner_worker,
            motion_worker=motion_worker,
        )


def _run_b10_gap_solver_runtime(
    runtime,
    backend,
    episode: str,
    directory: Path,
    deadline_ns: int,
    fixture_writer: Callable[[tuple[tuple[int, int, int], ...], str], None],
    player_teleporter: Callable[[float, float, float, float, float], None],
    *,
    planner_worker: PlannerWorker,
    motion_worker: MotionSolverWorker,
) -> tuple[dict, list[dict], list[dict]]:
    """Solve and execute frozen trials, including the default coordinator."""
    task = TaskIntentV0(
        "b10-gap-solver", "solve_same_height_one_cell_gap", "{}",
        (SuccessCriterionV0("validation_trials", ComparisonOperatorV0.EQUAL, 150, "count"),),
        5000, deadline_ns, True, 0.0,
    )
    behavior = BehaviorProfileV0()
    frame = _runtime_navigation_frame(runtime)
    rows: list[dict] = []
    counter = 0
    latest_application = None
    manual_source = runtime.register_ordered_source(_SOURCE)

    def diagnostic() -> None:
        row = dict(
            episode_id=episode,
            observation_sequence_id=runtime.observation.sequence_id,
            diagnostics=backend.last_diagnostics,
        )
        rows.append(row)
        append_jsonl(directory / "diagnostics.jsonl", row)

    def step(movement=MovementV1(), *, look=None, request=None):
        nonlocal counter, frame, latest_application
        runtime.cancel_source(manual_source.source_id)
        counter += 1
        now = time.perf_counter_ns()
        intent = ActionIntentV1(
            ordered_intent_id(manual_source, counter),
            manual_source.source_id,
            episode,
            runtime.observation.sequence_id,
            ActionPriorityV0.TASK,
            now,
            min(deadline_ns, now + 750_000_000),
            movement=movement,
            look=look,
            valid_for_ticks=1,
        )
        runtime.submit_ordered_intent(
            OrderedIntentV1(manual_source, counter, intent)
        )
        result = runtime.step(
            task, behavior, min(deadline_ns, now + 5_000_000_000),
            observation_request=request,
        )
        if result.observation is None or result.backend_result is None:
            raise RuntimeError(f"B10 Fabric step failed: {result.report}")
        if result.decision is None or result.decision.action.movement != movement:
            raise RuntimeError("B10 command bypassed the formal arbiter")
        receipt = result.backend_result.receipt
        if type(receipt) is not ClientBehaviorReceiptV3:
            raise RuntimeError("B10 controlled execution requires V3 input evidence")
        owned = tuple(
            item for item in receipt.input_applications
            if item.request_sequence_id == result.decision.action.request_sequence_id
        )
        if not owned or not any(_application_matches(item, movement) for item in owned):
            raise RuntimeError("B10 command was not observed at the player movement input")
        latest_application = receipt.last_input_sample
        frame = _runtime_navigation_frame(runtime)
        diagnostic()
        return result

    diagnostic()
    feet_y = math.floor(frame.body.position[1] + 1.0e-6)
    origin_x = math.floor(frame.body.position[0])
    origin_z = math.floor(frame.body.position[2])
    start_support = (origin_x, feet_y - 1, origin_z)
    targets = tuple(
        (origin_x + dx * 2, feet_y - 1, origin_z + dz * 2)
        for dx, dz, _ in _DIRECTIONS
    )
    turn_directions = tuple((dz, -dx) for dx, dz, _ in _DIRECTIONS)
    turn_supports = tuple(
        (target[0] + turn[0], target[1], target[2] + turn[1])
        for target, turn in zip(targets, turn_directions)
    )
    volume = tuple(
        (x, y, z)
        for x in range(origin_x - 3, origin_x + 4)
        for y in range(feet_y - 2, feet_y + 4)
        for z in range(origin_z - 3, origin_z + 4)
    )
    supports = (start_support, *targets, *turn_supports)
    support_set = set(supports)
    fixture_writer(
        tuple(position for position in volume if position not in support_set),
        "minecraft:air",
    )
    fixture_writer(supports, "minecraft:grass_block")
    request = ObservationRequestV3(
        "navigation_v1",
        tuple(position for position in volume if position not in support_set),
    )

    def teleport(
            yaw: float, *, position: tuple[float, float, float] | None = None,
            observation_request: ObservationRequestV3 | None = None) -> None:
        nonlocal frame
        target_position = position or (
            origin_x + .5, float(feet_y), origin_z + .5,
        )
        active_request = observation_request or request
        player_teleporter(*target_position, yaw, 0.0)
        for _ in range(16):
            step(request=active_request)
            yaw_error = abs((math.degrees(frame.body.yaw_radians) - yaw + 180) % 360 - 180)
            speed = math.hypot(
                frame.body.velocity_blocks_per_second[0],
                frame.body.velocity_blocks_per_second[2],
            )
            if (math.dist(frame.body.position, target_position) <= .03
                    and frame.body.is_on_ground and speed <= .03
                    and yaw_error <= 1.0):
                return
        raise RuntimeError("B10 teleport did not settle")

    def look_at_cell(
            position: tuple[int, int, int], *,
            observation_request: ObservationRequestV3 | None = None) -> None:
        active_request = observation_request or request
        for _ in range(8):
            dx = position[0] + .5 - frame.body.position[0]
            dy = position[1] + .5 - (frame.body.position[1] + 1.62)
            dz = position[2] + .5 - frame.body.position[2]
            target_yaw = math.degrees(math.atan2(-dx, dz))
            target_pitch = -math.degrees(
                math.atan2(dy, max(1.0e-6, math.hypot(dx, dz)))
            )
            step(
                look=LookV1(
                    (target_yaw - math.degrees(frame.body.yaw_radians) + 180) % 360 - 180,
                    target_pitch - math.degrees(frame.body.pitch_radians),
                ),
                request=active_request,
            )
            if frame.world.cell(position).knowledge is CellKnowledge.BLOCK:
                return
        raise RuntimeError(f"B10 support was not formally observed: {position}")

    teleport(0.0)
    for position in supports:
        look_at_cell(position)

    trials: list[dict] = []
    solve_times: list[int] = []

    def run_trial(
            direction_index: int, repetition: int, group: str, *,
            moving_exit: bool = False, prepared_entry: bool = False,
            observation_request: ObservationRequestV3 | None = None,
            speed_band: tuple[float, float] | None = None) -> None:
        nonlocal frame
        dx, dz, yaw = _DIRECTIONS[direction_index]
        active_request = observation_request or request
        if not prepared_entry:
            teleport(yaw, observation_request=active_request)
        if latest_application is None:
            raise RuntimeError("B10 has no movement-tick evidence for its anchor")
        built = build_physics_state(frame, JAVA_1_21_RULESET, _STATE_ASSUMPTIONS)
        if built.status is not StateBuildStatus.READY or built.state is None:
            raise RuntimeError(f"B10 entry state is incomplete: {built}")
        movement_tick = latest_application.movement_tick_id
        entry_state = replace(built.state, movement_tick_id=movement_tick)
        entry_speed = math.hypot(
            frame.body.velocity_blocks_per_second[0],
            frame.body.velocity_blocks_per_second[2],
        )
        anchor = StateAnchor(
            frame.session, frame.body.sequence_id, movement_tick,
            MotionTickPhase.AFTER_MOVEMENT,
            latest_application.request_sequence_id,
            (movement_tick, movement_tick),
            JAVA_1_21_RULESET.ruleset_id,
            JAVA_1_21_RULESET.state_schema,
            "mc2p.input-projection.v1", entry_state,
        )
        target = targets[direction_index]
        solve_request = GapSolveRequest(
            (dx, dz),
            LandingRegion(
                target[0] + .3, target[0] + .7,
                target[2] + .3, target[2] + .7,
                float(target[1] + 1),
            ),
            CandidateExecutionWindow(movement_tick + 1, movement_tick + 2),
            max_candidates=12, max_ticks=20,
            exit_direction=(turn_directions[direction_index]
                            if moving_exit else None),
            exit_motion_ticks=1 if moving_exit else 0,
        )
        started = time.perf_counter_ns()
        solved = solve_one_cell_gap(
            anchor, PhysicsWorldView(frame.world, JAVA_1_21_RULESET), solve_request,
        )
        solve_ns = time.perf_counter_ns() - started
        solve_times.append(solve_ns)
        if solved.status is not SolveStatus.SOLVED or solved.proof is None:
            raise RuntimeError(f"B10 solver returned {solved.status.value}: {solved.reasons}")
        if frame.body.sequence_id != anchor.observation_sequence_id:
            raise RuntimeError("B10 world or body changed before candidate execution")
        reusable = VerifiedMotionCandidate(
            solved.proof,
            MotionCandidateContext(
                f"{episode}-{group}-{direction_index}-{repetition}",
                repetition, "b10-gap-goal", direction_index,
                f"b10-gap-route-{group}-{direction_index}", 1, 0, 1,
                "no_expected_damage",
                ("server_hunger_clock_not_in_physics_state",),
            ),
        )
        admission = MotionCandidateAdmitter().admit(
            reusable, anchor,
            planning_request_id=reusable.context.planning_request_id,
            planning_generation=reusable.context.planning_generation,
            goal_id=reusable.context.goal_id,
            goal_revision=reusable.context.goal_revision,
            route_id=reusable.context.route_id,
            route_revision=reusable.context.route_revision,
            action_index=0, candidate_revision=1,
            risk_policy_id="no_expected_damage",
            intended_start_tick=movement_tick + 1,
            changed_cells=(),
        )
        if (admission.status is not MotionCandidateStatus.ACCEPTED
                or admission.candidate is None):
            raise RuntimeError(f"B10 candidate rejected: {admission.reason}")
        executor = VerifiedMotionExecutor()
        executor.start(admission.candidate)
        ledger = InputApplicationLedger(max_records=64)
        current_anchor = anchor
        samples = []
        for _ in range(len(solved.proof.commands) + 2):
            decision = executor.decide(current_anchor, ledger)
            if decision.state is VerifiedMotionExecutorState.COMPLETE:
                break
            if (decision.state is not VerifiedMotionExecutorState.RUNNING
                    or decision.movement is None
                    or decision.expected_movement_tick is None):
                raise RuntimeError(f"B10 verified executor stopped: {decision}")
            look = None
            if decision.movement_yaw_radians is not None:
                yaw_delta = math.degrees(math.atan2(
                    math.sin(decision.movement_yaw_radians
                             - frame.body.yaw_radians),
                    math.cos(decision.movement_yaw_radians
                             - frame.body.yaw_radians),
                ))
                if abs(yaw_delta) > 1.0e-6:
                    look = LookV1(yaw_delta, 0.0)
            result = step(
                decision.movement, look=look, request=active_request,
            )
            action = result.decision.action
            ledger.submit(
                anchor.session, action,
                requested_first_tick=decision.expected_movement_tick,
                latest_allowed_first_tick=decision.latest_movement_tick,
            )
            executor.register_submission(
                decision.command_index,
                control_sequence=action.request_sequence_id,
                requested_movement_tick=decision.expected_movement_tick,
            )
            receipt = result.backend_result.receipt
            owned_applications = tuple(
                application
                for application in receipt.input_applications
                if application.request_sequence_id == action.request_sequence_id
            )
            if not owned_applications:
                raise RuntimeError(
                    "B10 verified input had no formal application sample"
                )
            for application in owned_applications:
                ledger.observe_sample(application)
            if latest_application is None:
                raise RuntimeError("B10 execution lost the movement-tick receipt")
            built_after = build_physics_state(
                frame, JAVA_1_21_RULESET, _STATE_ASSUMPTIONS,
            )
            if (built_after.status is not StateBuildStatus.READY
                    or built_after.state is None):
                raise RuntimeError("B10 could not anchor the observed exit state")
            observed_tick = latest_application.movement_tick_id
            current_anchor = StateAnchor(
                frame.session, frame.body.sequence_id, observed_tick,
                MotionTickPhase.AFTER_MOVEMENT,
                latest_application.request_sequence_id,
                (observed_tick, observed_tick),
                JAVA_1_21_RULESET.ruleset_id,
                JAVA_1_21_RULESET.state_schema,
                "mc2p.input-projection.v1",
                replace(built_after.state, movement_tick_id=observed_tick),
            )
            samples.append(dict(
                command_index=decision.command_index,
                command=asdict(decision.movement),
                required_movement_yaw_radians=(
                    decision.movement_yaw_radians
                ),
                look=asdict(look) if look is not None else None,
                position=list(frame.body.position),
                velocity=list(frame.body.velocity_blocks_per_second),
                on_ground=frame.body.is_on_ground,
                horizontal_collision=frame.body.horizontal_collision,
                receipt_status=result.backend_result.receipt.status,
            ))
        else:
            raise RuntimeError("B10 verified executor exceeded its proof horizon")
        if executor.state is not VerifiedMotionExecutorState.COMPLETE:
            raise RuntimeError(f"B10 verified executor ended in {executor.state.value}")
        target_position = (target[0] + .5, float(target[1] + 1), target[2] + .5)
        horizontal_error = math.hypot(
            frame.body.position[0] - target_position[0],
            frame.body.position[2] - target_position[2],
        )
        trial = dict(
            name=f"{group}-{direction_index}-{repetition}",
            group=group, direction_index=direction_index, repetition=repetition,
            moving_exit=moving_exit,
            requested_speed_band=list(speed_band) if speed_band is not None else None,
            entry_speed_blocks_per_second=entry_speed,
            entry_position=list(anchor.physics_state.position),
            start_observation_sequence_id=anchor.observation_sequence_id,
            anchor_movement_tick_id=anchor.movement_tick_id,
            solve_time_ns=solve_ns,
            candidates_evaluated=solved.candidates_evaluated,
            command_count=len(solved.proof.commands),
            tick_input_count=len(solved.proof.tick_inputs),
            trajectory_state_count=len(solved.proof.trajectory),
            world_dependency_count=len(solved.proof.world_dependencies),
            target_position=list(target_position),
            final_position=list(frame.body.position),
            horizontal_error_blocks=horizontal_error,
            level_error_blocks=abs(frame.body.position[1] - target_position[1]),
            jump_pulses=sum(int(row["command"]["jump"]) for row in samples),
            horizontal_collisions=sum(int(row["horizontal_collision"]) for row in samples),
            landed=frame.body.is_on_ground,
            samples=samples,
        )
        trials.append(trial)
        append_jsonl(directory / "b10-gap-solver-trials.jsonl", trial)

    run_trial(0, 0, "preflight")
    for direction_index in range(4):
        for repetition in range(10):
            run_trial(direction_index, repetition, "validation")
    run_trial(0, 0, "turn_preflight", moving_exit=True)
    for direction_index in range(4):
        for repetition in range(10):
            run_trial(
                direction_index, repetition, "turn_validation",
                moving_exit=True,
            )

    environment = load_frozen_environment(CONFIG / "environment-v1.json")
    catalog = BlockMotionCatalog.load(
        CONFIG / "block-motion-traits-v1.json",
        CONFIG / "vanilla-block-registry-1_21.json",
    )
    ground_profile = load_ground_motion_profile(
        CONFIG / "ordinary-ground-b07-v1.json",
        environment=environment, catalog=catalog,
    )
    jump_profile = load_jump_up_profile(
        CONFIG / "jump-up-b06-v1.json",
        environment=environment, catalog=catalog,
    )
    step_profile = load_step_profile(
        CONFIG / "step-b07-v1.json", environment=environment,
    )
    air_profiles = load_air_motion_profiles(
        CONFIG / "air-motions-b09-v1.json",
        environment=environment, catalog=catalog,
    )
    session_profiles = NavigationSessionProfiles(
        ground_profile, jump_profile, step_profile, air=air_profiles,
        gap_solver=load_gap_solver_policy(
            CONFIG / "air-motions-b09-v1.json",
        ),
    )
    coordinator_trials: list[dict] = []

    def anchor_now() -> StateAnchor:
        if latest_application is None:
            raise RuntimeError("B10 coordinator has no movement-tick evidence")
        built = build_physics_state(frame, JAVA_1_21_RULESET, _STATE_ASSUMPTIONS)
        if built.status is not StateBuildStatus.READY or built.state is None:
            raise RuntimeError(f"B10 coordinator state is incomplete: {built}")
        tick = latest_application.movement_tick_id
        return StateAnchor(
            frame.session, frame.body.sequence_id, tick,
            MotionTickPhase.AFTER_MOVEMENT,
            latest_application.request_sequence_id, (tick, tick),
            JAVA_1_21_RULESET.ruleset_id,
            JAVA_1_21_RULESET.state_schema,
            "mc2p.input-projection.v1",
            replace(built.state, movement_tick_id=tick),
        )

    for repetition in range(10):
        direction_index = repetition % len(_DIRECTIONS)
        _, _, yaw = _DIRECTIONS[direction_index]
        teleport(yaw)
        anchor = anchor_now()
        target = targets[direction_index]
        start_query = query_support_surfaces(
            frame.world, origin_x, origin_z, feet_y, feet_y,
        )
        target_query = query_support_surfaces(
            frame.world, target[0], target[2], feet_y, feet_y,
        )
        if not start_query.surfaces or not target_query.surfaces:
            raise RuntimeError("B10 session could not resolve a known support surface")
        start_id = start_query.surfaces[0].node_id
        end_id = target_query.surfaces[0].node_id
        route_id = f"b10-session-{direction_index}-{repetition}"
        session = NavigationSession(
            route_id,
            session_profiles,
            planner_worker=planner_worker,
            owns_planner_worker=False,
            motion_worker=motion_worker,
            observation_adapter=runtime.navigation_observation_adapter,
        )
        source = runtime.register_ordered_source(route_id)
        session.bind_source(source)
        try:
            session.start(SurfacePlanningRequest(
                repetition + 1,
                f"{route_id}-request",
                "b10-gap-goal",
                repetition + 1,
                frame.session.value,
                start_id,
                end_id,
                initial_resources=ResourceState((
                    ("food_points", float(frame.body.food_points)),
                )),
            ), frame)
            ledger = _runtime_input_ledger(runtime)
            waiting_polls = 0
            current_anchor = anchor
            samples: list[dict] = []
            poll_deadline = time.perf_counter() + 5.0
            while len(samples) < 22:
                proposal_started_ns = time.perf_counter_ns()
                observation_age_ns = max(
                    0,
                    proposal_started_ns
                    - runtime.observation.received_at_monotonic_ns,
                )
                proposal = session.propose(
                    frame,
                    current_anchor,
                    min(deadline_ns, time.perf_counter_ns() + 500_000_000),
                    input_ledger=ledger,
                )
                proposal_elapsed_ns = time.perf_counter_ns() - proposal_started_ns
                decision = proposal.route_decision
                if proposal.report.state is NavigationSessionState.COMPLETE:
                    break
                if decision is None or not decision.submit_input:
                    waiting_polls += 1
                    if proposal.report.terminal:
                        raise RuntimeError(
                            "B10 navigation session stopped before completion: "
                            f"{proposal.report.reason}"
                        )
                    if time.perf_counter() >= poll_deadline:
                        raise RuntimeError(
                            "B10 navigation session did not deliver motion in time: "
                            f"{proposal.report.reason}"
                        )
                    if (decision is not None
                            and decision.reason_code == "awaiting_verified_motion"
                            and proposal.control_frame is not None):
                        control_started_ns = time.perf_counter_ns()
                        backend_started_ns = runtime.backend_elapsed_ns_total
                        blocking_started_ns = runtime.backend_blocking_io_ns_total
                        idle = runtime.control_frame(
                            task,
                            behavior,
                            min(deadline_ns, time.perf_counter_ns() + 5_000_000_000),
                            proposals=(proposal.control_frame,),
                        )
                        control_wall_ns = time.perf_counter_ns() - control_started_ns
                        backend_elapsed_ns = (
                            runtime.backend_elapsed_ns_total - backend_started_ns
                        )
                        blocking_elapsed_ns = (
                            runtime.backend_blocking_io_ns_total - blocking_started_ns
                        )
                        if (idle.observation is None
                                or idle.backend_result is None
                                or idle.decision is None):
                            raise RuntimeError(
                                f"B10 waiting observation failed: {idle.report}"
                            )
                        receipt = idle.backend_result.receipt
                        if type(receipt) is not ClientBehaviorReceiptV3:
                            raise RuntimeError(
                                "B10 waiting observation requires V3 input evidence"
                            )
                        latest_application = receipt.last_input_sample
                        if latest_application is None:
                            raise RuntimeError(
                                "B10 waiting observation lost its movement tick"
                            )
                        frame = _runtime_navigation_frame(runtime)
                        diagnostic()
                        current_anchor = anchor_now()
                        append_jsonl(
                            directory / "b10-coordinator-control-frames.jsonl",
                            {
                                "trial": f"default-session-{direction_index}-{repetition}",
                                "observation_sequence_id": frame.body.sequence_id,
                                "session_state": proposal.report.state.value,
                                "decision_state": decision.state.value,
                                "reason_code": decision.reason_code,
                                "submit_input": decision.submit_input,
                                "observation_age_ns": observation_age_ns,
                                "proposal_elapsed_ns": proposal_elapsed_ns,
                                "control_wall_ns": control_wall_ns,
                                "backend_elapsed_ns": backend_elapsed_ns,
                                "backend_blocking_io_ns": blocking_elapsed_ns,
                                "runtime_outside_backend_ns": max(
                                    0, control_wall_ns - backend_elapsed_ns,
                                ),
                            },
                        )
                    else:
                        time.sleep(.005)
                    continue
                if proposal.control_frame is None:
                    raise RuntimeError("B10 session omitted its control frame")
                verified_window = _verified_submission_window(decision)
                now = time.perf_counter_ns()
                backend_started_ns = runtime.backend_elapsed_ns_total
                blocking_started_ns = runtime.backend_blocking_io_ns_total
                result = runtime.control_frame(
                    task,
                    behavior,
                    min(deadline_ns, now + 5_000_000_000),
                    proposals=(
                        proposal.control_frame,
                        ControlFrameProposalV1(observation_request=request),
                    ),
                    input_execution_window=(
                        None if verified_window is None
                        else CandidateExecutionWindow(
                            verified_window[1], verified_window[2],
                        )
                    ),
                )
                control_wall_ns = time.perf_counter_ns() - now
                backend_elapsed_ns = (
                    runtime.backend_elapsed_ns_total - backend_started_ns
                )
                blocking_elapsed_ns = (
                    runtime.backend_blocking_io_ns_total - blocking_started_ns
                )
                if (result.observation is None or result.backend_result is None
                        or result.decision is None):
                    raise RuntimeError(
                        f"B10 navigation session Fabric step failed: {result.report}"
                    )
                action = result.decision.action
                if verified_window is not None:
                    session.register_verified_submission(
                        proposal, control_sequence=action.request_sequence_id,
                    )
                receipt = result.backend_result.receipt
                if type(receipt) is not ClientBehaviorReceiptV3:
                    raise RuntimeError(
                        "B10 navigation session requires V3 input evidence"
                    )
                owned_applications = tuple(
                    application
                    for application in receipt.input_applications
                    if application.request_sequence_id == action.request_sequence_id
                )
                if not owned_applications:
                    raise RuntimeError(
                        "B10 session input had no formal application sample"
                    )
                latest_application = receipt.last_input_sample
                frame = _runtime_navigation_frame(runtime)
                diagnostic()
                current_anchor = anchor_now()
                window_diagnostics = _input_window_diagnostics(
                    owned_applications, verified_window,
                )
                sample = dict(
                    command_index=decision.verified_command_index,
                    decision_state=decision.state.value,
                    reason_code=decision.reason_code,
                    submit_input=decision.submit_input,
                    movement=asdict(decision.movement),
                    position=list(frame.body.position),
                    on_ground=frame.body.is_on_ground,
                    horizontal_collision=frame.body.horizontal_collision,
                    observation_age_ns=observation_age_ns,
                    proposal_elapsed_ns=proposal_elapsed_ns,
                    route_control_time_ns=decision.control_time_ns,
                    control_wall_ns=control_wall_ns,
                    backend_elapsed_ns=backend_elapsed_ns,
                    backend_blocking_io_ns=blocking_elapsed_ns,
                    runtime_outside_backend_ns=max(
                        0, control_wall_ns - backend_elapsed_ns,
                    ),
                    **window_diagnostics,
                )
                samples.append(sample)
                append_jsonl(
                    directory / "b10-coordinator-control-frames.jsonl",
                    {
                        "trial": f"default-session-{direction_index}-{repetition}",
                        "observation_sequence_id": frame.body.sequence_id,
                        "session_state": proposal.report.state.value,
                        **sample,
                    },
                )
            else:
                raise RuntimeError("B10 navigation session exceeded its motion horizon")
            if session.report.state is not NavigationSessionState.COMPLETE:
                raise RuntimeError(
                    f"B10 navigation session ended in {session.report.state.value}: "
                    f"{session.report.reason}"
                )
            target_position = (
                target[0] + .5, float(target[1] + 1), target[2] + .5,
            )
            trial = dict(
                name=f"default-session-{direction_index}-{repetition}",
                direction_index=direction_index,
                repetition=repetition,
                waiting_polls=waiting_polls,
                command_count=len(samples),
                final_position=list(frame.body.position),
                horizontal_error_blocks=math.hypot(
                    frame.body.position[0] - target_position[0],
                    frame.body.position[2] - target_position[2],
                ),
                level_error_blocks=abs(frame.body.position[1] - target_position[1]),
                landed=frame.body.is_on_ground,
                horizontal_collisions=sum(
                    int(sample["horizontal_collision"]) for sample in samples
                ),
                samples=samples,
            )
            coordinator_trials.append(trial)
            append_jsonl(directory / "b10-coordinator-trials.jsonl", trial)
        finally:
            if runtime.state.value == "ready":
                runtime.cancel_source(source.source_id)
                runtime.unregister_ordered_source(source)
            session.unbind_source(source)
            session.close()

    moving_negative_trials: list[dict] = []
    speed_bands = ((.5, 1.0), (1.0, 2.0), (2.0, 3.0))
    speed_targets = (.75, 1.5, 2.5)

    def configure_moving_lane(direction_index: int) -> ObservationRequestV3:
        dx, dz, _ = _DIRECTIONS[direction_index]
        lane_supports = tuple(dict.fromkeys((
            *( (origin_x - dx * distance, feet_y - 1,
                 origin_z - dz * distance) for distance in range(4) ),
            targets[direction_index],
        )))
        # The negative landing case must remain fully observed while all bounded
        # templates run to their horizon.  Keep a narrow corridor, but extend it
        # beyond the landing support so a rejected candidate is not mislabeled as
        # missing world knowledge after it passes the deliberately narrow target.
        lane_volume = tuple(
            (
                origin_x + dx * along - dz * lateral,
                y,
                origin_z + dz * along + dx * lateral,
            )
            for along in range(-5, 9)
            for lateral in range(-2, 3)
            for y in range(feet_y - 2, feet_y + 4)
        )
        known_gap_and_landing_depth = tuple(
            (origin_x + dx * distance, y, origin_z + dz * distance)
            for distance in (0, 1, 2)
            for y in range(feet_y - 24, feet_y - 2)
        )
        support_set = set(lane_supports)
        air = tuple(dict.fromkeys(
            position
            for position in (*lane_volume, *known_gap_and_landing_depth)
            if position not in support_set
        ))
        fixture_writer(air, "minecraft:air")
        fixture_writer(lane_supports, "minecraft:grass_block")
        return ObservationRequestV3("navigation_v1", air)

    def prepare_moving_entry(
            direction_index: int, speed_band: tuple[float, float],
            target_speed: float,
            observation_request: ObservationRequestV3, *,
            look_position: tuple[int, int, int] | None = None) -> None:
        nonlocal frame
        dx, dz, yaw = _DIRECTIONS[direction_index]
        start_position = (
            origin_x + .5 - dx * 3.0,
            float(feet_y),
            origin_z + .5 - dz * 3.0,
        )
        teleport(
            yaw, position=start_position,
            observation_request=observation_request,
        )
        look_at_cell(
            look_position or targets[direction_index],
            observation_request=observation_request,
        )
        target_along = -.05
        controls = (
            MovementV1(),
            MovementV1(forward=1),
            MovementV1(forward=1, sprint=True),
            MovementV1(forward=-1),
        )
        for _ in range(80):
            along = (
                (frame.body.position[0] - (origin_x + .5)) * dx
                + (frame.body.position[2] - (origin_z + .5)) * dz
            )
            forward_speed = (
                frame.body.velocity_blocks_per_second[0] * dx
                + frame.body.velocity_blocks_per_second[2] * dz
            )
            if (-.18 <= along <= .05
                    and speed_band[0] - 1.0e-6 <= forward_speed
                        <= speed_band[1] + 1.0e-6
                    and frame.body.is_on_ground):
                return
            if along > .07:
                raise RuntimeError(
                    "R4 approach passed its verified entry before reaching "
                    f"speed band {speed_band}: along={along}, speed={forward_speed}"
                )
            built = build_physics_state(
                frame, JAVA_1_21_RULESET, _STATE_ASSUMPTIONS,
            )
            if built.status is not StateBuildStatus.READY or built.state is None:
                raise RuntimeError(f"R4 approach state is incomplete: {built}")
            physics_world = PhysicsWorldView(frame.world, JAVA_1_21_RULESET)
            ranked = []
            position_weight = 5.0 if along > -.5 else 1.5
            for movement in controls:
                projected = project_movement_command(
                    built.state, movement,
                    movement_yaw_radians=built.state.yaw_radians,
                )
                if (projected.status is not ProjectionStatus.READY
                        or projected.tick_input is None):
                    continue
                calculated = physics_step(
                    built.state, projected.tick_input,
                    physics_world, JAVA_1_21_RULESET,
                )
                if (calculated.status is not CalculationStatus.OK
                        or calculated.next_state is None):
                    continue
                next_state = calculated.next_state
                next_along = (
                    (next_state.position[0] - (origin_x + .5)) * dx
                    + (next_state.position[2] - (origin_z + .5)) * dz
                )
                next_speed = 20.0 * (
                    next_state.velocity_blocks_per_tick[0] * dx
                    + next_state.velocity_blocks_per_tick[2] * dz
                )
                score = (
                    abs(next_along - target_along) * position_weight
                    + abs(next_speed - target_speed) * .8
                )
                if next_along > .06:
                    score += 100.0 + (next_along - .06) * 100.0
                if next_speed < -.01:
                    score += 20.0
                ranked.append((score, movement))
            if not ranked:
                raise RuntimeError("R4 approach has no calculable ground input")
            ranked.sort(key=lambda item: item[0])
            step(ranked[0][1], request=observation_request)
        raise RuntimeError(
            f"R4 approach did not reach speed band {speed_band} within 80 ticks"
        )

    for direction_index in range(4):
        moving_request = configure_moving_lane(direction_index)
        dx, dz, _ = _DIRECTIONS[direction_index]
        target = targets[direction_index]
        for band_index, (speed_band, target_speed) in enumerate(zip(
                speed_bands, speed_targets)):
            for repetition in range(5):
                if repetition == 0:
                    low_ceiling = (target[0], feet_y + 1, target[2])
                    fixture_writer((low_ceiling,), "minecraft:stone")
                    blocked_request = ObservationRequestV3(
                        "navigation_v1",
                        tuple(
                            position for position in moving_request.air_positions
                            if position != low_ceiling
                        ),
                    )
                    prepare_moving_entry(
                        direction_index,
                        speed_band,
                        target_speed,
                        blocked_request,
                        look_position=low_ceiling,
                    )
                    negative_anchor = anchor_now()
                    landing = LandingRegion(
                        target[0] + .3, target[0] + .7,
                        target[2] + .3, target[2] + .7,
                        float(target[1] + 1),
                    )
                    reverse = solve_one_cell_gap(
                        negative_anchor,
                        PhysicsWorldView(frame.world, JAVA_1_21_RULESET),
                        GapSolveRequest(
                            (-dx, -dz), landing,
                            CandidateExecutionWindow(
                                negative_anchor.movement_tick_id + 1,
                                negative_anchor.movement_tick_id + 2,
                            ),
                        ),
                    )
                    insufficient = solve_one_cell_gap(
                        negative_anchor,
                        PhysicsWorldView(frame.world, JAVA_1_21_RULESET),
                        GapSolveRequest(
                            (dx, dz), landing,
                            CandidateExecutionWindow(
                                negative_anchor.movement_tick_id + 1,
                                negative_anchor.movement_tick_id + 2,
                            ),
                        ),
                    )
                    negative = dict(
                        direction_index=direction_index,
                        speed_band_index=band_index,
                        entry_speed_blocks_per_second=20.0 * math.hypot(
                            negative_anchor.physics_state.velocity_blocks_per_tick[0],
                            negative_anchor.physics_state.velocity_blocks_per_tick[2],
                        ),
                        entry_direction_status=reverse.status.value,
                        entry_direction_reasons=list(reverse.reasons),
                        insufficient_landing_status=insufficient.status.value,
                        insufficient_landing_reasons=list(insufficient.reasons),
                        insufficient_landing_missing_cells=(
                            [list(position) for position in insufficient.missing_cells]
                        ),
                    )
                    moving_negative_trials.append(negative)
                    append_jsonl(
                        directory / "r4-moving-gap-negative-trials.jsonl",
                        negative,
                    )
                    fixture_writer((low_ceiling,), "minecraft:air")
                    prepare_moving_entry(
                        direction_index, speed_band, target_speed, moving_request,
                    )
                    if frame.world.cell(low_ceiling).knowledge is not CellKnowledge.AIR:
                        raise RuntimeError(
                            "R4 removed landing obstruction was not reobserved as air"
                        )
                else:
                    prepare_moving_entry(
                        direction_index, speed_band, target_speed, moving_request,
                    )
                run_trial(
                    direction_index, repetition,
                    f"r4_validation_{band_index}",
                    prepared_entry=True,
                    observation_request=moving_request,
                    speed_band=speed_band,
                )

    validation = [trial for trial in trials if trial["group"] == "validation"]
    turn_validation = [
        trial for trial in trials if trial["group"] == "turn_validation"
    ]
    moving_validation = [
        trial for trial in trials if trial["group"].startswith("r4_validation_")
    ]
    summary = dict(
        schema_version="mc2p.b10-gap-solver.v2",
        solver_id=SOLVER_ID,
        preflight_count=1,
        turn_preflight_count=1,
        validation_count=len(validation),
        validation_success_count=sum(
            trial["landed"]
            and trial["horizontal_error_blocks"] <= .2
            and trial["level_error_blocks"] <= .03
            and trial["horizontal_collisions"] == 0
            and trial["jump_pulses"] == 1
            for trial in validation
        ),
        turn_validation_count=len(turn_validation),
        turn_validation_success_count=sum(
            trial["landed"]
            and trial["horizontal_error_blocks"] <= .2
            and trial["level_error_blocks"] <= .03
            and trial["horizontal_collisions"] == 0
            and trial["jump_pulses"] == 1
            for trial in turn_validation
        ),
        moving_validation_count=len(moving_validation),
        moving_validation_success_count=sum(
            trial["landed"]
            and trial["horizontal_error_blocks"] <= .2
            and trial["level_error_blocks"] <= .03
            and trial["horizontal_collisions"] == 0
            and trial["jump_pulses"] == 1
            and trial["requested_speed_band"][0] - 1.0e-6
                <= trial["entry_speed_blocks_per_second"]
                <= trial["requested_speed_band"][1] + 1.0e-6
            for trial in moving_validation
        ),
        moving_negative_case_count=len(moving_negative_trials) * 2,
        moving_negative_trials=moving_negative_trials,
        coordinator_validation_count=len(coordinator_trials),
        coordinator_validation_success_count=sum(
            trial["waiting_polls"] > 0
            and trial["landed"]
            and trial["horizontal_error_blocks"] <= .2
            and trial["level_error_blocks"] <= .03
            and trial["horizontal_collisions"] == 0
            for trial in coordinator_trials
        ),
        solve_time_ns=dict(
            samples=len(solve_times),
            p95=_percentile(solve_times, .95),
            p99=_percentile(solve_times, .99),
            maximum=max(solve_times),
        ),
        trials=trials,
        coordinator_trials=coordinator_trials,
    )
    write_json_atomic(directory / "b10-gap-solver.json", summary)
    checks = [
        dict(name="b10b_preflight_separate_from_validation",
             passed=(len(trials) - len(moving_validation) == 82
                     and summary["preflight_count"] == 1
                     and summary["turn_preflight_count"] == 1)),
        dict(name="b10b_four_directions_ten_fabric_runs_each", passed=(
            len(validation) == 40
            and {index: sum(trial["direction_index"] == index for trial in validation)
                 for index in range(4)} == {0: 10, 1: 10, 2: 10, 3: 10}
        )),
        dict(name="b10c_turn_exit_four_directions_ten_runs_each", passed=(
            len(turn_validation) == 40
            and summary["turn_validation_success_count"] == 40
            and {index: sum(trial["direction_index"] == index
                            for trial in turn_validation)
                 for index in range(4)} == {0: 10, 1: 10, 2: 10, 3: 10}
        )),
        dict(name="b10b_all_validation_runs_land_in_target", passed=(
            summary["validation_success_count"] == 40
        )),
        dict(name="b10c_default_coordinator_ten_fabric_runs", passed=(
            summary["coordinator_validation_count"] == 10
            and summary["coordinator_validation_success_count"] == 10
        )),
        dict(name="r4_three_speed_bands_four_directions_five_runs_each",
             passed=(
                 len(moving_validation) == 60
                 and summary["moving_validation_success_count"] == 60
                 and {
                     (direction, band): sum(
                         trial["direction_index"] == direction
                         and trial["group"] == f"r4_validation_{band}"
                         for trial in moving_validation
                     )
                     for direction in range(4) for band in range(3)
                 } == {
                     (direction, band): 5
                     for direction in range(4) for band in range(3)
                 }
             )),
        dict(name="r4_two_typed_negative_cases_per_speed_direction_category",
             passed=(
                 summary["moving_negative_case_count"] == 24
                 and all(
                     trial["entry_direction_status"]
                         == SolveStatus.NEEDS_STATE.value
                     and "entry_velocity_direction"
                         in trial["entry_direction_reasons"]
                     and trial["insufficient_landing_status"]
                         == SolveStatus.NO_SOLUTION_WITHIN_SEARCH.value
                     for trial in moving_negative_trials
                 )
             )),
        dict(name="b10b_proof_records_stay_correlated", passed=all(
            trial["command_count"] == trial["tick_input_count"]
            and trial["trajectory_state_count"] == trial["command_count"] + 1
            and trial["world_dependency_count"] > 0
            for trial in trials
        )),
        dict(name="b10b_solver_is_bounded", passed=(
            all(trial["candidates_evaluated"] <= 12 for trial in trials)
            and summary["solve_time_ns"]["maximum"] < 30_000_000
        )),
    ]
    runtime.unregister_ordered_source(manual_source)
    return summary, rows, checks
