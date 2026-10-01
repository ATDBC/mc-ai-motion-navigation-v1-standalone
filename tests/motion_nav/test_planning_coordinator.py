from __future__ import annotations

import unittest
from dataclasses import replace

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.async_work import (
    AsyncAdmissionDisposition,
    AsyncWorkKind,
)
from mc2p.motion_nav.known_map_planner import (
    PlanningBlocker,
    PlanningBlockerKind,
    PlanningFrontierKind,
    PlanningFactRequirement,
    PlanningFactRequirementKind,
    SurfacePlanningRequest,
    plan_known_surface_snapshot,
)
from mc2p.motion_nav.planning_coordinator import (
    InformationOutcome,
    PlanningAttemptPermit,
    PlanningAttemptPermitKind,
    PlanningCapabilities,
    PlanningCoordinator,
    PlanningUpdateKind,
)
from mc2p.motion_nav.retry_ledger import RetryCause, RetryLedger
from mc2p.motion_nav.route_admission import RouteAdmitter
from mc2p.motion_nav.support_surfaces import query_support_surfaces
from mc2p.motion_nav.world_model import (
    BlockGeometry,
    ObservationStamp,
    WorldKnowledge,
    WorldSessionId,
)
from tests.motion_nav.test_b07_step_route import frame, step_profile
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_navigation_session import _InlinePlanner


class _DeferredPlanner:
    def __init__(self, *, expand_information: bool = False) -> None:
        self._candidate = None
        self._defer = False
        self.expand_information = expand_information

    def submit_surface_snapshot(
        self, snapshot, ground, step, request, jump, *,
        air_profiles=(), ground_mode_profile=None,
    ) -> bool:
        candidate = plan_known_surface_snapshot(
            snapshot, ground, step, request, jump,
            air_profiles=air_profiles,
            ground_mode_profile=ground_mode_profile,
        )
        if self.expand_information and candidate.information_need is not None:
            blockers = tuple(
                PlanningBlocker(
                    (index, 2, 0),
                    PlanningBlockerKind.CLEARANCE,
                    "test-clearance",
                    PlanningFrontierKind.SEARCH_EDGE,
                    f"frontier-{index}",
                )
                for index in range(65)
            )
            candidate = replace(
                candidate,
                information_need=replace(
                    candidate.information_need,
                    blockers=blockers,
                    truncated=False,
                ),
            )
        self._candidate = candidate
        self._defer = True
        return True

    def submit_snapshot(self, *_args, **_kwargs) -> bool:
        raise AssertionError("legacy planning is not used in this test")

    def poll_latest(self):
        if self._defer:
            self._defer = False
            return None
        candidate = self._candidate
        self._candidate = None
        return candidate

    def is_alive(self) -> bool:
        return True

    def close(self) -> None:
        return None


class _NeverResultPlanner(_DeferredPlanner):
    def poll_latest(self):
        return None


def _world(*, unknown: frozenset[tuple[int, int, int]] = frozenset()):
    session = WorldSessionId("planning-coordinator-world")
    world = WorldKnowledge(session)
    stamp = ObservationStamp(session, 1, 1, "test-clock", 1)
    world.confirm_air(stamp, tuple(
        (x, y, z)
        for x in range(-2, 3)
        for y in range(-2, 4)
        for z in range(-1, 2)
        if (x, y, z) not in unknown
    ))
    world.observe_blocks(stamp, {
        (x, 0, 0): BlockGeometry.full_cube("minecraft:stone")
        for x in range(-1, 2)
        if (x, 0, 0) not in unknown
    })
    return world


def _request(world: WorldKnowledge) -> SurfacePlanningRequest:
    start = query_support_surfaces(world.view(), -1, 0, 1, 1).surfaces[0]
    goal = query_support_surfaces(world.view(), 1, 0, 1, 1).surfaces[0]
    return SurfacePlanningRequest(
        1, "coordinator-request-1", "coordinator-goal", 1,
        world.session.value, start.node_id, goal.node_id,
    )


def _permit() -> PlanningAttemptPermit:
    return PlanningAttemptPermit(
        "coordinator-permit-1",
        "coordinator-task",
        1,
        "goal-revision-1",
        PlanningAttemptPermitKind.TASK_UPDATE,
    )


