"""Twenty full-parent Fabric trials; body perturbation uses Runtime's sole motor."""
from dataclasses import asdict
import math
import time

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, MovementV1
from mc2p.contracts.intent_source import ControlFrameProposalV1, OrderedIntentV1, ordered_intent_id
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.motion_nav.bridge_planner import BridgePlacementPolicy
from mc2p.motion_nav.navigation_session import NavigationSession
from mc2p.motion_nav.world_interaction import PlacementState
from mc2p.skills.world_change_navigation_driver import RuntimeWorldChangeNavigationDriver
from scripts.control_probe_core import append_jsonl


def run_world_change_stop_cases(runtime, backend, directory, deadline_ns, profiles, diagnostic, fixture_writer):
    from scripts.b11_world_change_runtime import _fixture_commands, _ready_fixture, _goal_for_gap, _task, FEET_Y
    rows = []
    for mode in ("complete", "failed", "cancelled", "normal"):
        for repetition in range(1, 6):
            trial = {"trial_id": f"r27-parent-{mode}-{repetition}", "kind": "bridge",
                     "gap_count": 1, "initial_items": 2}
            fixture_writer(_fixture_commands(trial), trial)
            backend.delay_inventory = False
            backend.delay_inventory_after_dispatch = mode in {"failed", "cancelled"}
            backend.wrong_destination = (1, FEET_Y - 1, 0) if mode == "failed" else None
            _, profile, _ = _ready_fixture(runtime, trial, deadline_ns, diagnostic, fixture_writer)
            session = NavigationSession(trial["trial_id"], profiles, bridge_policy=BridgePlacementPolicy(maximum_blocks=1))
            driver = RuntimeWorldChangeNavigationDriver(runtime, session)
            driver.start(trial["trial_id"] + "/goal", 1, _goal_for_gap(1), time.perf_counter_ns())
            perturbation_source = None
            retained = None
            injected = False
            stopped = False
            business_state = None
            released = False
            operations = 0
            body_case = ("inertia", "air", "pending", "inertia", "air")[repetition - 1]
            original_source = None
            try:
                for tick in range(1, 161):
                    if driver.report.terminal:
                        break
                    result = driver.tick(profile, min(deadline_ns, time.perf_counter_ns() + 500_000_000))
                    if result is not None:
                        diagnostic()
                        if result.report.failure is not None:
                            raise RuntimeError(f"full parent stop control failed: {result.report.failure}")
                        operations += int(result.decision is not None and result.decision.action.operation is not None)
                    active = driver.placement
                    if active is not None:
                        retained = active
                    if (mode != "normal" and not injected and active is not None
                            and active.transaction.report.state is PlacementState.AWAITING_CONFIRMATION):
                        # An explicitly labelled test input advances real player physics.
                        # The placement has no prepared frame here; its original owner stays bound.
                        perturbation_source = runtime.register_ordered_source("r27-body-tail-test")
                        now = time.perf_counter_ns()
                        limit = min(deadline_ns, now + 500_000_000)
                        intent = ActionIntentV1(ordered_intent_id(perturbation_source, 1),
                            perturbation_source.source_id, perturbation_source.episode_id,
                            runtime.observation.sequence_id, ActionPriorityV0.SAFETY,
                            now, limit, movement=MovementV1(forward=1, jump=body_case == "air"),
                            valid_for_ticks=4 if body_case == "pending" else 1)
                        update = runtime.control_frame(_task(trial["trial_id"], limit), profile, limit,
                            proposals=(ControlFrameProposalV1((OrderedIntentV1(perturbation_source, 1, intent),),
                                observation_request=ObservationRequestV3("interaction_v1", active.transaction.requirement.dependencies)),))
                        diagnostic()
                        if update.report.failure is not None:
                            raise RuntimeError(f"body tail test input failed: {update.report.failure}")
                        runtime.cancel_source(perturbation_source.source_id)
                        runtime.unregister_ordered_source(perturbation_source)
                        perturbation_source = None
                        injected = True
                        original_source = active.source
                        if mode == "cancelled":
                            driver.cancel("r27_parent_cancel_after_dispatch")
                    if (mode != "normal" and injected and active is not None
                            and active.transaction.report.terminal and active.source is not None and not stopped):
                        business_state = active.transaction.report.state
                        for _ in range(3):
                            driver.cancel("r27_parent_repeat_stop")
                        stopped = True
                    frame = runtime.navigation_observation_adapter.latest_frame
                    speed = math.hypot(frame.body.velocity_blocks_per_second[0], frame.body.velocity_blocks_per_second[2])
                    append_jsonl(directory / "r27-parent-stop-controls.jsonl", {
                        "trial_id": trial["trial_id"], "tick": tick, "body_case": body_case,
                        "sequence": runtime.observation.sequence_id, "movement_tick": frame.body.movement_tick_id,
                        "position": frame.body.position, "velocity": frame.body.velocity_blocks_per_second,
                        "pose": frame.body.pose, "on_ground": frame.body.is_on_ground,
                        "original_source": None if original_source is None else asdict(original_source),
                        "placement_source_owned": retained is not None and retained.source is not None,
                        "world_driver": asdict(driver.report), "placement": None if retained is None else asdict(retained.transaction.report),
                        "injected": injected, "stop_triggered": stopped,
                        "actual_input_applications": [asdict(sample) for sample in runtime.input_ledger.samples_between(
                            max(0, frame.body.movement_tick_id - 8), frame.body.movement_tick_id)],
                        "release_evidence": None if retained is None or retained.body_release_evidence is None
                            else asdict(retained.body_release_evidence),
                        "raw_inventory": backend.raw_inventory_evidence})
                    if retained is not None and retained.source is None:
                        released = frame.body.is_on_ground and speed <= .10
                expected = {"complete": PlacementState.COMPLETE, "normal": PlacementState.COMPLETE,
                            "failed": PlacementState.FAILED, "cancelled": PlacementState.CANCELLED}[mode]
                passed = (retained is not None and retained.transaction.report.state is expected
                    and released and driver.report.terminal
                    and driver.report.state == ("success" if mode == "normal" else "cancelled")
                    and (mode == "normal" or (injected and stopped and business_state is expected))
                    and driver.report.confirmed_placements == (1 if expected is PlacementState.COMPLETE else 0)
                    and operations == 1)
                row = {"trial_id": trial["trial_id"], "mode": mode, "body_case": body_case, "passed": passed,
                       "injected": injected, "stop_triggered": stopped, "released_safely": released,
                       "operations": operations, "world_driver": asdict(driver.report),
                       "placement": None if retained is None else asdict(retained.transaction.report)}
                rows.append(row)
                append_jsonl(directory / "r27-parent-stop-trials.jsonl", row)
                if not passed:
                    raise RuntimeError(f"full parent stop case failed: {row}")
                driver.release()
                for tail in range(5):
                    now = time.perf_counter_ns()
                    limit = min(deadline_ns, now + 500_000_000)
                    result = runtime.step(_task(trial["trial_id"], limit), profile, limit,
                        observation_request=ObservationRequestV3("navigation_v1"))
                    diagnostic()
                    if result.report.failure is not None:
                        raise RuntimeError(f"post-release observation failed: {result.report.failure}")
                    frame = runtime.navigation_observation_adapter.latest_frame
                    append_jsonl(directory / "r27-parent-stop-controls.jsonl", {
                        "trial_id": trial["trial_id"], "post_release_tail": tail,
                        "position": frame.body.position, "on_ground": frame.body.is_on_ground,
                        "velocity": frame.body.velocity_blocks_per_second})
                    if not frame.body.is_on_ground:
                        raise RuntimeError("body became airborne after owner release")
            finally:
                backend.delay_inventory = False
                backend.delay_inventory_after_dispatch = False
                backend.wrong_destination = None
                if perturbation_source is not None:
                    runtime.cancel_source(perturbation_source.source_id)
                    runtime.unregister_ordered_source(perturbation_source)
                session.close()
    return rows
