"""D114 executes the B10 worker-delay loop through the formal driver."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.intent_source import OrderedIntentV1, ordered_intent_id
from mc2p.contracts.reset import ResetRequestV0
from mc2p.motion_nav.navigation_session import (
    NavigationSession,
    NavigationSessionProfiles,
)
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1, RuntimeStateV1
from mc2p.runtime.trace import trace_projection
from scripts import b10_gap_solver_runtime as probe
from scripts import analyze_d114_formal_driver_run as run_analysis
from tests.motion_nav.test_action_continuity_formal import _gap_case
from tests.sim.backend import CalculatorBackend, Scene
from tests.sim.runner import (
    InlineMotionWorker,
    InlinePlannerWorker,
    _goal,
    seed_memory,
)
from tests.test_player_runtime import _RecordingTrace, _task


CONFIG = Path("config/motion-navigation")


def _narrow_b10_gap_case(turns: int):
    dx, dz, yaw = probe._DIRECTIONS[turns]
    target = (dx * 2, 63, dz * 2)
    side = (target[0] + dz, 63, target[2] - dx)
    scene = Scene(
        {
            (0, 63, 0): "minecraft:grass_block",
            target: "minecraft:grass_block",
            side: "minecraft:grass_block",
        },
        ((-4, 4), (60, 70), (-4, 4)),
    )
    return type(_gap_case(0))(
        f"d114-narrow-gap-{turns}",
        scene,
        (.5, 64.0, .5),
        (target[0] + .5, 64.0, target[2] + .5),
        yaw_degrees=yaw,
        max_ticks=200,
    )


class D114B10FormalDriverTests(unittest.TestCase):
    def test_read_only_correction_rejects_missing_or_duplicate_body_ticks(self):
        def trace_row(tick):
            return {
                "payload": {
                    "backend_result": {
                        "observation": {
                            "self_state": {
                                "value": {
                                    "movement_tick_id": tick,
                                    "position": {"x": 0.0, "y": 0.0, "z": 0.0},
                                    "is_on_ground": True,
                                },
                            },
                        },
                    },
                },
            }

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace = root / "trace.jsonl"
            trial = root / "b10-coordinator-trials.jsonl"
            trial.write_text(json.dumps({
                "name": "delay-10",
                "scenario_kind": "worker_delay",
                "worker_delay_ticks": 10,
                "worker_injection": {"submissions": [{
                    "job_anchor_movement_tick": 10,
                    "first_control_visible_tick": 12,
                }]},
                "samples": [{
                    "grant_movement_tick": 12,
                    "position": [0.0, 0.0, 0.0],
                }],
                "verified_command_count": 0,
                "left_start_support": False,
                "terminal_state": "failed",
                "terminal_reason": "execution_window_expired",
                "damage_points": 0.0,
                "source_released": True,
            }) + "\n", encoding="utf-8")

            trace.write_text("\n".join(
                json.dumps(trace_row(tick)) for tick in (10, 12)
            ) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "omitted body evidence"):
                run_analysis.analyze(root)

            trace.write_text("\n".join(
                json.dumps(trace_row(tick)) for tick in (10, 11, 11, 12)
            ) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate body evidence"):
                run_analysis.analyze(root)

    def test_ground_flag_blip_without_displacement_is_not_departure(self):
        def frame(position, on_ground):
            body = SimpleNamespace(
                position=SimpleNamespace(
                    x=position[0], y=position[1], z=position[2]
                ),
                is_on_ground=on_ground,
            )
            return SimpleNamespace(result=SimpleNamespace(
                observation=SimpleNamespace(
                    self_state=SimpleNamespace(value=body)
                )
            ))

        start = (-2.5, 0.0, -6.5)
        self.assertFalse(probe._b10_uncommanded_body_departed(
            (frame(start, False), frame(start, True)),
            verified_command_count=0,
            start_position=start,
        ))
        self.assertFalse(probe._b10_uncommanded_body_departed(
            (frame((-2.48, 0.0, -6.5), True),),
            verified_command_count=0,
            start_position=start,
        ))
        for displaced in (
            (-2.48, 0.0, -6.5),
            (-2.5, 0.02, -6.5),
            (-2.5, 0.0, -6.48),
        ):
            with self.subTest(displaced=displaced):
                self.assertTrue(probe._b10_uncommanded_body_departed(
                    (frame(displaced, False),),
                    verified_command_count=0,
                    start_position=start,
                ))
        self.assertTrue(probe._b10_uncommanded_body_departed(
            (frame((-2.5, -0.02, -6.5), True),),
            verified_command_count=0,
            start_position=start,
        ))

    def test_formal_gates_reject_stale_sources_and_weak_never_return(self):
        complete = {
            "scenario_kind": "baseline",
            "worker_injection": {"stale_result_count": 0},
            "terminal_state": "complete",
            "landed": True,
            "horizontal_error_blocks": 0.0,
            "level_error_blocks": 0.0,
            "horizontal_collisions": 0,
            "damage_points": 0.0,
            "source_released": True,
            "old_manual_intent_selected": False,
            "old_session_intent_selected": False,
            "left_start_support": False,
            "bounded_terminal": True,
            "recorded_control_frames": 1,
            "adopted_frame_count": 1,
        }
        self.assertTrue(probe._b10_coordinator_completed(complete))
        for field in (
            "old_manual_intent_selected",
            "old_session_intent_selected",
            "source_released",
        ):
            with self.subTest(field=field):
                stale = dict(complete)
                stale[field] = False if field == "source_released" else True
                self.assertFalse(probe._b10_coordinator_completed(stale))
        missing_frame = dict(complete)
        missing_frame["recorded_control_frames"] = 0
        self.assertFalse(probe._b10_coordinator_completed(missing_frame))
        for field in (
            "old_manual_intent_selected",
            "old_session_intent_selected",
            "left_start_support",
            "bounded_terminal",
        ):
            with self.subTest(missing=field):
                missing = dict(complete)
                missing.pop(field)
                self.assertFalse(probe._b10_coordinator_completed(missing))

        never = {
            **complete,
            "scenario_kind": "worker_never_return",
            "terminal_state": "failed",
            "terminal_reason": "motion_solver_request_expired",
            "waiting_polls": 1,
            "command_count": 0,
            "verified_command_count": 0,
            "jump_pulses": 0,
            "left_start_support": False,
            "worker_injection": {
                "stale_result_count": 0,
                "held_result_count": 2,
            },
        }
        self.assertTrue(probe._b10_coordinator_never_return_safe(never))
        mutations = (
            {"terminal_reason": "some_other_typed_failure"},
            {"waiting_polls": 0},
            {"verified_command_count": 1},
            {"jump_pulses": 1},
            {"left_start_support": True},
            {"damage_points": 1.0},
            {"source_released": False},
            {"worker_injection": {"stale_result_count": 0, "held_result_count": 1}},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assertFalse(probe._b10_coordinator_never_return_safe({
                    **never,
                    **mutation,
                }))
        for reason in (
            "deadline_exceeded",
            "control_unavailable",
            "BODY_RESPONSIBILITY_UNRESOLVED",
        ):
            with self.subTest(reason=reason):
                unsafe = dict(never)
                unsafe["terminal_reason"] = reason
                self.assertFalse(
                    probe._b10_coordinator_typed_safe_terminal(unsafe)
                )

    def test_stale_session_intent_in_candidate_set_is_detected(self):
        self.assertTrue(probe._b10_stale_intent_seen(
            {"old-session-intent"},
            {"current-session-intent"},
            set(),
            {"old-session-intent"},
        ))

    def _run(
        self,
        *,
        turns: int,
        delay: int | None,
        cancel_mutation: str | None = None,
        bypass_adopt: bool = False,
        max_control_frames: int = 160,
        capture_evidence: bool = False,
        narrow_fixture: bool = False,
    ):
        case = (
            _narrow_b10_gap_case(turns)
            if narrow_fixture else _gap_case(turns)
        )
        clock = [100_000_000]
        backend = CalculatorBackend(
            clock,
            Scene(dict(case.scene.solids), case.scene.volume).with_floor(),
            case.start,
            case.yaw_degrees,
        )
        trace = _RecordingTrace()
        runtime = PlayerRuntimeV1(
            backend,
            trace,
            clock_ns=lambda: clock[0],
        )
        reset = runtime.reset(ResetRequestV0(
            "d114-reset",
            backend.episode,
            "d114-b10-formal-driver",
            1,
            clock[0] + 30_000_000_000,
        ))
        self.assertTrue(reset.succeeded)
        seed_memory(runtime, backend.scene)

        planner = InlinePlannerWorker()
        inline_motion = InlineMotionWorker()
        delayed = probe._MovementTickDelayedMotionWorker(inline_motion)
        session = NavigationSession(
            f"d114-{turns}-{delay}",
            NavigationSessionProfiles.load(CONFIG),
            planner_worker=planner,
            motion_worker=delayed,
            clock_ns=lambda: clock[0],
        )

        # The component backend advances simulated ticks faster than the inbox
        # thread.  This bounded fixture barrier changes no production timing.
        inbox = session._motion_result_inbox
        self.addCleanup(inbox.close, close_worker=False)
        self.addCleanup(session.close)
        original_drain = inbox.drain_once

        def fixture_drain(worker, sequence):
            inbox.attach_worker(worker)
            self.assertTrue(inbox.wait_for_receiver(timeout=10.0))
            return original_drain(worker, sequence)

        inbox.drain_once = fixture_drain

        manual = runtime.register_ordered_source("d114-manual-fixture")
        manual_intent = ActionIntentV1(
            ordered_intent_id(manual, 1),
            manual.source_id,
            manual.episode_id,
            runtime.observation.sequence_id,
            ActionPriorityV0.TASK,
            clock[0],
            clock[0] + 750_000_000,
            movement=MovementV1(),
        )
        runtime.submit_ordered_intent(OrderedIntentV1(
            manual,
            1,
            manual_intent,
        ))

        scenario = probe._B10CoordinatorScenario(
            f"component-{turns}-{delay}",
            "worker_never_return" if delay is None else (
                "baseline" if delay == 0 else "worker_delay"
            ),
            turns,
            0,
            worker_delay_ticks=0 if delay is None else delay,
            never_return=delay is None,
        )
        native_cancel = runtime.cancel_source
        manual_cancel_seen = False

        def mutated_cancel(source_id):
            nonlocal manual_cancel_seen
            if cancel_mutation == "manual" and source_id == manual.source_id:
                return ()
            if cancel_mutation == "session":
                if not manual_cancel_seen and source_id == manual.source_id:
                    manual_cancel_seen = True
                    return native_cancel(source_id)
                return ()
            return native_cancel(source_id)

        runtime.cancel_source = mutated_cancel
        captured = []
        evidence_buffer = None
        temporary = None
        if capture_evidence:
            temporary = TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            evidence_buffer = probe._B10EvidenceBuffer(Path(temporary.name))

            def capture(driver_frame):
                evidence_buffer.collect_receiver(
                    session._motion_result_inbox,
                    scenario.name,
                    episode_id=backend.episode,
                )
                captured.append(probe._capture_b10_formal_driver_frame(
                    driver_frame=driver_frame,
                    runtime=runtime,
                    session=session,
                    trial_name=scenario.name,
                    episode=backend.episode,
                    scenario_kind=scenario.kind,
                    evidence_buffer=evidence_buffer,
                ))
        else:
            capture = None
        try:
            adopt_patch = (
                patch.object(
                    probe.RuntimeNavigationDriver,
                    "adopt_result",
                    return_value=None,
                )
                if bypass_adopt else None
            )
            if adopt_patch is None:
                result = probe._run_b10_formal_driver_scenario(
                    runtime=runtime,
                    session=session,
                    delayed_motion_worker=delayed,
                    scenario=scenario,
                    goal_state=_goal(case.goal),
                    goal_id=f"d114-goal-{turns}-{delay}",
                    goal_revision=1,
                    task=_task(clock[0] + 30_000_000_000),
                    behavior=BehaviorProfileV0(),
                    manual_source=manual,
                    clock_ns=lambda: clock[0],
                    max_control_frames=max_control_frames,
                    after_frame=capture,
                )
            else:
                with adopt_patch:
                    result = probe._run_b10_formal_driver_scenario(
                        runtime=runtime,
                        session=session,
                        delayed_motion_worker=delayed,
                        scenario=scenario,
                        goal_state=_goal(case.goal),
                        goal_id=f"d114-goal-{turns}-{delay}",
                        goal_revision=1,
                        task=_task(clock[0] + 30_000_000_000),
                        behavior=BehaviorProfileV0(),
                        manual_source=manual,
                        clock_ns=lambda: clock[0],
                        max_control_frames=max_control_frames,
                        after_frame=capture,
                    )
        finally:
            runtime.cancel_source = native_cancel
        if runtime.state is RuntimeStateV1.READY:
            runtime.unregister_ordered_source(manual)
        if evidence_buffer is not None:
            evidence_buffer.collect_receiver(
                session._motion_result_inbox,
                scenario.name,
                episode_id=backend.episode,
            )
            self.last_formal_evidence = {
                "buffer": evidence_buffer,
                "captured": tuple(captured),
                "trace_rows": tuple({
                    "record_type": record_type,
                    "payload": trace_projection(payload),
                } for record_type, payload in trace.records),
                "scenario": scenario,
                "case": case,
            }
        return result, runtime, backend

    def test_ten_tick_delivery_never_uses_an_expiring_fixture_or_session_lease(self):
        result, runtime, backend = self._run(turns=0, delay=10)

        self.assertIs(runtime.state, RuntimeStateV1.READY)
        self.assertTrue(result.bounded_terminal, result)
        self.assertNotIn("deadline_exceeded", result.terminal_reason)
        self.assertTrue(result.source_released)
        self.assertFalse(result.manual_intent_selected)
        self.assertFalse(result.old_session_intent_selected)
        self.assertEqual(backend.damage_taken, 0.0)

    def test_goal_state_plans_exact_gap_route_to_supported_target(self):
        result, _runtime, backend = self._run(turns=0, delay=0)

        self.assertIn(
            ("WalkSegment", "JumpGapSegment", "WalkSegment"),
            result.observed_action_routes,
        )
        self.assertIn((0.5, 64.0, 8.5), result.observed_route_endpoints)
        self.assertEqual(
            backend.scene.solids[(0, 63, 8)],
            "minecraft:grass_block",
        )
        self.assertEqual(result.terminal_state, "complete")
        self.assertLessEqual(
            ((backend.state.position[0] - .5) ** 2
             + (backend.state.position[2] - 8.5) ** 2) ** .5,
            .2,
        )

    def test_goal_state_matches_the_real_narrow_b10_fixture_in_four_directions(self):
        for turns in range(4):
            with self.subTest(turns=turns):
                result, runtime, backend = self._run(
                    turns=turns,
                    delay=0,
                    narrow_fixture=True,
                )
                expected_target = _narrow_b10_gap_case(turns).goal
                dx, dz, _ = probe._DIRECTIONS[turns]
                self.assertIn(
                    (dx * 2, 63, dz * 2),
                    backend.scene.solids,
                )
                self.assertIn(
                    ("JumpGapSegment",),
                    result.observed_action_routes,
                )
                self.assertEqual(result.terminal_state, "complete")
                self.assertTrue(result.source_released)
                self.assertTrue(result.bounded_terminal)
                self.assertIs(runtime.state, RuntimeStateV1.READY)
                self.assertEqual(backend.damage_taken, 0.0)
                self.assertLessEqual(
                    ((backend.state.position[0] - expected_target[0]) ** 2
                     + (backend.state.position[2] - expected_target[2]) ** 2)
                    ** .5,
                    .2,
                )

    def test_component_runs_the_v3_evidence_and_formal_gates(self):
        from scripts.analyze_d113_delivery_baseline import group_control_prepare

        for delay in (0, 5, 10, None):
            with self.subTest(delay=delay):
                result, runtime, backend = self._run(
                    turns=0,
                    delay=delay,
                    capture_evidence=True,
                )
                evidence = self.last_formal_evidence
                rows = evidence["buffer"].rows(
                    "b10-coordinator-control-frames.jsonl"
                )
                self.assertEqual(len(rows), result.adopted_frame_count)
                self.assertTrue(all(
                    row["schema_version"]
                        == "mc2p.b10-coordinator-control-frame.v3"
                    for row in rows
                ))
                grouped = group_control_prepare(
                    evidence["trace_rows"],
                    rows,
                    (),
                )
                self.assertTrue(grouped["performance_signable"], grouped)
                receiver = probe._receiver_diagnostic_summary(list(
                    evidence["buffer"].rows(
                        "b10-receiver-diagnostics.jsonl"
                    )
                ))
                self.assertIn("deliveries", receiver)
                self.assertNotIn("delivery", receiver)

                samples = tuple(
                    captured.sample
                    for captured in evidence["captured"]
                    if captured.sample is not None
                )
                delivery_fields = probe._b10_delivery_chain_fields(
                    samples,
                    receiver["deliveries"],
                )
                formal_fields = probe._b10_formal_trial_fields(
                    result,
                    samples=samples,
                    recorded_control_frames=len(rows),
                    start_position=evidence["case"].start,
                )

                target = evidence["case"].goal
                trial = {
                    "scenario_kind": evidence["scenario"].kind,
                    "samples": samples,
                    **delivery_fields,
                    "landed": backend.state.on_ground,
                    "horizontal_error_blocks": (
                        (backend.state.position[0] - target[0]) ** 2
                        + (backend.state.position[2] - target[2]) ** 2
                    ) ** .5,
                    "level_error_blocks": abs(
                        backend.state.position[1] - target[1]
                    ),
                    "horizontal_collisions": sum(
                        int(row["horizontal_collision"])
                        for row in rows
                    ),
                    "damage_points": backend.damage_taken,
                    "waiting_polls": result.waiting_frame_count,
                    **formal_fields,
                }
                evidence["buffer"].append(
                    "b10-coordinator-trials.jsonl",
                    trial,
                )
                self.assertIs(runtime.state, RuntimeStateV1.READY)
                self.assertTrue(
                    probe._b10_coordinator_delivery_gate(trial),
                    trial,
                )
                if delay in (0, 5):
                    self.assertTrue(probe._b10_coordinator_completed(trial))
                elif delay == 10:
                    self.assertTrue(
                        probe._b10_coordinator_completed(trial)
                        or probe._b10_coordinator_typed_safe_terminal(trial)
                    )
                else:
                    self.assertTrue(
                        probe._b10_coordinator_never_return_safe(trial)
                    )

                missing = dict(trial)
                waiting_rows = [
                    row for row in rows if not row["submit_input"]
                ]
                if delay != 0:
                    self.assertTrue(waiting_rows)
                    missing["recorded_control_frames"] -= 1
                else:
                    missing["recorded_control_frames"] = 0
                self.assertFalse(
                    probe._b10_coordinator_completed(missing)
                    or probe._b10_coordinator_typed_safe_terminal(missing)
                )

    def test_forged_release_ack_cannot_replace_actual_source_release(self):
        with patch.object(
            probe.RuntimeNavigationDriver,
            "release",
            return_value=True,
        ):
            result, _runtime, _backend = self._run(turns=0, delay=0)

        self.assertFalse(result.source_released)
        self.assertFalse(result.bounded_terminal)

    def test_four_directions_share_the_formal_driver_for_delivery_and_timeout(self):
        for turns in range(4):
            for delay in (0, 5, 10, None):
                with self.subTest(turns=turns, delay=delay):
                    result, runtime, backend = self._run(
                        turns=turns,
                        delay=delay,
                    )
                    self.assertIs(runtime.state, RuntimeStateV1.READY)
                    self.assertTrue(result.bounded_terminal, result)
                    self.assertTrue(result.source_released)
                    self.assertFalse(result.manual_intent_selected)
                    self.assertFalse(result.old_session_intent_selected)
                    if delay != 0:
                        self.assertGreater(result.waiting_frame_count, 0)
                    self.assertEqual(
                        result.prepared_frame_count,
                        result.adopted_frame_count,
                    )
                    self.assertEqual(result.discarded_frame_count, 0)
                    self.assertEqual(
                        len(result.frames),
                        result.adopted_frame_count,
                    )
                    self.assertTrue(all(
                        frame.movement_tick_after
                        == frame.movement_tick_before + 1
                        for frame in result.frames
                    ))
                    self.assertEqual(backend.damage_taken, 0.0)
                    self.assertTrue(backend.state.on_ground)
                    self.assertNotIn("deadline_exceeded", result.terminal_reason)
                    if delay in (0, 5):
                        self.assertEqual(result.terminal_state, "complete")
                        self.assertEqual(sum(
                            movement.jump for movement in backend.applied
                        ), 1)
                        self.assertLessEqual(
                            abs(backend.state.position[1] - 64.0),
                            .08,
                        )
                    if delay in (5, 10):
                        submissions = result.worker_injection["submissions"]
                        self.assertTrue(submissions)
                        self.assertTrue(all(
                            row["injection_status"] == "matched"
                            and row["actual_delivery_ticks"] == delay
                            and row["first_control_visible_tick"]
                                == row["target_visible_tick"]
                            for row in submissions
                        ), submissions)
                    elif delay is None:
                        self.assertIn(
                            "motion_solver_request_expired",
                            result.terminal_reason,
                        )
                        self.assertFalse(any(
                            movement.jump for movement in backend.applied
                        ))
                        self.assertEqual(
                            result.worker_injection["held_result_count"],
                            2,
                        )

    def test_mutation_leaving_the_manual_lease_is_observable(self):
        result, _runtime, _backend = self._run(
            turns=0,
            delay=10,
            cancel_mutation="manual",
        )

        self.assertTrue(
            result.manual_intent_selected
            or not result.bounded_terminal
            or _runtime.state is not RuntimeStateV1.READY,
            result,
        )

    def test_mutation_leaving_session_leases_cannot_pass_the_safety_gate(self):
        result, runtime, _backend = self._run(
            turns=0,
            delay=10,
            cancel_mutation="session",
        )

        self.assertTrue(
            runtime.state is not RuntimeStateV1.READY
            or result.old_session_intent_selected
            or not result.bounded_terminal,
            result,
        )

    def test_mutation_bypassing_adopt_cannot_prepare_the_next_frame(self):
        with self.assertRaisesRegex(Exception, "prepared frame"):
            self._run(turns=0, delay=5, bypass_adopt=True)

    def test_frame_limit_cannot_return_when_driver_refuses_release(self):
        with patch.object(
            probe.RuntimeNavigationDriver,
            "release",
            return_value=False,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "frame limit.*body responsibility",
            ):
                self._run(
                    turns=0,
                    delay=10,
                    max_control_frames=1,
                )

    def test_runtime_exception_discards_prepared_frame_and_closes_session(self):
        class HarnessBoom(RuntimeError):
            pass

        discarded = []
        closed = []
        native_discard = probe.RuntimeNavigationDriver.discard_prepared
        native_close = NavigationSession.close

        def record_discard(driver):
            discarded.append(driver.prepared_proposal)
            return native_discard(driver)

        def record_close(session):
            closed.append(session.session_id)
            return native_close(session)

        with patch.object(
            PlayerRuntimeV1,
            "control_frame",
            side_effect=HarnessBoom("injected Runtime exception"),
        ), patch.object(
            probe.RuntimeNavigationDriver,
            "discard_prepared",
            record_discard,
        ), patch.object(
            NavigationSession,
            "close",
            record_close,
        ):
            with self.assertRaisesRegex(
                HarnessBoom,
                "injected Runtime exception",
            ):
                self._run(turns=0, delay=5)

        self.assertEqual(len(discarded), 1)
        self.assertIsNotNone(discarded[0])
        self.assertEqual(len(closed), 1)


if __name__ == "__main__":
    unittest.main()
