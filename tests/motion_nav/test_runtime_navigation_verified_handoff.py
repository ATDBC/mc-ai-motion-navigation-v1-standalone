"""Runtime bridge keeps verified motion owned through incomplete evidence."""
from __future__ import annotations

from dataclasses import replace
import time
import unittest

from mc2p.contracts.action_receipt import behavior_receipt_from_mapping
from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.reset import ResetRequestV0, ResetResultV0
from mc2p.motion_nav.navigation_session import (
    NavigationSession,
    NavigationSessionProfiles,
    NavigationSessionState,
)
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.online_motion import InputApplicationStatus
from mc2p.runtime.backend_v1 import BackendStepResultV1
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
from tests.follow_v3_fixtures import follow_snapshot, observed_block
from tests.motion_nav.test_b07_step_route import step_profile
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from tests.motion_nav.test_b09_air_transitions import air_profile
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_navigation_session import _InlinePlanner, _goal
from tests.test_action_receipt import receipt_value
from tests.test_player_runtime import _RecordingTrace


class _GapRuntimeBackend:
    action_schema_version = "mc2p.action-snapshot.v1"
    observation_schema_version = "mc2p.client_observation.v3"

    def __init__(
        self, clock, *, change_landing_on_jump: bool = False,
        apply_jump_one_tick_late: bool = False,
        extra_tick_after_second_airborne_command: bool = False,
    ) -> None:
        self.clock = clock
        self.sequence = 0
        self.movement_tick = 1
        self.airborne = False
        self.change_landing_on_jump = change_landing_on_jump
        self.apply_jump_one_tick_late = apply_jump_one_tick_late
        self.extra_tick_after_second_airborne_command = (
            extra_tick_after_second_airborne_command
        )
        self.airborne_commands = 0
        self.extra_tick_done = False
        self.landing_present = True
        self.actions: list[ActionSnapshotV1] = []

    def _blocks(self):
        support = {(0, 63, 0)}
        if self.landing_present:
            support.add((0, 63, 2))
        return tuple(
            observed_block(
                (x, y, z),
                "minecraft:grass_block" if (x, y, z) in support else "minecraft:air",
                kind="full_cube" if (x, y, z) in support else "empty",
                sources=("surface_depth",) if (x, y, z) in support else ("air_query",),
            )
            for x in range(-2, 3)
            for y in range(60, 71)
            for z in range(-2, 5)
        )

    def observation(self, *, request_sequence_id=None):
        snapshot = follow_snapshot(
            sequence=self.sequence,
            received=self.clock[0],
            position=(.5, 64.0, .5),
            blocks=self._blocks(),
            episode="episode-verified-runtime",
            self_changes={
                "movement_tick_id": self.movement_tick,
                "is_on_ground": not self.airborne,
                "velocity": {
                    "x": 0.0,
                    "y": 0.24 if self.airborne else -0.0784,
                    "z": 0.0,
                },
            },
        )
        return replace(snapshot, request_sequence_id=request_sequence_id)

    def reset(self, request):
        return ResetResultV0(
            request.request_id, request.episode_id, True,
            replace(self.observation(), episode_id=request.episode_id),
        )

    def step(self, action, deadline, *, observation_request=None):
        if type(action) is not ActionSnapshotV1:
            raise AssertionError("wrong action type")
        self.actions.append(action)
        self.sequence += 1
        self.clock[0] += 50_000_000
        active = action.movement != MovementV1()
        if active:
            self.movement_tick += (
                2 if action.movement.jump and self.apply_jump_one_tick_late else 1
            )
        if action.movement.jump:
            self.airborne = True
            if self.change_landing_on_jump:
                self.landing_present = False
        observation = replace(
            self.observation(request_sequence_id=action.request_sequence_id),
            episode_id=action.episode_id,
        )
        applications = ([{
            "schema_version": "mc2p.input-application.v1",
            "movement_tick_id": self.movement_tick,
            "episode_id": action.episode_id,
            "request_sequence_id": action.request_sequence_id,
            "sampled_at_jvm_ns": self.movement_tick,
            "state": "leased",
            "forward": float(action.movement.forward),
            "strafe": float(action.movement.strafe),
            "jump": action.movement.jump,
            "sneak": action.movement.sneak,
            "sprint": action.movement.sprint,
        }] if active else [])
        if active and self.airborne:
            self.airborne_commands += 1
        if (
            self.extra_tick_after_second_airborne_command
            and self.airborne_commands == 2
            and not self.extra_tick_done
        ):
            self.extra_tick_done = True
            self.movement_tick += 1
            observation = replace(
                self.observation(request_sequence_id=action.request_sequence_id),
                episode_id=action.episode_id,
            )
            applications.append({
                "schema_version": "mc2p.input-application.v1",
                "movement_tick_id": self.movement_tick,
                "episode_id": action.episode_id,
                "request_sequence_id": action.request_sequence_id,
                "sampled_at_jvm_ns": self.movement_tick,
                "state": "lease_exhausted",
                "forward": 0.0,
                "strafe": 0.0,
                "jump": False,
                "sneak": False,
                "sprint": False,
            })
        receipt = behavior_receipt_from_mapping({
            **receipt_value(
                episode_id=action.episode_id,
                generation_id=self.sequence,
                request_sequence_id=action.request_sequence_id,
                world_tick=observation.world_time_ticks.value,
                input_samples=self.movement_tick,
                leased_input_samples=1 if active else 0,
            ),
            "schema_version": "mc2p.client_action_receipt.v3",
            "dropped_input_samples": 0,
            "oldest_retained_input_tick": self.movement_tick,
            "input_applications": applications,
        })
        return BackendStepResultV1(observation, 0, False, False, receipt)

    def close(self):
        pass