class PlanningCoordinatorTests(unittest.TestCase):
    def coordinator(
        self, planner=None, *, snapshot_cells_per_step=10_000,
        clock_ns=lambda: 1_000_000_000,
    ) -> PlanningCoordinator:
        return PlanningCoordinator(
            "coordinator-task",
            PlanningCapabilities(
                ordinary_profile(), step_profile(), jump_profile(), (), None,
            ),
            planner_worker=planner or _InlinePlanner(),
            route_admitter=RouteAdmitter(),
            retry_ledger=RetryLedger("coordinator-task"),
            clock_ns=clock_ns,
            snapshot_cells_per_step=snapshot_cells_per_step,
        )

    def test_builds_snapshot_submits_and_admits_route(self):
        world = _world()
        request = _request(world)
        coordinator = self.coordinator()
        current = frame(
            world, 0,
            query_support_surfaces(world.view(), -1, 0, 1, 1).surfaces[0].position,
        )

        coordinator.begin(
            request, current, permit=_permit(), state_anchor=None,
            remaining_damage_budget=request.damage_budget,
        )
        update = coordinator.advance(
            current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget,
        )

        self.assertIs(update.kind, PlanningUpdateKind.ROUTE_READY)
        self.assertIsNotNone(update.route)
        self.assertEqual(update.request_id, request.request_id)
        self.assertEqual(update.goal_revision, request.goal_revision)

    def test_returns_only_search_frontier_information(self):
        external = (-2, -1, -1)
        gap = (0, 0, 0)
        world = _world(unknown=frozenset({external, gap}))
        request = _request(world)
        coordinator = self.coordinator()
        current = frame(
            world, 0,
            query_support_surfaces(world.view(), -1, 0, 1, 1).surfaces[0].position,
        )
        coordinator.begin(
            request, current, permit=_permit(), state_anchor=None,
            remaining_damage_budget=request.damage_budget,
        )

        update = coordinator.advance(
            current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget,
        )

        self.assertIs(update.kind, PlanningUpdateKind.NEEDS_INFORMATION)
        self.assertEqual(
            tuple(blocker.position for blocker in update.information_need.blockers),
            (gap,),
        )
        self.assertTrue(
            coordinator.diagnostics(current).information_identity_valid,
        )
        # Invalid metadata cannot become an owned notification in the new contract.
        with self.assertRaises(ContractViolation):
            replace(update, information_need=replace(update.information_need,
                                                     request_id="foreign-request"))
        self.assertIs(coordinator.current_information_update, update)
        self.assertTrue(coordinator.diagnostics(current).information_identity_valid)

    def test_information_acquired_can_only_enter_through_typed_fact_query(self):
        gap = (0, 0, 0)
        world = _world(unknown=frozenset({gap}))
        request = _request(world)
        coordinator = self.coordinator()
        current = frame(
            world, 0,
            query_support_surfaces(world.view(), -1, 0, 1, 1)
            .surfaces[0].position,
        )
        coordinator.begin(
            request, current, permit=_permit(), state_anchor=None,
            remaining_damage_budget=request.damage_budget,
        )
        update = coordinator.advance(
            current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget,
        )
        blocker = update.information_need.blockers[0]

        with self.assertRaises(ContractViolation):
            coordinator.report_information_outcome(
                update.information_need.selection_revision,
                blocker.blocker_key,
                InformationOutcome.ACQUIRED,
                observation_sequence=current.body.sequence_id,
            )
        self.assertIsNotNone(coordinator.confirm_information_fact(
            update.information_need.selection_revision,
            blocker.blocker_key,
            current,
            edge_probe=None,
        ))

        world.observe_blocks(ObservationStamp(
            world.session, 2, 2, "test-clock", 2,
        ), {gap: BlockGeometry.full_cube("minecraft:stone")})
        current = frame(world, 1, current.body.position)
        information_identity = coordinator.work_identity
        self.assertIsNone(coordinator.confirm_information_fact(
            update.information_need.selection_revision,
            blocker.blocker_key,
            current,
            edge_probe=None,
        ))
        self.assertFalse(coordinator.has_owned_work)
        self.assertIsNone(coordinator.work_identity)
        self.assertEqual(
            coordinator.last_admission.identity, information_identity,
        )
        self.assertIs(
            coordinator.last_admission.disposition,
            AsyncAdmissionDisposition.APPLIED,
        )

    def test_attempt_permit_is_one_shot(self):
        world = _world()
        request = _request(world)
        coordinator = self.coordinator()
        current = frame(
            world, 0,
            query_support_surfaces(world.view(), -1, 0, 1, 1).surfaces[0].position,
        )
        permit = _permit()
        coordinator.begin(
            request, current, permit=permit, state_anchor=None,
            remaining_damage_budget=request.damage_budget,
        )

        with self.assertRaises(ContractViolation):
            coordinator.begin(
                request, current, permit=permit, state_anchor=None,
                remaining_damage_budget=request.damage_budget,
            )

    def test_foreign_or_stale_permit_cannot_start_attempt(self):
        world = _world()
        request = _request(world)
        current = frame(
            world, 0,
            query_support_surfaces(world.view(), -1, 0, 1, 1).surfaces[0].position,
        )
        for permit in (
            replace(_permit(), permit_id="foreign-task", task_id="other-task"),
            replace(_permit(), permit_id="stale-goal", goal_revision=0),
        ):
            with self.subTest(permit=permit.permit_id):
                with self.assertRaises(ContractViolation):
                    self.coordinator().begin(
                        request, current, permit=permit, state_anchor=None,
                        remaining_damage_budget=request.damage_budget,
                    )

    def test_diagnostics_bind_work_to_request_permit_and_deadline(self):
        world = _world()
        request = _request(world)
        coordinator = self.coordinator(_DeferredPlanner())
        current = frame(
            world, 0,
            query_support_surfaces(world.view(), -1, 0, 1, 1).surfaces[0].position,
        )
        coordinator.begin(
            request, current, permit=_permit(), state_anchor=None,
            remaining_damage_budget=request.damage_budget,
        )
        building = coordinator.diagnostics(current)
        self.assertTrue(building.has_owned_work)
        self.assertTrue(building.work_identity_valid)
        self.assertTrue(building.permit_identity_valid)

        coordinator.advance(
            current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget,
        )
        submitted = coordinator.diagnostics(current)
        self.assertTrue(submitted.has_owned_work)
        self.assertTrue(submitted.work_identity_valid)
        self.assertEqual(submitted.submitted_request_id, request.request_id)

    def test_unavailable_first_batch_advances_to_sixty_fifth_fact(self):
        gap = (0, 0, 0)
        world = _world(unknown=frozenset({gap}))
        request = _request(world)
        coordinator = self.coordinator(_DeferredPlanner(expand_information=True))
        current = frame(
            world, 0,
            query_support_surfaces(world.view(), -1, 0, 1, 1).surfaces[0].position,
        )
        coordinator.begin(
            request, current, permit=_permit(), state_anchor=None,
            remaining_damage_budget=request.damage_budget,
        )
        self.assertIs(
            coordinator.advance(
                current, state_anchor=None, edge_probe=None,
                remaining_damage_budget=request.damage_budget,
            ).kind,
            PlanningUpdateKind.RUNNING,
        )
        update = coordinator.advance(
            current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget,
        )
        self.assertEqual(len(update.information_need.blockers), 64)

        for blocker in update.information_need.blockers:
            update = coordinator.report_information_outcome(
                update.information_need.selection_revision,
                blocker.blocker_key,
                InformationOutcome.CURRENTLY_UNAVAILABLE,
            )

        self.assertIs(update.kind, PlanningUpdateKind.NEEDS_INFORMATION)
        self.assertEqual(len(update.information_need.blockers), 1)
        self.assertEqual(update.information_need.blockers[0].position, (64, 2, 0))

    def test_dependency_changes_consume_shared_retry_limit(self):
        world = _world()
        request = _request(world)
        planner = _DeferredPlanner()
        coordinator = self.coordinator(planner)
        start_position = query_support_surfaces(
            world.view(), -1, 0, 1, 1,
        ).surfaces[0].position
        current = frame(world, 0, start_position)
        coordinator.begin(
            request, current, permit=_permit(), state_anchor=None,
            remaining_damage_budget=request.damage_budget,
        )

        last = None
        for index in range(3):
            submitted = coordinator.advance(
                current, state_anchor=None, edge_probe=None,
                remaining_damage_budget=request.damage_budget,
            )
            self.assertIs(submitted.kind, PlanningUpdateKind.RUNNING)
            world.observe_blocks(ObservationStamp(
                world.session, index + 2, index + 2, "test-clock", index + 2,
            ), {(0, 0, 0): BlockGeometry.full_cube(
                "minecraft:smooth_stone_slab"
                if index % 2 == 0 else "minecraft:stone"
            )})
            current = frame(world, index + 1, start_position)
            coordinator.observe_changes(((0, 0, 0),))
            last = coordinator.advance(
                current, state_anchor=None, edge_probe=None,
                remaining_damage_budget=request.damage_budget,
            )
            if index < 2:
                self.assertIs(last.kind, PlanningUpdateKind.RUNNING)

        self.assertIs(last.kind, PlanningUpdateKind.FAILED)
        self.assertEqual(
            last.failure.reason,
            "route_dependencies_changed_retry_exhausted",
        )
        self.assertEqual(
            coordinator._retry_ledger.count_for(RetryCause.DEPENDENCY), 3,
        )

    def test_planning_timeout_uses_shared_limit_and_has_typed_terminal(self):
        clock = [1_000_000_000]
        world = _world()
        request = _request(world)
        coordinator = self.coordinator(
            _NeverResultPlanner(), clock_ns=lambda: clock[0],
        )
        current = frame(
            world, 0,
            query_support_surfaces(world.view(), -1, 0, 1, 1).surfaces[0].position,
        )
        coordinator.begin(
            request, current, permit=_permit(), state_anchor=None,
            remaining_damage_budget=request.damage_budget,
        )

        last = None
        for index in range(3):
            submitted = coordinator.advance(
                current, state_anchor=None, edge_probe=None,
                remaining_damage_budget=request.damage_budget,
            )
            self.assertIs(submitted.kind, PlanningUpdateKind.RUNNING)
            clock[0] += 1_000_000_000
            last = coordinator.advance(
                current, state_anchor=None, edge_probe=None,
                remaining_damage_budget=request.damage_budget,
            )
            if index < 2:
                self.assertEqual(last.reason, "planning_timeout_retry_started")

        self.assertIs(last.kind, PlanningUpdateKind.FAILED)
        self.assertEqual(last.failure.reason, "planning_timeout_retry_exhausted")
        self.assertEqual(
            coordinator._retry_ledger.count_for(RetryCause.PLANNING), 3,
        )

    def test_snapshot_unknown_becoming_known_restarts_without_retry(self):
        first_cell = (-2, -1, -1)
        world = _world(unknown=frozenset({first_cell}))
        request = _request(world)
        coordinator = self.coordinator(snapshot_cells_per_step=1)
        start_position = query_support_surfaces(
            world.view(), -1, 0, 1, 1,
        ).surfaces[0].position
        current = frame(world, 0, start_position)
        coordinator.begin(
            request, current, permit=_permit(), state_anchor=None,
            remaining_damage_budget=request.damage_budget,
        )
        self.assertEqual(
            coordinator.advance(
                current, state_anchor=None, edge_probe=None,
                remaining_damage_budget=request.damage_budget,
            ).reason,
            "snapshot_building",
        )
        world.confirm_air(ObservationStamp(
            world.session, 2, 2, "test-clock", 2,
        ), (first_cell,))
        current = frame(world, 1, start_position)
        coordinator.observe_changes((first_cell,))

        update = coordinator.advance(
            current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget,
        )

        self.assertEqual(update.reason, "snapshot_restarted")
        self.assertEqual(
            coordinator._retry_ledger.count_for(RetryCause.DEPENDENCY), 0,
        )

    def test_same_section_change_outside_bounds_does_not_consume_retry(self):
        world = _world()
        request = _request(world)
        coordinator = self.coordinator(snapshot_cells_per_step=1)
        start_position = query_support_surfaces(
            world.view(), -1, 0, 1, 1,
        ).surfaces[0].position
        current = frame(world, 0, start_position)
        coordinator.begin(
            request, current, permit=_permit(), state_anchor=None,
            remaining_damage_budget=request.damage_budget,
        )
        coordinator.advance(
            current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget,
        )
        outside = (5, 0, 0)
        world.observe_blocks(ObservationStamp(
            world.session, 2, 2, "test-clock", 2,
        ), {outside: BlockGeometry.full_cube("minecraft:stone")})
        current = frame(world, 1, start_position)
        coordinator.observe_changes((outside,))

        update = coordinator.advance(
            current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget,
        )

        self.assertEqual(update.reason, "snapshot_building")
        self.assertEqual(
            coordinator._retry_ledger.count_for(RetryCause.DEPENDENCY), 0,
        )

    def test_snapshot_building_uses_attempt_deadline_despite_restarts(self):
        clock = [1_000_000_000]
        world = _world()
        request = replace(_request(world), maximum_planning_seconds=.05)
        coordinator = self.coordinator(
            snapshot_cells_per_step=1, clock_ns=lambda: clock[0],
        )
        start_position = query_support_surfaces(
            world.view(), -1, 0, 1, 1,
        ).surfaces[0].position
        current = frame(world, 0, start_position)
        coordinator.begin(
            request, current, permit=_permit(), state_anchor=None,
            remaining_damage_budget=request.damage_budget,
        )
        self.assertEqual(
            coordinator.advance(
                current, state_anchor=None, edge_probe=None,
                remaining_damage_budget=request.damage_budget,
            ).reason,
            "snapshot_building",
        )

        outside = (5, 0, 0)
        world.observe_blocks(ObservationStamp(
            world.session, 2, 2, "test-clock", 2,
        ), {outside: BlockGeometry.full_cube("minecraft:stone")})
        coordinator.observe_changes((outside,))
        clock[0] += 100_000_000
        current = frame(world, 1, start_position)

        expired = coordinator.advance(
            current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget,
        )

        self.assertEqual(expired.reason, "planning_timeout_retry_started")
        self.assertEqual(
            coordinator._retry_ledger.count_for(RetryCause.PLANNING), 1,
        )

    def test_information_result_is_rebased_when_fact_became_known(self):
        gap = (0, 0, 0)
        world = _world(unknown=frozenset({gap}))
        request = _request(world)
        planner = _DeferredPlanner()
        coordinator = self.coordinator(planner)
        start_position = query_support_surfaces(
            world.view(), -1, 0, 1, 1,
        ).surfaces[0].position
        current = frame(world, 0, start_position)
        coordinator.begin(
            request, current, permit=_permit(), state_anchor=None,
            remaining_damage_budget=request.damage_budget,
        )
        self.assertEqual(
            coordinator.advance(
                current, state_anchor=None, edge_probe=None,
                remaining_damage_budget=request.damage_budget,
            ).reason,
            "planning_submitted",
        )

        world.observe_blocks(ObservationStamp(
            world.session, 2, 2, "test-clock", 2,
        ), {gap: BlockGeometry.full_cube("minecraft:stone")})
        coordinator.observe_changes((gap,))
        current = frame(world, 1, start_position)
        rebased = coordinator.advance(
            current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget,
        )

        self.assertIs(rebased.kind, PlanningUpdateKind.RUNNING)
        self.assertEqual(rebased.reason, "planning_information_updated")
        self.assertIsNone(coordinator.current_information_need)

    def test_no_route_result_is_rebased_when_snapshot_scope_changed(self):
        gap = (0, 0, 0)
        world = _world()
        world.confirm_air(ObservationStamp(
            world.session, 2, 2, "test-clock", 2,
        ), (gap,))
        request = _request(world)
        planner = _DeferredPlanner()
        coordinator = self.coordinator(planner)
        start_position = query_support_surfaces(
            world.view(), -1, 0, 1, 1,
        ).surfaces[0].position
        current = frame(world, 0, start_position)
        coordinator.begin(
            request, current, permit=_permit(), state_anchor=None,
            remaining_damage_budget=request.damage_budget,
        )
        self.assertEqual(
            coordinator.advance(
                current, state_anchor=None, edge_probe=None,
                remaining_damage_budget=request.damage_budget,
            ).reason,
            "planning_submitted",
        )

        world.observe_blocks(ObservationStamp(
            world.session, 3, 3, "test-clock", 3,
        ), {gap: BlockGeometry.full_cube("minecraft:stone")})
        coordinator.observe_changes((gap,))
        current = frame(world, 1, start_position)
        rebased = coordinator.advance(
            current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget,
        )

        self.assertIs(rebased.kind, PlanningUpdateKind.RUNNING)
        self.assertEqual(rebased.reason, "route_dependencies_changed_retry_started")
        self.assertEqual(
            coordinator._retry_ledger.count_for(RetryCause.DEPENDENCY), 1,
        )

    def test_cancel_work_retires_all_attempt_owned_state(self):
        world = _world()
        request = _request(world)
        coordinator = self.coordinator(snapshot_cells_per_step=1)
        current = frame(
            world, 0,
            query_support_surfaces(world.view(), -1, 0, 1, 1)
            .surfaces[0].position,
        )
        coordinator.begin(
            request, current, permit=_permit(), state_anchor=None,
            remaining_damage_budget=request.damage_budget,
        )
        self.assertTrue(coordinator.has_owned_work)

        coordinator.cancel_work()

        self.assertFalse(coordinator.has_owned_work)
        self.assertIsNone(coordinator.attempt_id)
        self.assertIsNone(coordinator.current_information_need)

    def test_late_retirement_of_old_identity_cannot_touch_new_attempt(self):
        world = _world()
        request = _request(world)
        coordinator = self.coordinator(snapshot_cells_per_step=1)
        current = frame(
            world, 0,
            query_support_surfaces(world.view(), -1, 0, 1, 1)
            .surfaces[0].position,
        )
        coordinator.begin(
            request, current, permit=_permit(), state_anchor=None,
            remaining_damage_budget=request.damage_budget,
        )
        old_identity = coordinator.work_identity
        coordinator.cancel_work("first_attempt_cancelled")
        replacement = replace(
            request, sequence=2, request_id="coordinator-request-2",
        )
        coordinator.begin(
            replacement,
            current,
            permit=replace(
                _permit(),
                permit_id="coordinator-permit-2",
                source_event_id="goal-revision-2",
            ),
            state_anchor=None,
            remaining_damage_budget=replacement.damage_budget,
        )
        current_identity = coordinator.work_identity

        repeated = coordinator.retire(old_identity, "late_old_retirement")

        self.assertTrue(repeated.already_retired)
        self.assertEqual(coordinator.work_identity, current_identity)
        self.assertTrue(coordinator.has_owned_work)

    def test_information_fact_requires_the_declared_query_to_be_known(self):
        missing = (0, 2, 0)
        world = _world(unknown=frozenset({missing}))
        coordinator = self.coordinator()
        blocker = PlanningBlocker(
            missing,
            PlanningBlockerKind.CLEARANCE,
            "clearance",
            PlanningFrontierKind.SEARCH_EDGE,
            "edge",
            PlanningFactRequirement(
                PlanningFactRequirementKind.CELL_KNOWLEDGE,
                missing,
            ),
        )
        current = frame(
            world, 0,
            query_support_surfaces(world.view(), -1, 0, 1, 1)
            .surfaces[0].position,
        )

        self.assertFalse(coordinator.information_fact_is_acquired(
            blocker, current, edge_probe=None,
        ))

        world.observe_blocks(ObservationStamp(
            world.session, 2, 2, "test-clock", 2,
        ), {missing: BlockGeometry.full_cube("minecraft:stone")})
        current = frame(world, 1, current.body.position)
        self.assertTrue(coordinator.information_fact_is_acquired(
            blocker, current, edge_probe=None,
        ))

    def test_planner_result_carries_and_admits_full_work_identity(self):
        world = _world()
        request = _request(world)
        coordinator = self.coordinator(_DeferredPlanner())
        current = frame(
            world, 0,
            query_support_surfaces(world.view(), -1, 0, 1, 1)
            .surfaces[0].position,
        )
        coordinator.begin(
            request, current, permit=_permit(), state_anchor=None,
            remaining_damage_budget=request.damage_budget,
        )
        identity = coordinator.work_identity
        self.assertIsNotNone(identity)
        self.assertIs(identity.work_kind, AsyncWorkKind.PLANNING)
        coordinator.advance(
            current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget,
        )

        completed = coordinator.advance(
            current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget,
        )

        self.assertIs(completed.kind, PlanningUpdateKind.ROUTE_READY)
        self.assertIs(
            coordinator.last_admission.disposition,
            AsyncAdmissionDisposition.APPLIED,
        )
        self.assertEqual(coordinator.last_admission.identity, identity)
        self.assertFalse(coordinator.has_owned_work)


if __name__ == "__main__":
    unittest.main()
