"""D059-D consumer, identity, and same-frame safety contracts."""
from __future__ import annotations

from dataclasses import replace
import unittest
from unittest.mock import patch, PropertyMock

from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
from mc2p.motion_nav.async_work import (
    AsyncComputationScope,
    AsyncWorkIdentity,
    AsyncWorkKind,
)
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.execution_supervisor import ExecutionSupervisor
from mc2p.motion_nav.movement_transition import (
    GoalState,
    GoalSupport,
    MovementMode,
)
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from mc2p.motion_nav.navigation_session import (
    NavigationSession,
    NavigationSessionProfiles,
    NavigationSessionState,
)
from mc2p.motion_nav.online_motion import InputApplicationLedger
from mc2p.motion_nav.retry_ledger import RetryLedger
from mc2p.motion_nav.route_admission import ActiveRouteTracker
from mc2p.motion_nav.route_body_controller import RouteControl
from mc2p.motion_nav.route_validation import (
    ActiveRouteValidationDisposition,
    ActiveRouteValidationReason,
    RouteProgressEvidence,
    WalkValidationQueryKind,
)
from mc2p.motion_nav.runtime_adapter import (
    NavigationObservationAdapter,
    TEST_ORACLE,
)
from mc2p.motion_nav.support_surfaces import StandablePointResult, SurfaceNodeId
from mc2p.motion_nav.world_model import (
    Aabb,
    BlockGeometry,
    CellKnowledge,
    ObservationStamp,
    WorldKnowledge,
)
from tests.motion_nav.test_b07_step_transition import (
    frame as make_frame,
    profile as step_profile,
)
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from tests.motion_nav.test_d059_terminal_selection_dependencies import (
    _admit_large_goal_route,
    _world_with_unselected_unknowns,
)
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_navigation_session import (
    _InlinePlanner,
    _ground_anchor,
    _source,
    _InlineMotionWorker,
)
from tests.observation_v3_fixtures import valid_snapshot_v3


class _DeferredMotionWorker:
    def submit(self, _job):
        return True

    def poll_available(self):
        return ()

    def close(self):
        pass

    def is_alive(self):
        return True


def _snapshot(sequence: int):
    snapshot = valid_snapshot_v3(sequence=sequence)
    own = snapshot.self_state.value
    own = replace(
        own,
        position=Vec3V0(.5, 64.0, 1.5),
        velocity=Vec3V0(0.0, 0.0, 0.0),
        is_on_ground=True,
    )
    return replace(
        snapshot,
        self_state=replace(snapshot.self_state, value=own),
        position=replace(snapshot.position, value=own.position),
        is_on_ground=replace(snapshot.is_on_ground, value=True),
    )


def _goal() -> GoalState:
    return GoalState(
        Aabb(2.2, 63.99, 1.2, 5.8, 64.01, 1.8),
        GoalSupport.SOLID,
        frozenset({MovementMode.WALK}),
        frozenset({"standing"}),
        .6,
    )


def _oracle_session(*, exact_terminal: bool):
    adapter = NavigationObservationAdapter()
    session = NavigationSession(
        "d059-consumer-session",
        NavigationSessionProfiles(
            ordinary_profile(), jump_profile(), step_profile(),
        ),
        planner_worker=_InlinePlanner(), motion_worker=_InlineMotionWorker(),
        clock_ns=lambda: 1_000_000_000,
    )
    session.attach_observation_adapter(adapter)
    initial = session.ingest(_snapshot(1))
    blocks = {
        (x, 63, z): BlockGeometry.full_cube("minecraft:stone")
        for x in range(3)
        for z in range(6)
    }
    selection_only = (3, 64, 1)
    air = tuple(
        (x, y, z)
        for x in range(-1, 6)
        for y in range(62, 68)
        for z in range(-1, 7)
        if (x, y, z) not in blocks and (x, y, z) != selection_only
    )
    initial = adapter.seed_test_oracle_memory(TEST_ORACLE, blocks, air)
    session.bind_source(_source())
    context = (
        patch(
            "mc2p.motion_nav.route_admission.query_standable_connection",
            return_value=StandablePointResult(QueryStatus.BLOCKED),
        )
        if not exact_terminal else None
    )
    with patch.object(
        NavigationSession,
        "_surface_for_goal",
        return_value=(SurfaceNodeId(2, 1, 64, 0), ()),
    ):
        session.start_goal("d059-goal", 1, _goal(), initial)
        if context is None:
            proposal = session.propose(
                initial,
                _ground_anchor(initial),
                2_000_000_000,
                input_ledger=InputApplicationLedger(),
            )
        else:
            with context:
                proposal = session.propose(
                    initial,
                    _ground_anchor(initial),
                    2_000_000_000,
                    input_ledger=InputApplicationLedger(),
                )
    if (exact_terminal
            and proposal.report.state is not NavigationSessionState.EXECUTING):
        raise AssertionError(proposal.report)
    return session, proposal, session.active_route, selection_only


