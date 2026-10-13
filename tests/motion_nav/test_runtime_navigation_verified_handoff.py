"""Runtime bridge keeps verified motion owned through incomplete evidence."""
from __future__ import annotations

from dataclasses import replace
import threading
import time
import unittest
from unittest.mock import patch

from mc2p.contracts.action_receipt import behavior_receipt_from_mapping
from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, ActionSnapshotV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.reset import ResetRequestV0, ResetResultV0
from mc2p.motion_nav.navigation_session import (
    NavigationSession,
    NavigationSessionProfiles,
    NavigationSessionState,
)
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.online_motion import InputApplicationStatus
from mc2p.motion_nav.action_route_executor import ActionRouteDecision, ActionRouteState
from mc2p.motion_nav.motion_worker import MotionSolverWorker
from mc2p.runtime.backend_v1 import BackendStepResultV1
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver, RuntimeNavigationDriverState
from tests.follow_v3_fixtures import follow_snapshot, observed_block
from tests.motion_nav.test_b07_step_route import step_profile
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from tests.motion_nav.test_b09_air_transitions import air_profile
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_navigation_session import _InlinePlanner, _goal
from tests.test_action_receipt import receipt_value
from tests.test_player_runtime import _RecordingTrace


class _FixtureMotionWorker(MotionSolverWorker):
    """Keep real process delivery time outside the fixture's simulated ticks."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._fixture_pending = 0
        self._fixture_results = []

    def submit(self, job):
        accepted = super().submit(job)
        self._fixture_pending += int(accepted)
        return accepted

    def _collect_fixture_results(self):
        results = super().poll_available()
        self._fixture_pending -= len(results)
        self._fixture_results.extend(results)

    def poll_available(self):
        self._collect_fixture_results()
        results = tuple(self._fixture_results)
        self._fixture_results.clear()
        return results

    def await_fixture_delivery(self):
        deadline = time.monotonic() + 5.0
        while self._fixture_pending:
            if not self.is_alive() or time.monotonic() >= deadline:
                raise AssertionError("fixture motion process did not deliver its job")
            self._collect_fixture_results()
            if self._fixture_pending:
                time.sleep(.001)


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
        # Real client ticks and samples continue during neutral preparation.
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
        }])
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
        self.raise_next_proposal_contract = False

    def propose(self, *args, **kwargs):
        if self.raise_next_proposal_contract:
            self.raise_next_proposal_contract = False
            raise ContractViolation("injected illegal transition")
        return super().propose(*args, **kwargs)

    def execution_anchor(self, snapshot, ledger):
        if self.drop_next_anchor:
            self.drop_next_anchor = False
            self.ingest(snapshot)
            return None
        return super().execution_anchor(snapshot, ledger)


class RuntimeDecisionWindowTests(unittest.TestCase):
    def session_fixture(self):
        clock = [100_000_000]
        backend = _GapRuntimeBackend(clock)
        backend.movement_tick = 10
        runtime = PlayerRuntimeV1(backend, _RecordingTrace(), lambda: clock[0])
        runtime.reset(ResetRequestV0(
            "reset-window", "episode-verified-runtime", "test", 1, 2_000_000_000,
        ))
        session = NavigationSession(
            "decision-window", NavigationSessionProfiles(ordinary_profile(), jump_profile(), step_profile()),
            planner_worker=_InlinePlanner(), clock_ns=lambda: clock[0],
        )
        session.bind_source(runtime.register_ordered_source("decision-window"))
        session.ingest(runtime.observation)
        self.addCleanup(runtime.close)
        self.addCleanup(session.close)
        return clock, backend, runtime, session

    @staticmethod
    def decision(**changes):
        return replace(ActionRouteDecision(
            ActionRouteState.RUNNING, MovementV1(forward=1), None, 1, 0,
            "submit_verified_command", (), 0, verified_command_index=0,
            expected_movement_tick=11, latest_movement_tick=12,
        ), **changes)

    def propose(self, session, decision):
        return session._proposal(
            decision.movement, decision.look, decision.input_lease_ticks,
            session._clock() + 1_000_000_000, route_decision=decision,
        )

    def test_half_window_is_rejected_without_exact_tick_fallback(self):
        _, backend, _, session = self.session_fixture()
        for endpoints in ((11, None), (None, 12)):
            with self.subTest(endpoints=endpoints), self.assertRaises(ContractViolation):
                self.propose(session, self.decision(
                    expected_movement_tick=endpoints[0], latest_movement_tick=endpoints[1],
                ))
        self.assertEqual(backend.actions, [])
        self.assertIsNotNone(session._source)

    def test_verified_command_without_both_endpoints_is_rejected(self):
        _, _, _, session = self.session_fixture()
        with self.assertRaises(ContractViolation):
            self.propose(session, self.decision(expected_movement_tick=None, latest_movement_tick=None))

    def test_invalid_start_window_order_width_and_endpoint_types_are_rejected(self):
        _, _, _, session = self.session_fixture()
        for endpoints in ((12, 11), (11, 13), (True, 12), (11, 12.0), (-1, 0)):
            with self.subTest(endpoints=endpoints), self.assertRaises(ContractViolation):
                self.propose(session, self.decision(
                    expected_movement_tick=endpoints[0], latest_movement_tick=endpoints[1],
                ))

    def test_expired_window_is_rejected_before_send_and_keeps_source(self):
        _, backend, _, session = self.session_fixture()
        source = session._source
        with self.assertRaisesRegex(ContractViolation, "expired"):
            self.propose(session, self.decision(expected_movement_tick=9, latest_movement_tick=10))
        self.assertEqual(backend.actions, [])
        self.assertIs(session._source, source)

    def test_another_movement_winner_does_not_borrow_verified_start_slack(self):
        clock, backend, runtime, session = self.session_fixture()
        proposal = self.propose(session, self.decision())
        runtime.submit_intent(ActionIntentV1(
            "safety-winner", "safety", "episode-verified-runtime", runtime.observation.sequence_id,
            ActionPriorityV0.SAFETY, clock[0], clock[0] + 1_000_000_000,
            movement=MovementV1(strafe=1),
        ))
        from tests.test_player_runtime import _task
        result = runtime.control_frame(
            _task(clock[0] + 1_000_000_000), BehaviorProfileV0(), clock[0] + 1_000_000_000,
            proposals=(proposal.control_frame,),
        )
        self.assertEqual(backend.actions[-1].movement, MovementV1(strafe=1))
        self.assertIsNone(result.decision.movement_tick_window)
        record = runtime.input_ledger.record(backend.actions[-1].request_sequence_id)
        self.assertIs(record.status, InputApplicationStatus.APPLIED)
        self.assertEqual((record.requested_first_tick, record.latest_allowed_first_tick), (11, 11))

    def test_ordinary_unverified_input_keeps_exact_next_tick_default(self):
        clock, backend, runtime, session = self.session_fixture()
        proposal = self.propose(session, self.decision(
            verified_command_index=None, expected_movement_tick=None, latest_movement_tick=None,
        ))
        from tests.test_player_runtime import _task
        result = runtime.control_frame(
            _task(clock[0] + 1_000_000_000), BehaviorProfileV0(), clock[0] + 1_000_000_000,
            proposals=(proposal.control_frame,),
        )
        self.assertIsNone(result.decision.movement_tick_window)
        record = runtime.input_ledger.record(backend.actions[-1].request_sequence_id)
        self.assertEqual((record.requested_first_tick, record.latest_allowed_first_tick), (11, 11))


class RuntimeVerifiedMotionHandoffTests(unittest.TestCase):
    def test_fixture_waits_for_unique_receiver_publication_before_next_tick(self):
        from mc2p.motion_nav.motion_worker import MotionResultInbox
        from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
        reached, resume, waiting = threading.Event(), threading.Event(), threading.Event()
        outputs, failures, backends, publications, grants = [], [], [], [], []
        native_backend = _GapRuntimeBackend.__init__
        native_publish = MotionResultInbox._process_legacy
        native_wait = MotionResultInbox.wait_for_receiver
        native_accept = MotionRouteCoordinator._accept_result
        def backend_created(backend, *args, **kwargs):
            native_backend(backend, *args, **kwargs)
            backends.append(backend)
        def held_publication(inbox, result):
            if not reached.is_set():
                reached.set()
                if not resume.wait(5.):
                    raise AssertionError("receiver publication barrier was not released")
            native_publish(inbox, result)
            publications.append((inbox, threading.get_ident(), result,
                                 inbox.peek(result.work_identity)))
        def receiver_wait(inbox, **kwargs):
            waiting.set()
            return native_wait(inbox, **kwargs)
        def grant_checked(owner, result, anchor, *args, **kwargs):
            accepted = native_accept(owner, result, anchor, *args, **kwargs)
            proof = result.solve_result.proof
            grants.append((accepted, anchor.movement_tick_id, proof.execution_window))
            return accepted
        def run_fixture():
            try:
                outputs.append(self._running_gap(apply_jump_one_tick_late=True))
            except BaseException as error:
                failures.append(error)
        with patch.object(_GapRuntimeBackend, "__init__", backend_created), \
                patch.object(MotionResultInbox, "_process_legacy", held_publication), \
                patch.object(MotionResultInbox, "wait_for_receiver", receiver_wait), \
                patch.object(MotionRouteCoordinator, "_accept_result", grant_checked):
            runner = threading.Thread(target=run_fixture, name="fixture-clock-driver")
            runner.start()
            try:
                self.assertTrue(reached.wait(5.), "the original receiver did not receive the process result")
                self.assertTrue(waiting.wait(2.), "fixture advanced ticks before waiting for receiver publication")
                self.assertEqual(backends[0].movement_tick, 3)
                self.assertEqual(publications, [])
            finally:
                resume.set()
                runner.join(10.)
        self.assertFalse(runner.is_alive())
        if failures:
            raise failures[0]
        self.assertEqual(len(outputs), 1)
        _, backend, runtime, session, _ = outputs[0]
        self.addCleanup(runtime.close)
        self.addCleanup(session._motion_result_inbox.close)
        self.addCleanup(session.close)
        inbox = session._motion_result_inbox
        self.assertEqual({thread_id for _, thread_id, _, _ in publications}, {inbox.receiver_thread_id})
        self.assertTrue(all(source is inbox and published is result
                            for source, _, result, published in publications))
        self.assertEqual([(tick, window.earliest_start_tick, window.latest_start_tick)
                          for accepted, tick, window in grants if accepted], [(3, 4, 5)])
        self.assertEqual(backend.movement_tick, 5)

    def _running_gap(
        self, *, change_landing_on_jump=False,
        apply_jump_one_tick_late=False,
        extra_tick_after_second_airborne_command=False,
        fixture_worker_type=_FixtureMotionWorker,
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
        # This setup verifies body ownership, not operating-system scheduling.
        # Solve in the real process, but expose each result on the next
        # simulated frame. Dedicated delivery tests inject client-tick delays.
        with patch("mc2p.motion_nav.navigation_session.MotionSolverWorker",
                   fixture_worker_type):
            for _ in range(80):
                result = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
                self.assertIsNone(result.report.failure)
                self.assertFalse(session.report.terminal, session.report.reason)
                if result.decision is not None and result.decision.action.movement.jump:
                    return clock, backend, runtime, session, driver
                worker = session._motion_worker
                if worker is not None:
                    worker.await_fixture_delivery()
                    # A ready worker result may still be in this fixture's
                    # delivery buffer. The same receiver must publish it
                    # before the next simulated client tick consumes slack.
                    self.assertTrue(session._motion_result_inbox.wait_for_receiver(timeout=5.))
        self.fail("runtime bridge did not submit the verified gap command")

    def test_fixture_delivery_delay_does_not_spend_simulated_motion_window(self):
        class DelayedDeliveryWorker(_FixtureMotionWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.deferred_polls = 0

            def _collect_fixture_results(self):
                if self._fixture_pending and self.deferred_polls < 100:
                    self.deferred_polls += 1
                    return
                super()._collect_fixture_results()

        clock, backend, runtime, session, driver = self._running_gap(
            fixture_worker_type=DelayedDeliveryWorker,
        )
        self.addCleanup(runtime.close)
        self.addCleanup(session.close)
        self.assertEqual(session._motion_worker.deferred_polls, 100)
        self.assertTrue(backend.airborne)
        self.assertLess(backend.movement_tick, 20)
        self.assertIsNone(runtime.last_failure_disposition)

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
        self.assertNotEqual(driver.state, RuntimeNavigationDriverState.FAILED)
        self.assertIsNotNone(driver.source)
        self.assertEqual(recovered.decision.action.movement, MovementV1())
        self.assertEqual(
            session.report.reason,
            "coast_to_verified_landing",
        )

    def test_formal_driver_converts_internal_contract_error_to_safe_stop(self):
        clock, backend, runtime, session, driver = self._running_gap()
        self.addCleanup(runtime.close)
        self.addCleanup(session.close)
        session.raise_next_proposal_contract = True

        result = driver.tick(
            BehaviorProfileV0(), clock[0] + 500_000_000,
        )

        self.assertIsNone(result.report.failure)
        self.assertEqual(runtime.state.value, "ready")
        self.assertIn(driver.state, {RuntimeNavigationDriverState.STOPPING, RuntimeNavigationDriverState.FAILED})
        self.assertIsNotNone(driver.source)
        self.assertIs(session.report.state, NavigationSessionState.CANCELLING)
        self.assertIsNotNone(session._executor)


if __name__ == "__main__":
    unittest.main()