class _AnchorInjectionSession(NavigationSession):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.drop_next_anchor = False

    def execution_anchor(self, snapshot, ledger):
        if self.drop_next_anchor:
            self.drop_next_anchor = False
            self.ingest(snapshot)
            return None
        return super().execution_anchor(snapshot, ledger)


class RuntimeVerifiedMotionHandoffTests(unittest.TestCase):
    def _running_gap(
        self, *, change_landing_on_jump=False,
        apply_jump_one_tick_late=False,
        extra_tick_after_second_airborne_command=False,
    ):
        clock = [100_000_000]
        backend = _GapRuntimeBackend(
            clock,
            change_landing_on_jump=change_landing_on_jump,
            apply_jump_one_tick_late=apply_jump_one_tick_late,
            extra_tick_after_second_airborne_command=(
                extra_tick_after_second_airborne_command
            ),
        )
        runtime = PlayerRuntimeV1(backend, _RecordingTrace(), lambda: clock[0])
        reset = runtime.reset(ResetRequestV0(
            "reset-verified", "episode-verified-runtime", "test", 1,
            10_000_000_000,
        ))
        self.assertTrue(reset.succeeded)
        profiles = NavigationSessionProfiles(
            replace(
                ordinary_profile(),
                support_materials=frozenset({"minecraft:grass_block"}),
            ),
            jump_profile(), step_profile(),
            air=(air_profile(MovementMode.JUMP_GAP),),
        )
        session = _AnchorInjectionSession(
            "runtime-gap-session", profiles,
            planner_worker=_InlinePlanner(),
            clock_ns=lambda: clock[0],
        )
        driver = RuntimeNavigationDriver(runtime, session, clock_ns=lambda: clock[0])
        driver.start("runtime-gap-goal", 1, _goal((.5, 64.0, 2.5)), clock[0])
        for _ in range(80):
            result = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
            self.assertIsNone(result.report.failure)
            if result.decision is not None and result.decision.action.movement.jump:
                return clock, backend, runtime, session, driver
            time.sleep(.01)
        self.fail("runtime bridge did not submit the verified gap command")

    def test_runtime_bridge_keeps_landing_owner_when_anchor_disappears(self):
        clock, backend, runtime, session, driver = self._running_gap()
        self.addCleanup(runtime.close)
        self.addCleanup(session.close)
        executor = session._executor
        session.drop_next_anchor = True

        result = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)

        self.assertIsNone(result.report.failure)
        self.assertIs(session._executor, executor)
        self.assertEqual(
            session.report.reason,
            "verified_motion_anchor_unavailable_retain_landing",
        )
        self.assertEqual(result.decision.action.movement, MovementV1())

    def test_runtime_bridge_keeps_landing_owner_when_dependency_changes(self):
        clock, backend, runtime, session, driver = self._running_gap(
            change_landing_on_jump=True,
        )
        self.addCleanup(runtime.close)
        self.addCleanup(session.close)
        executor = session._executor

        result = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)

        self.assertIsNone(result.report.failure)
        self.assertIs(session._executor, executor)
        self.assertIn(session.report.state, {
            NavigationSessionState.EXECUTING,
            NavigationSessionState.CANCELLING,
        })
        self.assertIn(session.report.reason, {
            "world_dependency_changed_retain_landing",
            "active_route_dependency_changed",
            "executing_safe_prefix_during_replan",
        })

    def test_runtime_bridge_keeps_verified_two_tick_start_window(self):
        clock, backend, runtime, session, driver = self._running_gap(
            apply_jump_one_tick_late=True,
        )
        self.addCleanup(runtime.close)
        self.addCleanup(session.close)

        submitted = backend.actions[-1]
        record = runtime.input_ledger.record(submitted.request_sequence_id)

        self.assertIsNotNone(record)
        self.assertIs(record.status, InputApplicationStatus.APPLIED)
        self.assertEqual(
            record.latest_allowed_first_tick,
            record.requested_first_tick + 1,
        )

    def test_extra_client_tick_during_jump_recovers_without_failing_runtime(self):
        clock, backend, runtime, session, driver = self._running_gap(
            extra_tick_after_second_airborne_command=True,
        )
        self.addCleanup(runtime.close)
        self.addCleanup(session.close)

        applied_then_skipped = driver.tick(
            BehaviorProfileV0(), clock[0] + 500_000_000,
        )
        recovered = driver.tick(
            BehaviorProfileV0(), clock[0] + 500_000_000,
        )

        self.assertIsNone(applied_then_skipped.report.failure)
        self.assertIsNone(recovered.report.failure)
        self.assertEqual(runtime.state.value, "ready")
        self.assertNotEqual(driver.state, "failed")
        self.assertIsNotNone(driver.source)
        self.assertEqual(recovered.decision.action.movement, MovementV1())
        self.assertEqual(
            session.report.reason,
            "coast_to_verified_landing",
        )


if __name__ == "__main__":
    unittest.main()