class D059ConsumersAndIdentityTests(unittest.TestCase):
    @staticmethod
    def _requested_dependencies(session, proposal, dependencies):
        # Region proofs add cells; retain the runtime's 16-cell refresh budget
        # and verify successive requests rather than requiring one full page.
        requested = set(proposal.control_frame.observation_request.air_positions)
        for _ in range((len(dependencies) + 15) // 16):
            request = session.observation_request()
            assert len(request.air_positions) <= 16
            requested.update(request.air_positions)
        return requested

    @staticmethod
    def _coordinator(route, executor):
        ledger = RetryLedger("d059-control-identity-task")
        return MotionRouteCoordinator(
            route,
            executor,
            _DeferredMotionWorker(),
            retry_ledger=ledger,
            computation_scope=AsyncComputationScope(
                route.world_session,
                ledger.task_id,
                1,
            ),
        )

    def _supervised_exact_route(self):
        world = _world_with_unselected_unknowns()
        candidate, route = _admit_large_goal_route(
            world,
            row=1,
            goal_revision=1,
        )
        initial = make_frame(world, 2, candidate.path[0].position)
        executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
        )
        executor.start(route.action_route, initial)
        control = RouteControl(route, executor)
        supervisor = ExecutionSupervisor()
        ledger = InputApplicationLedger()
        self.assertTrue(supervisor.offer_route(
            control, initial, ledger, _ground_anchor(initial),
        ))
        terminal = next(
            recipe for recipe in route.validation_plan.recipes
            if recipe.query_kind
                is WalkValidationQueryKind.STANDABLE_CONNECTION
        )
        return world, candidate, control, supervisor, ledger, terminal

    def _assert_same_frame_stop(self, *, dependency_kind: str):
        world, candidate, _, supervisor, ledger, terminal = (
            self._supervised_exact_route()
        )
        if dependency_kind == "support":
            changed = next(
                position for position in terminal.dependencies
                if world.view().cell(position).knowledge
                    is CellKnowledge.BLOCK
            )
            world.confirm_air(
                ObservationStamp(
                    world.session, 3, 3, "test-clock", 150_000_000,
                ),
                (changed,),
            )
        else:
            changed = next(
                position for position in terminal.dependencies
                if position[1] >= 1
                and world.view().cell(position).knowledge
                    is CellKnowledge.AIR
            )
            world.observe_blocks(
                ObservationStamp(
                    world.session, 3, 3, "test-clock", 150_000_000,
                ),
                {changed: BlockGeometry.full_cube("minecraft:stone")},
            )
        current = replace(
            make_frame(world, 3, candidate.path[0].position),
            changed_cells=(changed,),
        )

        advance = supervisor.advance_body(
            current, ledger, _ground_anchor(current),
        )

        self.assertIs(
            advance.route_validation.incumbent.disposition,
            ActiveRouteValidationDisposition.STOP,
        )
        self.assertFalse(advance.route_advance.decision.movement.forward)

    def test_complete_route_identity_matrix_cannot_borrow_validation(self):
        world = _world_with_unselected_unknowns()
        _, route = _admit_large_goal_route(
            world,
            row=1,
            goal_revision=1,
        )
        tracker = ActiveRouteTracker(route)
        identity = tracker.identity
        other_work = AsyncWorkIdentity(
            AsyncComputationScope(
                world.session.value,
                route.goal_id,
                2,
            ),
            "d059-other-owner",
            AsyncWorkKind.PLANNING,
            "d059-other-work",
            1,
        )
        mismatches = {
            "world_session": replace(identity, world_session="other-world"),
            "route_id": replace(identity, route_id="other-route"),
            "route_revision": replace(
                identity, route_revision=identity.route_revision + 1,
            ),
            "source_request_id": replace(
                identity, source_request_id="other-request",
            ),
            "goal_id": replace(identity, goal_id="other-goal"),
            "goal_revision": replace(
                identity, goal_revision=identity.goal_revision + 1,
            ),
            "planning_generation": replace(
                identity,
                planning_generation=identity.planning_generation + 1,
            ),
            "work_identity": replace(identity, work_identity=other_work),
            "action_index": replace(
                identity, action_index=identity.action_index + 1,
            ),
        }

        for field, expected_identity in mismatches.items():
            with self.subTest(field=field):
                result = tracker.validate(
                    world.view(),
                    (),
                    ground_profile=ordinary_profile(),
                    expected_identity=expected_identity,
                )
                self.assertIs(
                    result.disposition,
                    ActiveRouteValidationDisposition.STOP,
                )
                self.assertIs(
                    result.reason,
                    ActiveRouteValidationReason.ROUTE_IDENTITY_CHANGED,
                )
                self.assertEqual(result.identity, identity)

        action = route.action_route.actions[0]
        fixed_route = action.fixed_route.route_id
        wrong_fixed_route = ActiveRouteTracker(route).record_progress(
            0,
            RouteProgressEvidence(
                0,
                fixed_route + "-other",
                .1,
                2,
            ),
            observation_sequence_id=2,
        )
        self.assertIs(
            wrong_fixed_route.disposition,
            ActiveRouteValidationDisposition.STOP,
        )
        self.assertIs(
            wrong_fixed_route.reason,
            ActiveRouteValidationReason.PROGRESS_IDENTITY_MISMATCH,
        )

    def test_old_executor_cannot_be_wrapped_as_a_new_active_route(self):
        _, _, control, _, _, _ = self._supervised_exact_route()
        new_route = replace(
            control.route,
            route_revision=control.route.route_revision + 1,
            action_route=replace(control.route.action_route),
        )

        with self.assertRaises(ContractViolation):
            RouteControl(new_route, control.executor)

    def test_coordinator_route_and_executor_must_match_the_control(self):
        world, candidate, control, _, _, _ = (
            self._supervised_exact_route()
        )
        route = control.route
        executor = control.executor
        other_route = replace(
            route,
            route_id="d059-other-route",
            route_revision=route.route_revision + 1,
            action_route=replace(
                route.action_route,
                route_id="d059-other-route",
            ),
        )
        route_mismatch = self._coordinator(other_route, executor)

        with self.assertRaises(ContractViolation):
            RouteControl(route, executor, route_mismatch)

        other_executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
        )
        other_executor.start(
            route.action_route,
            make_frame(world, 2, candidate.path[0].position),
        )
        executor_mismatch = self._coordinator(route, other_executor)

        with self.assertRaises(ContractViolation):
            RouteControl(route, executor, executor_mismatch)

    def test_matching_incumbent_and_pending_keep_their_own_identities(self):
        world = _world_with_unselected_unknowns()
        first_candidate, first_route = _admit_large_goal_route(
            world,
            row=1,
            goal_revision=1,
        )
        _, second_route = _admit_large_goal_route(
            world,
            row=2,
            goal_revision=2,
        )
        current = make_frame(world, 2, first_candidate.path[0].position)
        first_executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
        )
        first_executor.start(first_route.action_route, current)
        second_executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
        )
        second_executor.start(second_route.action_route, current)
        supervisor = ExecutionSupervisor()
        ledger = InputApplicationLedger()
        anchor = _ground_anchor(current)

        self.assertTrue(supervisor.offer_route(
            RouteControl(first_route, first_executor),
            current,
            ledger,
            anchor,
        ))
        self.assertTrue(supervisor.offer_route(
            RouteControl(second_route, second_executor),
            current,
            ledger,
            anchor,
        ))

        advance = supervisor.advance_body(current, ledger, anchor)

        self.assertIs(
            advance.route_validation.incumbent.disposition,
            ActiveRouteValidationDisposition.UNAFFECTED,
        )
        self.assertIs(
            advance.route_validation.pending.disposition,
            ActiveRouteValidationDisposition.UNAFFECTED,
        )

    def test_exact_consumers_exclude_selection_and_keep_execution_proofs(self):
        session, proposal, route, selection_only = _oracle_session(
            exact_terminal=True,
        )
        try:
            tracker = ActiveRouteTracker(route)
            effective = tracker.effective_dependencies
            exact = next(
                recipe for recipe in route.validation_plan.recipes
                if recipe.query_kind
                    is WalkValidationQueryKind.STANDABLE_CONNECTION
            )
            self.assertNotIn(selection_only, effective)
            self.assertTrue(set(exact.dependencies).issubset(effective))

            requested = self._requested_dependencies(session, proposal, effective)
            self.assertNotIn(selection_only, requested)
            self.assertTrue(
                set(exact.dependencies).issubset(requested)
            )
            self.assertTrue(set(effective).issubset(requested))

            protected = []
            original = WorldKnowledge.set_protection

            def record(owner, center, dependencies):
                protected.append(dependencies)
                return original(owner, center, dependencies)

            with patch.object(WorldKnowledge, "set_protection", new=record):
                session.ingest(_snapshot(2))

            self.assertTrue(protected)
            self.assertEqual(set(protected[-1]), set(effective))
            self.assertNotIn(selection_only, protected[-1])
            self.assertTrue(set(exact.dependencies).issubset(protected[-1]))
        finally:
            session.close()

    def test_unproved_terminal_is_rejected_instead_of_protecting_selection_scan(self):
        session, proposal, route, selection_only = _oracle_session(
            exact_terminal=False,
        )
        try:
            self.assertIs(proposal.report.state, NavigationSessionState.FAILED)
            self.assertEqual(
                proposal.report.reason,
                "goal_standing_point_unavailable",
            )
            self.assertIsNone(route)
            self.assertNotIn(
                selection_only,
                proposal.control_frame.observation_request.air_positions,
            )
        finally:
            session.close()

    def test_selected_terminal_support_removal_stops_before_forward(self):
        self._assert_same_frame_stop(dependency_kind="support")

    def test_selected_terminal_clearance_block_stops_before_forward(self):
        self._assert_same_frame_stop(dependency_kind="clearance")

    def test_current_route_identity_change_stops_before_forward(self):
        world, candidate, control, supervisor, ledger, _ = (
            self._supervised_exact_route()
        )
        current = make_frame(world, 3, candidate.path[0].position)
        wrong = replace(
            control.validation_identity,
            goal_revision=control.validation_identity.goal_revision + 1,
        )

        with patch.object(
            RouteControl,
            "validation_identity",
            new_callable=PropertyMock,
            return_value=wrong,
        ):
            advance = supervisor.advance_body(
                current, ledger, _ground_anchor(current),
            )

        self.assertIs(
            advance.route_validation.incumbent.disposition,
            ActiveRouteValidationDisposition.STOP,
        )
        self.assertIs(
            advance.route_validation.incumbent.reason,
            ActiveRouteValidationReason.ROUTE_IDENTITY_CHANGED,
        )
        self.assertFalse(advance.route_advance.decision.movement.forward)


if __name__ == "__main__":
    unittest.main()
