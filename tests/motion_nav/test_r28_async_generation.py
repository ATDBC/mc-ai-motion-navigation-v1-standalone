"""R28-3 A0: passing guards and callable, uncollected baseline probes.

The probes record the original defects.  They deliberately make no assertion
that a defect must remain: later tasks can use them to write repair tests.
"""
from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch
import unittest

from mc2p.contracts.common import ContractViolation

from mc2p.motion_nav.known_map_planner import SurfacePlanningStatus
from mc2p.motion_nav.navigation_session import NavigationSession
from mc2p.motion_nav.planning_coordinator import PlanningUpdateKind
from tests.motion_nav.test_b07_step_route import frame
from tests.motion_nav import test_navigation_session as session_fixtures
from tests.motion_nav import test_planning_coordinator as planning_fixtures
from tests.motion_nav.test_navigation_session import _InlinePlanner, _goal
from tests.motion_nav.test_navigation_session import _InlineMotionWorker
from tests.motion_nav.test_planning_coordinator import _DeferredPlanner, _permit, _request, _world


class CurrentPositiveBindingTests(unittest.TestCase):
    def test_unidentified_motion_cannot_bypass_common_work_gate(self):
        from tests.motion_nav import test_r27_async_admission as motion
        from mc2p.motion_nav.motion_worker import _execute_job
        worker = motion.DeferredMotionWorker()
        owner, current, anchor, world, ledger = motion.gap_owner(worker)
        owner.decide(current, anchor, ledger, world, changed_cells=(), current_scope=owner.computation_scope)
        result = _execute_job(worker.jobs[-1])
        owner.cancel_work()
        owner._pending_connection = result.connection_id
        self.assertFalse(owner._accept_result(replace(result, work_identity=None),
            anchor, world, (), ledger, current_scope=owner.computation_scope))
        self.assertEqual(owner._local_attempts.failure_count, 0)
        self.assertEqual(owner.unidentified_results, 1)

    def submitted(self, *, request_changes=None, clock_ns=None):
        world, planner = _world(), _DeferredPlanner()
        request = replace(_request(world), **(request_changes or {}))
        owner = planning_fixtures.PlanningCoordinatorTests().coordinator(planner,
            **({"clock_ns": clock_ns} if clock_ns else {}))
        current = frame(world, 0, (-.5, 1., .5))
        owner.begin(request, current, permit=_permit(), state_anchor=None,
                    remaining_damage_budget=request.damage_budget)
        owner.advance(current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget, current_scope=owner.work_identity.scope)
        self.assertIs(planner._candidate.status, SurfacePlanningStatus.COMPLETE)
        return world, planner, request, owner, current

    def deliver(self, owner, current, budget):
        return owner.advance(current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=budget, current_scope=owner._request_ledger.current_computation_scope)

    def test_old_positive_binds_current_goal_from_actual_terminal_exit(self):
        world, planner, request, owner, current = self.submitted()
        identity, window, source = owner.work_identity, owner.work_window, planner._candidate
        goal = replace(_goal((1.5, 1., .5)), maximum_terminal_speed_blocks_per_second=10.)
        revised = replace(request, request_id='current-positive', sequence=2,
                          goal_revision=2, goal_state=goal)
        owner.revise_request(revised)
        update = self.deliver(owner, current, request.damage_budget)
        self.assertIs(update.kind, PlanningUpdateKind.ROUTE_READY)
        self.assertEqual(update.route.source_request_id, revised.request_id)
        self.assertEqual(update.route.planning_generation, revised.sequence)
        self.assertEqual(update.route.goal_revision, 2)
        self.assertEqual(update.route.goal_state, goal)
        self.assertEqual(update.route.action_route.goal_state, goal)
        self.assertEqual(update.route.work_identity, identity)
        self.assertEqual(source.request_id, request.request_id)
        self.assertEqual(source.work_identity, identity)
        applied = [e for e in owner.async_diagnostics.events if e.operation == 'apply']
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0].window, window)
        self.assertEqual(owner.local_attempt_failures, 0)

    def test_same_position_incompatible_current_terminal_constraints_recompute(self):
        from mc2p.motion_nav.motion_risk import TaskDamageBudget
        from mc2p.motion_nav.movement_transition import ResourceState
        base = replace(_goal((1.5, 1., .5)), maximum_terminal_speed_blocks_per_second=10.)
        cases = {
            'speed': replace(base, maximum_terminal_speed_blocks_per_second=.01),
            'pose': replace(base, allowed_poses=frozenset({'crouching'})),
            'yaw': replace(base, required_yaw_radians=0., maximum_yaw_error_radians=.1),
            'risk': replace(base, risk_policy_id='bounded_damage'),
            'resources': replace(base, minimum_resources=ResourceState((('food_points', 21.),))),
        }
        for name, goal in cases.items():
            with self.subTest(constraint=name):
                world, planner, request, owner, current = self.submitted()
                budget = TaskDamageBudget(goal.risk_policy_id)
                revised = replace(request, request_id='current-' + name, sequence=2,
                    goal_revision=2, goal_state=goal, damage_budget=budget,
                    initial_resources=ResourceState((('food_points', 30.),)),
                    minimum_resources=goal.minimum_resources)
                old = owner.work_identity
                owner.revise_request(revised)
                update = self.deliver(owner, current, budget)
                self.assertIs(update.kind, PlanningUpdateKind.RUNNING)
                self.assertIsNone(update.route)
                self.assertIsNone(update.failure)
                self.assertNotEqual(owner.work_identity, old)
                self.assertEqual(owner.calculation_basis.request.request_id, revised.request_id)
                self.assertEqual(owner.local_attempt_failures, 0)

    def test_body_departure_and_farther_goal_do_not_cut_or_extend_old_route(self):
        for boundary in ('body', 'goal'):
            with self.subTest(boundary=boundary):
                world, planner, request, owner, current = self.submitted()
                old = owner.work_identity
                goal = replace(_goal((1.8 if boundary == 'goal' else 1.5, 1., .5)),
                               maximum_terminal_speed_blocks_per_second=10.)
                owner.revise_request(replace(request, request_id='current-' + boundary,
                    sequence=2, goal_revision=2, goal_state=goal))
                if boundary == 'body':
                    current = frame(world, 1, (1.5, 1., .5))
                update = self.deliver(owner, current, request.damage_budget)
                self.assertIs(update.kind, PlanningUpdateKind.RUNNING)
                self.assertIsNone(update.route)
                self.assertNotEqual(owner.work_identity, old)
                self.assertEqual(owner.local_attempt_failures, 0)

    def test_same_body_position_does_not_hide_changed_entry_state(self):
        for change in ({'velocity_blocks_per_second': (9., 0., 0.)},
                       {'pose': 'crouching', 'is_sneaking': True},
                       {'is_on_ground': False}):
            with self.subTest(change=change):
                world, planner, request, owner, current = self.submitted()
                old = owner.work_identity
                owner.revise_request(replace(request, request_id='changed-entry', sequence=2, goal_revision=2))
                current = replace(current, body=replace(current.body, **change))
                update = self.deliver(owner, current, request.damage_budget)
                self.assertIs(update.kind, PlanningUpdateKind.RUNNING)
                self.assertIsNone(update.route)
                self.assertNotEqual(owner.work_identity, old)

    def test_actual_dependencies_and_capability_change_reject_old_positive(self):
        from mc2p.motion_nav.world_model import ObservationStamp, BlockGeometry
        for boundary in ('dependency', 'capability'):
            with self.subTest(boundary=boundary):
                world, planner, request, owner, current = self.submitted()
                old, original_capabilities = owner.work_identity, owner.calculation_basis.capabilities
                owner.revise_request(replace(request, request_id='current-' + boundary,
                                             sequence=2, goal_revision=2))
                if boundary == 'dependency':
                    position = (0, 0, 0)
                    self.assertIn(position, planner._candidate.dependencies)
                    world.confirm_air(ObservationStamp(world.session, 2, 2, 'clock', 2), (position,))
                    owner.observe_changes((position,))
                    current = frame(world, 1, (-.5, 1., .5))
                else:
                    owner.capabilities = replace(original_capabilities,
                        ground=replace(original_capabilities.ground,
                            maximum_speed_blocks_per_second=1.))
                update = self.deliver(owner, current, request.damage_budget)
                self.assertIs(update.kind, PlanningUpdateKind.RUNNING)
                self.assertIsNone(update.route)
                self.assertNotEqual(owner.work_identity, old)

    def test_original_positive_window_is_not_refreshed_by_revision(self):
        clock = [1_000_000_000]
        world, planner, request, owner, current = self.submitted(clock_ns=lambda: clock[0])
        old, window = owner.work_identity, owner.work_window
        owner.revise_request(replace(request, request_id='current-window', sequence=2,
            goal_revision=2, maximum_planning_seconds=60.))
        clock[0] = window.deadline_monotonic_ns
        update = self.deliver(owner, frame(world, 10, (-.5, 1., .5)), request.damage_budget)
        self.assertIs(update.kind, PlanningUpdateKind.RUNNING)
        self.assertIsNone(update.route)
        self.assertNotEqual(owner.work_identity, old)
        self.assertFalse(any(e.operation == 'apply' and e.identity == old
                             for e in owner.async_diagnostics.events))

    def test_continuously_receding_goal_keeps_original_work_bounded(self):
        clock = [1_000_000_000]
        world, planner, request, owner, current = self.submitted(clock_ns=lambda: clock[0])
        old, window = owner.work_identity, owner.work_window
        for revision in range(2, 10):
            clock[0] += 50_000_000
            goal = replace(_goal((1.5 + revision * .2, 1., .5)),
                           maximum_terminal_speed_blocks_per_second=10.)
            latest = replace(request, request_id=f'receding-{revision}', sequence=revision,
                             goal_revision=revision, goal_state=goal)
            owner.revise_request(latest)
            self.assertEqual(owner.work_identity, old)
            self.assertEqual(owner.work_window, window)
        update = self.deliver(owner, frame(world, 8, (-.5, 1., .5)), request.damage_budget)
        self.assertIs(update.kind, PlanningUpdateKind.RUNNING)
        self.assertIsNone(update.route)
        self.assertEqual(update.request_id, latest.request_id)
        self.assertNotEqual(owner.work_identity, old)
        self.assertEqual(sum(e.operation == 'finish' and e.identity == old
                             for e in owner.async_diagnostics.events), 1)
        self.assertFalse(any(e.operation == 'apply' and e.identity == old
                             for e in owner.async_diagnostics.events))
        self.assertEqual(owner._retry_ledger.total_recovery_starts, 0)

    def test_result_from_different_rule_or_capability_cannot_bind(self):
        for changed in ({'environment_id': 'different-rules'},
                        {'trajectory_profile_id': 'unavailable-profile'}):
            with self.subTest(changed=changed):
                world, planner, request, owner, current = self.submitted()
                old = owner.work_identity
                edges = list(planner._candidate.segments)
                edges[-1] = replace(edges[-1], transition=replace(edges[-1].transition, **changed))
                planner._candidate = replace(planner._candidate, segments=tuple(edges))
                owner.revise_request(replace(request, request_id='current-profile', sequence=2,
                                             goal_revision=2))
                update = self.deliver(owner, current, request.damage_budget)
                self.assertIs(update.kind, PlanningUpdateKind.RUNNING)
                self.assertIsNone(update.route)
                self.assertNotEqual(owner.work_identity, old)

    def test_same_generation_other_work_cannot_clear_current_submission(self):
        world, planner, request, owner, current = self.submitted()
        old, source = owner.work_identity, planner._candidate
        revised = replace(request, request_id='current-work', sequence=2, goal_revision=2)
        owner.revise_request(revised)
        planner._candidate = replace(source,
            work_identity=replace(old, subject_id='other-work', revision=old.revision + 1))
        update = self.deliver(owner, current, request.damage_budget)
        self.assertEqual(update.reason, 'stale_result_discarded')
        self.assertEqual(owner.work_identity, old)
        self.assertEqual(owner.diagnostics(current).submitted_request_id, request.request_id)
        planner._candidate = source
        update = self.deliver(owner, current, request.damage_budget)
        self.assertIs(update.kind, PlanningUpdateKind.ROUTE_READY)
        self.assertEqual(update.route.source_request_id, revised.request_id)
        # Repeated observation of the immutable terminal update cannot apply work twice.
        self.assertIs(self.deliver(owner, current, request.damage_budget), update)
        self.assertEqual(sum(e.operation == 'apply' for e in owner.async_diagnostics.events), 1)

    def test_current_food_is_replayed_and_retained_in_bound_route(self):
        from mc2p.motion_nav.movement_transition import ResourceState
        world, planner, request, owner, current = self.submitted(request_changes={
            'initial_resources': ResourceState((('food_points', 20.),)),
            'minimum_resources': ResourceState((('food_points', 5.),)),
        })
        source = planner._candidate
        owner.revise_request(replace(request, request_id='current-food', sequence=2, goal_revision=2))
        current = replace(current, body=replace(current.body, food_points=6))
        update = self.deliver(owner, current, request.damage_budget)
        self.assertIs(update.kind, PlanningUpdateKind.ROUTE_READY)
        self.assertEqual(update.route.action_route.final_resources.as_dict()['food_points'], 6.)
        self.assertEqual(source.final_resources.as_dict()['food_points'], 20.)

    def test_current_damage_balance_cannot_borrow_original_capacity(self):
        from tests.motion_nav.test_continuous_descent import world_and_anchor
        from tests.motion_nav.test_b07_surface_planning import ordinary_profile, step_profile
        from tests.motion_nav.test_b09_air_transitions import air_profile, frame as air_frame
        from mc2p.motion_nav.known_map_planner import (
            KnownMapBounds, KnownMapSnapshotBuilder, SurfacePlanningRequest, plan_known_surface_snapshot,
        )
        from mc2p.motion_nav.support_surfaces import SurfaceNodeId
        from mc2p.motion_nav.motion_risk import TaskDamageBudget
        from mc2p.motion_nav.movement_transition import MovementMode
        from mc2p.motion_nav.route_admission import RouteAdmitter, AdmissionStatus
        _, physics = world_and_anchor(direct_height=6, material='minecraft:stone')
        world = physics._world
        snapshot = KnownMapSnapshotBuilder(world,
            KnownMapBounds(0, 0, 58, 64, 0, 1, True)).advance(world, 10_000).snapshot
        request = SurfacePlanningRequest(1, 'original-damage', 'landing', 1,
            world.session.value, SurfaceNodeId(0, 0, 64, 0), SurfaceNodeId(0, 1, 58, 0),
            damage_budget=TaskDamageBudget('allowed-damage', 3.))
        candidate = plan_known_surface_snapshot(snapshot, ordinary_profile(), step_profile(), request,
            air_profiles=(air_profile(MovementMode.CONTROLLED_DROP),))
        self.assertIs(candidate.status, SurfacePlanningStatus.COMPLETE)
        current = air_frame(world._owner, 2, (.5, 64., .5), (0., 0., 0.), on_ground=True)
        current = replace(current, body=replace(current.body, health_points=20.))
        revised = replace(request, request_id='current-damage', sequence=2, goal_revision=2)
        admitter = RouteAdmitter()
        enough = admitter.admit_current_request(candidate, request, revised, current,
            remaining_damage_budget=request.damage_budget, changed_cells=())
        self.assertIs(enough.status, AdmissionStatus.ACCEPTED)
        reduced = admitter.admit_current_request(candidate, request, revised, current,
            remaining_damage_budget=TaskDamageBudget('allowed-damage', 2.), changed_cells=())
        self.assertIs(reduced.status, AdmissionStatus.REJECTED)
        self.assertIsNone(reduced.route)

    def test_airborne_revision_cancel_and_late_result_keep_real_body_owner(self):
        from tests.sim.runner import Event, InlineMotionWorker, run
        from tests.sim.backend import Perturbations
        from tests.motion_nav.test_navigation_supervised_interruptions import scenario
        from tests.sim.scenarios import airborne_in_drop
        for operation in ('revision', 'cancel'):
            with self.subTest(operation=operation):
                returned, pending, checked = [], [], []
                original_poll = InlineMotionWorker.poll_available
                def poll(worker):
                    results = original_poll(worker)
                    returned.extend(results)
                    late = tuple(pending)
                    pending.clear()
                    return (*results, *late)
                def interrupt(context):
                    self.assertTrue(returned, 'the original solve must actually have returned')
                    self.assertTrue(context.session.has_owned_body_control)
                    if operation == 'revision':
                        self.assertTrue(context.driver.replace_goal('goal', 2,
                            context.goal_state, context.clock[0]))
                    else:
                        context.driver.release('r28-late-airborne-cancel')
                    pending.append(returned[-1])
                    self.assertIsNotNone(context.driver.source)
                    self.assertTrue(context.session.has_owned_body_control)
                    checked.append(context.backend.movement_tick)
                base = scenario('direct_drop_5_budget_2')
                with patch.object(InlineMotionWorker, 'poll_available', poll):
                    result = run(replace(base, name='r28-airborne-' + operation,
                        events=[Event('interrupt-with-late-result', airborne_in_drop, interrupt)],
                        perturbations=Perturbations(), max_ticks=180,
                        expect='success' if operation == 'revision' else 'cancelled'))
                self.assertEqual(len(checked), 1)
                self.assertEqual(result.outcome, 'success' if operation == 'revision' else 'cancelled',
                                 result.reason)
                self.assertFalse(result.violations)
                self.assertTrue(result.verification_complete)

    def test_dispatched_placement_revision_and_cancellation_confirm_original_effect(self):
        from tests.motion_nav import test_b11_block_placement as placement
        from mc2p.motion_nav.navigation_owners import GoalRequestLedger
        from mc2p.motion_nav.async_work import ComputationInvalidationCause
        for operation in ('revision', 'cancel'):
            with self.subTest(operation=operation):
                requirement = placement.requirement()
                request = replace(_request(_world()), goal_id=requirement.goal_id,
                                  world_session=requirement.world_session)
                ledger = GoalRequestLedger(request)
                scope = ledger.bind_computation_scope(request.goal_id, request.world_session)
                clock = [150_000_000]
                transaction = placement.BlockPlacementTransaction(requirement,
                    computation_scope=scope, clock_ns=lambda: clock[0])
                adapter = placement.NavigationObservationAdapter()
                snapshot = placement.observation(1, count=3)
                proposal = transaction.propose(snapshot, adapter.ingest(snapshot))
                transaction.register_dispatch(proposal, selected=True, receipt_status='pending_confirmation',
                    receipt_reason='block_use_dispatched', control_sequence=7)
                original, window = transaction.work_identity, transaction.work_window
                if operation == 'revision':
                    ledger.advance('new-goal', goal_revision=2)
                else:
                    ledger.invalidate_computation(ComputationInvalidationCause.CANCELLED)
                snapshot = placement.observation(2, count=2, destination='minecraft:dirt')
                clock[0] = snapshot.received_at_monotonic_ns
                confirmed = transaction.propose(snapshot, adapter.ingest(snapshot))
                self.assertIs(confirmed.state, placement.PlacementState.COMPLETE)
                self.assertEqual(transaction.last_admission.identity, original)
                self.assertEqual(transaction.last_admission.deadline_monotonic_ns, window.deadline_monotonic_ns)


class PlanningRevisionPreservationTests(unittest.TestCase):
    def test_revision_after_worker_return_during_fact_scan_keeps_original_result(self):
        cells = ((0, 1, 0), (0, 2, 0))
        world, planner = _world(unknown=frozenset(cells)), _DeferredPlanner()
        request = _request(world)
        owner = planning_fixtures.PlanningCoordinatorTests().coordinator(planner)
        current = frame(world, 0, (-.5, 1., .5))
        owner.begin(request, current, permit=_permit(), state_anchor=None,
                    remaining_damage_budget=request.damage_budget)
        old = owner.work_identity
        owner.advance(current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget, current_scope=old.scope)
        world.confirm_air(planning_fixtures.ObservationStamp(world.session, 2, 2, 'test-clock', 2), cells)
        owner._snapshot_cells_per_step = 1
        update = owner.advance(frame(world, 1, (-.5, 1., .5)), state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget, current_scope=old.scope)
        self.assertEqual(update.reason, 'planning_basis_verifying')
        returned = owner._selected.candidate
        self.assertIsNotNone(returned)
        owner.revise_request(replace(request, sequence=2, request_id='revision-current', goal_revision=2))
        self.assertEqual(owner.work_identity, old)
        self.assertIs(owner._selected.candidate, returned)
        for tick in range(2, 5):
            update = owner.advance(frame(world, tick, (-.5, 1., .5)), state_anchor=None, edge_probe=None,
                remaining_damage_budget=request.damage_budget, current_scope=old.scope)
            if owner.work_identity != old:
                break
        self.assertNotEqual(owner.work_identity, old)
        self.assertEqual(update.goal_revision, 2)
        self.assertIsNone(update.information_need)
        self.assertIsNone(owner._retry_ledger.last_progress_evidence)

    def test_old_submission_rejection_does_not_fail_current_goal(self):
        class RejectingPlanner(_DeferredPlanner):
            def submit_surface_snapshot(self, *args, **kwargs):
                if args[3].goal_revision == 1:
                    return False
                return super().submit_surface_snapshot(*args, **kwargs)
        world, request = _world(), _request(_world())
        owner = planning_fixtures.PlanningCoordinatorTests().coordinator(RejectingPlanner())
        current = frame(world, 0, (-.5, 1., .5))
        owner.begin(request, current, permit=_permit(), state_anchor=None,
                    remaining_damage_budget=request.damage_budget)
        scope = owner.work_identity.scope
        owner.revise_request(replace(request, sequence=2, request_id='revision-current', goal_revision=2))
        update = owner.advance(current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget, current_scope=scope)
        self.assertIs(update.kind, PlanningUpdateKind.RUNNING)
        self.assertEqual(update.goal_revision, 2)
        self.assertIsNone(update.failure)
        self.assertEqual(owner.local_attempt_failures, 0)

    def test_revision_during_build_submits_original_request_and_bounds(self):
        world, planner = _world(), _InlinePlanner(hold_first=True)
        owner = planning_fixtures.PlanningCoordinatorTests().coordinator(planner, snapshot_cells_per_step=1)
        request = replace(_request(world), maximum_planning_seconds=60.)
        current = frame(world, 0, (-.5, 1., .5))
        owner.begin(request, current, permit=_permit(), state_anchor=None,
                    remaining_damage_budget=request.damage_budget)
        basis = owner.calculation_basis
        with self.assertRaises(FrozenInstanceError):
            basis.bounds = None
        owner.advance(current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget, current_scope=basis.scope)
        goal = planning_fixtures.query_support_surfaces(world.view(), 0, 0, 1, 1).surfaces[0].node_id
        owner.revise_request(replace(request, sequence=2, request_id='revision-current', goal_revision=2,
                                     goal=goal, maximum_planning_seconds=.05))
        for tick in range(1, 1200):
            owner.advance(frame(world, tick, (-.5, 1., .5)), state_anchor=None, edge_probe=None,
                remaining_damage_budget=request.damage_budget, current_scope=basis.scope)
            if planner.jobs:
                break
        self.assertEqual(len(planner.jobs), 1)
        snapshot, _, _, submitted, *_ = planner.jobs[0]
        self.assertEqual(snapshot.bounds, basis.bounds)
        self.assertEqual(submitted.request_id, basis.request.request_id)
        self.assertEqual(submitted.goal, basis.request.goal)
        self.assertEqual(submitted.maximum_planning_seconds, 60.)
        self.assertEqual(owner.work_identity, basis.work_identity)

    def test_old_no_route_cannot_launch_bridge_for_current_goal(self):
        from mc2p.motion_nav.bridge_planner import BridgePlacementPolicy
        world, planner = _world(), _DeferredPlanner()
        owner = planning_fixtures.PlanningCoordinatorTests().coordinator(planner)
        owner._bridge_policy, owner._bridge_remaining = BridgePlacementPolicy(), 3
        request, current = _request(world), frame(world, 0, (-.5, 1., .5))
        owner.begin(request, current, permit=_permit(), state_anchor=None,
                    remaining_damage_budget=request.damage_budget)
        old = owner.work_identity
        owner.advance(current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget, current_scope=old.scope)
        planner._candidate = replace(planner._candidate, status=SurfacePlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE,
            path=(), segments=(), ground_traversal_plans=(), planner_states=(), total_cost_seconds=None,
            total_cost_ticks=None, final_resources=None)
        owner.revise_request(replace(request, sequence=2, request_id='revision-current', goal_revision=2))
        with patch('mc2p.motion_nav.planning_coordinator.plan_next_bridge_interaction') as bridge:
            update = owner.advance(frame(world, 1, (-.5, 1., .5)), state_anchor=None, edge_probe=None,
                remaining_damage_budget=request.damage_budget, current_scope=old.scope)
            bridge.assert_not_called()
        self.assertIs(update.kind, PlanningUpdateKind.RUNNING)
        self.assertIsNone(update.interaction)
        self.assertEqual(owner.bridge_remaining, 3)

    def test_every_three_five_eight_ticks_revisions_keep_two_bounded_work(self):
        class HeldPlanner(_InlinePlanner):
            def poll_latest(self):
                return None if self.hold_first else super().poll_latest()
        for cadence in (3, 5, 8):
            with self.subTest(cadence=cadence):
                world, planner = _world(), HeldPlanner(hold_first=True)
                request = replace(_request(world), maximum_planning_seconds=2.)
                clock = [1_000_000_000]
                session = NavigationSession(f'cadence-{cadence}',
                    session_fixtures.NavigationSessionTests().profiles(), planner_worker=planner, motion_worker=session_fixtures._InlineMotionWorker(),
                    clock_ns=lambda: clock[0])
                try:
                    current = frame(world, 0, (-.5, 1., .5))
                    session.start(request, current)
                    session.propose(current, None, 5_000_000_000)
                    owner, old = session._planning_coordinator, session._planning_coordinator.work_identity
                    window = owner.work_window
                    for tick in range(1, cadence * 3 + 1):
                        clock[0] = 1_000_000_000 + tick * 50_000_000
                        if tick % cadence == 0:
                            session.update_goal(request.goal_id, tick // cadence + 1, _goal((.5, 1., .5)))
                        session.propose(frame(world, tick, (-.5, 1., .5)), None, 5_000_000_000)
                        self.assertEqual(owner.work_identity, old)
                        self.assertEqual(owner.work_window, window)
                        self.assertTrue(owner.diagnostics(session._frame).work_identity_valid)
                    self.assertEqual(len(planner.jobs), 2)
                    planner.hold_first = False
                    session.propose(frame(world, cadence * 3 + 1, (-.5, 1., .5)), None, 5_000_000_000)
                    self.assertNotEqual(owner.work_identity, old)
                    self.assertEqual(owner.request.goal_revision, 4)
                    self.assertTrue(any(work.basis is not None
                        and work.basis.request.goal_revision == 4 for work in owner._works))
                    self.assertEqual(owner.local_attempt_failures, 0)
                    self.assertEqual(session._retry_ledger.total_recovery_starts, 0)
                    self.assertIsNone(session.active_route)
                finally:
                    session.close()

    def test_expiry_reads_original_limit_and_restarts_latest_without_retry(self):
        world, planner = _world(), planning_fixtures._NeverResultPlanner()
        clock = [1_000_000_000]
        owner = planning_fixtures.PlanningCoordinatorTests().coordinator(planner, clock_ns=lambda: clock[0])
        request = replace(_request(world), maximum_planning_seconds=.5)
        current = frame(world, 0, (-.5, 1., .5))
        owner.begin(request, current, permit=_permit(), state_anchor=None,
                    remaining_damage_budget=request.damage_budget)
        old, window = owner.work_identity, owner.work_window
        owner.advance(current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget, current_scope=old.scope)
        revised = replace(request, sequence=2, request_id='longer-current', goal_revision=2,
                          maximum_planning_seconds=60.)
        owner.revise_request(revised)
        clock[0] = window.deadline_monotonic_ns
        update = owner.expire_at_observation(frame(world, 10, (-.5, 1., .5)),
                                           remaining_damage_budget=request.damage_budget)
        self.assertIs(update.kind, PlanningUpdateKind.RUNNING)
        self.assertEqual(update.request_id, revised.request_id)
        self.assertNotEqual(owner.work_identity, old)
        self.assertEqual(owner.local_attempt_failures, 0)
        self.assertEqual(owner._retry_ledger.total_recovery_starts, 0)

    def test_old_information_with_new_knowledge_does_not_install_old_blockers(self):
        world, planner = _world(unknown=frozenset({(0, 1, 0)})), _DeferredPlanner()
        request = _request(world)
        owner = planning_fixtures.PlanningCoordinatorTests().coordinator(planner)
        current = frame(world, 0, (-.5, 1., .5))
        owner.begin(request, current, permit=_permit(), state_anchor=None,
                    remaining_damage_budget=request.damage_budget)
        old = owner.work_identity
        owner.advance(current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget, current_scope=old.scope)
        owner.revise_request(replace(request, sequence=2, request_id='revision-current', goal_revision=2))
        world.confirm_air(planning_fixtures.ObservationStamp(world.session, 2, 2, 'test-clock', 2), ((0, 1, 0),))
        update = owner.advance(frame(world, 1, (-.5, 1., .5)), state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget, current_scope=old.scope)
        self.assertIs(update.kind, PlanningUpdateKind.RUNNING)
        self.assertEqual(owner._retry_ledger._blockers, set())
        self.assertIsNone(owner._retry_ledger.last_progress_evidence)
        self.assertEqual(owner.local_attempt_failures, 0)

    def test_public_revision_preserves_snapshot_and_submitted_work(self):
        for submitted in (False, True):
            with self.subTest(submitted=submitted):
                observed = goal_revision_retirement_probe(submitted=submitted)
                self.assertFalse(observed['active_identity_replaced'])
                self.assertFalse(observed['old_work_finished'])
                self.assertEqual(observed['new_goal_revision'], 2)

    def test_revision_at_each_calculation_phase_preserves_original_basis(self):
        for phase in ('before_build', 'during_build', 'submitted', 'returned'):
            with self.subTest(phase=phase):
                world, planner = _world(), _DeferredPlanner()
                request = _request(world)
                owner = planning_fixtures.PlanningCoordinatorTests().coordinator(
                    planner, snapshot_cells_per_step=1 if phase == 'during_build' else 10_000)
                current = frame(world, 0, (-.5, 1., .5))
                owner.begin(request, current, permit=_permit(), state_anchor=None,
                            remaining_damage_budget=request.damage_budget)
                basis, identity, window = owner.calculation_basis, owner.work_identity, owner.work_window
                if phase != 'before_build':
                    owner.advance(current, state_anchor=None, edge_probe=None,
                        remaining_damage_budget=request.damage_budget, current_scope=identity.scope)
                if phase == 'returned':
                    planner._defer = False
                revised = replace(request, sequence=2, request_id='revision-current', goal_revision=2,
                                  maximum_planning_seconds=.05)
                owner.revise_request(revised)
                self.assertEqual(owner.calculation_basis.request, basis.request)
                self.assertEqual(owner.work_identity, identity)
                self.assertEqual(owner.work_window, window)
                self.assertTrue(owner.diagnostics(current).permit_identity_valid)
                self.assertFalse(any(e.operation == 'finish' and e.identity == identity
                                     for e in owner.async_diagnostics.events))

    def test_old_negative_conclusions_restart_current_without_recovery_spend(self):
        for status, missing in ((SurfacePlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE, False),
                (SurfacePlanningStatus.NO_KNOWN_ROUTE, True),
                (SurfacePlanningStatus.TIMEOUT, False)):
            with self.subTest(status=status):
                world = _world(unknown=frozenset({(0, 1, 0)}) if missing else frozenset())
                planner, request = _DeferredPlanner(), _request(world)
                owner = planning_fixtures.PlanningCoordinatorTests().coordinator(planner)
                current = frame(world, 0, (-.5, 1., .5))
                owner.begin(request, current, permit=_permit(), state_anchor=None,
                            remaining_damage_budget=request.damage_budget)
                old = owner.work_identity
                owner.advance(current, state_anchor=None, edge_probe=None,
                    remaining_damage_budget=request.damage_budget, current_scope=old.scope)
                if not missing and status is not SurfacePlanningStatus.COMPLETE:
                    planner._candidate = replace(planner._candidate, status=status, path=(), segments=(),
                        ground_traversal_plans=(), planner_states=(), total_cost_seconds=None,
                        total_cost_ticks=None, final_resources=None)
                revised = replace(request, sequence=2, request_id='revision-current', goal_revision=2)
                owner.revise_request(revised)
                update = owner.advance(frame(world, 1, (-.5, 1., .5)), state_anchor=None, edge_probe=None,
                    remaining_damage_budget=request.damage_budget, current_scope=old.scope)
                self.assertIs(update.kind, PlanningUpdateKind.RUNNING)
                self.assertEqual(update.goal_revision, 2)
                self.assertIsNone(update.failure)
                self.assertIsNone(update.information_need)
                self.assertIsNone(update.interaction)
                self.assertNotEqual(owner.work_identity, old)
                self.assertEqual(owner.local_attempt_failures, 0)
                self.assertEqual(owner._retry_ledger.total_recovery_starts, 0)


class ComputationGenerationTests(unittest.TestCase):
    def test_session_candidate_dependency_retry_invalidates_scope_once(self):
        from mc2p.motion_nav.world_model import ObservationStamp, BlockGeometry
        from mc2p.motion_nav.async_work import WorkCheck
        world = _world()
        request = _request(world)
        planner = _DeferredPlanner()
        session = NavigationSession("candidate-dependency-scope", session_fixtures.NavigationSessionTests().profiles(),
            planner_worker=planner, motion_worker=session_fixtures._InlineMotionWorker(), clock_ns=lambda: 1_000_000_000)
        try:
            current = frame(world, 0, (-.5, 1., .5))
            session.start(request, current)
            session.propose(current, None, 2_000_000_000)
            owner = session._planning_coordinator
            old = owner.work_identity
            old_result = planner._candidate
            scope = session.current_computation_scope
            world.observe_blocks(ObservationStamp(world.session, 2, 2, "test-clock", 2),
                {(0, 0, 0): BlockGeometry.full_cube("minecraft:smooth_stone_slab")})
            session.propose(frame(world, 1, (-.5, 1., .5)), None, 2_000_000_000)
            next_scope = session.current_computation_scope
            self.assertEqual(next_scope.generation, scope.generation + 1)
            self.assertEqual(owner.work_identity.scope, next_scope)
            self.assertEqual(owner.local_attempt_failures, 1)
            self.assertEqual(session._retry_ledger.total_recovery_starts, 0)
            self.assertEqual(session.report.reason, "route_dependencies_changed_retry_started")
            self.assertIs(owner._selected.lifecycle.check(old, 1_000_000_000, current_scope=next_scope), WorkCheck.STALE_SCOPE)
            session.propose(frame(world, 2, (-.5, 1., .5)), None, 2_000_000_000)
            planner._candidate, planner._defer = old_result, False
            session.propose(frame(world, 3, (-.5, 1., .5)), None, 2_000_000_000)
            self.assertEqual(session.current_computation_scope, next_scope)
            self.assertTrue(any(record.identity == old and record.disposition.value == "discarded_late"
                                for record in owner.async_diagnostics.admissions))
        finally:
            session.close()

    def test_scanned_known_snapshot_dependency_retry_invalidates_scope_once(self):
        from mc2p.motion_nav.world_model import ObservationStamp, BlockGeometry
        from mc2p.motion_nav.async_work import WorkCheck
        world = _world()
        request = _request(world)
        owner = planning_fixtures.PlanningCoordinatorTests().coordinator(snapshot_cells_per_step=1)
        current = frame(world, 0, (-.5, 1., .5))
        owner.begin(request, current, permit=_permit(), state_anchor=None,
                    remaining_damage_budget=request.damage_budget)
        scope, old = owner._request_ledger.current_computation_scope, owner.work_identity
        owner.advance(current, state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget, current_scope=scope)
        outside = (100, 0, 100)
        world.observe_blocks(ObservationStamp(world.session, 2, 2, "test-clock", 2),
            {outside: BlockGeometry.full_cube("minecraft:stone")})
        owner.observe_changes((outside,))
        owner.advance(frame(world, 1, (-.5, 1., .5)), state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget, current_scope=scope)
        self.assertEqual(owner._request_ledger.current_computation_scope, scope)
        self.assertEqual(owner.work_identity, old)
        scanned = (-2, -1, -1)
        world.observe_blocks(ObservationStamp(world.session, 3, 3, "test-clock", 3),
            {scanned: BlockGeometry.full_cube("minecraft:stone")})
        owner.observe_changes((scanned,))
        update = owner.advance(frame(world, 2, (-.5, 1., .5)), state_anchor=None, edge_probe=None,
            remaining_damage_budget=request.damage_budget, current_scope=scope)
        next_scope = owner._request_ledger.current_computation_scope
        self.assertEqual(update.reason, "snapshot_dependency_changed_retry_started")
        self.assertEqual(next_scope.generation, scope.generation + 1)
        self.assertEqual(owner.work_identity.scope, next_scope)
        self.assertEqual(owner.local_attempt_failures, 1)
        self.assertEqual(owner._retry_ledger.total_recovery_starts, 0)
        self.assertIs(owner._selected.lifecycle.check(old, 1_000_000_000, current_scope=next_scope), WorkCheck.STALE_SCOPE)
        self.assertFalse(owner._selected.lifecycle.try_apply(old, 1_000_000_000, current_scope=next_scope))

    def test_ledger_revision_and_typed_invalidation(self):
        from mc2p.motion_nav.async_work import ComputationInvalidationCause
        from mc2p.motion_nav.navigation_owners import GoalRequestLedger
        ledger = GoalRequestLedger(_request(_world()))
        scope = ledger.bind_computation_scope("task", ledger.request.world_session)
        ledger.advance("session", goal_revision=2)
        self.assertEqual(ledger.current_computation_scope, scope)
        for cause in ComputationInvalidationCause:
            previous = ledger.current_computation_scope
            current = ledger.invalidate_computation(cause,
                world_session_id="another-world" if cause is ComputationInvalidationCause.WORLD_CHANGED else None)
            self.assertEqual(current.generation, previous.generation + 1)
            from mc2p.motion_nav.async_work import AsyncWorkIdentity, AsyncWorkKind, AsyncWorkLifecycle, AsyncWorkWindow, WorkCheck
            work = AsyncWorkLifecycle()
            old = AsyncWorkIdentity(previous, "owner", AsyncWorkKind.PLANNING, "request", 1)
            work.begin(old, AsyncWorkWindow(0, 100, 200))
            self.assertIs(work.check(old, 101, current_scope=current), WorkCheck.STALE_SCOPE)
        with self.assertRaises(ContractViolation):
            ledger.invalidate_computation("cancelled")
        with self.assertRaises(ContractViolation):
            ledger.bind_computation_scope("other-task", ledger.request.world_session)

    def test_revision_look_and_unrelated_observation_preserve_scope(self):
        from mc2p.motion_nav.world_model import ObservationStamp, BlockGeometry
        world = _world()
        request = _request(world)
        planner = _InlinePlanner(hold_first=True)
        session = NavigationSession("stable-scope", session_fixtures.NavigationSessionTests().profiles(),
            planner_worker=planner, motion_worker=session_fixtures._InlineMotionWorker(), clock_ns=lambda: 1_000_000_000)
        current = frame(world, 0, (-.5, 1., .5))
        try:
            session.start(request, current)
            session.propose(current, None, 2_000_000_000)
            scope = session.current_computation_scope
            turned = frame(world, 1, (-.5, 1., .5))
            turned = replace(turned, body=replace(turned.body, yaw_radians=.5))
            session.observe(turned, ())
            self.assertEqual(session.current_computation_scope, scope)
            world.observe_blocks(ObservationStamp(world.session, 2, 2, "clock", 1_000_000_000),
                {(100, 0, 100): BlockGeometry.full_cube("minecraft:stone")})
            session.observe(frame(world, 2, (-.5, 1., .5)), ((100, 0, 100),))
            self.assertEqual(session.current_computation_scope, scope)
            session.update_goal(request.goal_id, 2, _goal((.5, 1., .5)))
            self.assertEqual(session.current_computation_scope, scope)
            session.cancel("user_cancel")
            self.assertGreater(session.current_computation_scope.generation, scope.generation)
        finally:
            session.close()

    def test_work_gate_uses_current_scope_and_distinguishes_delivery_states(self):
        from mc2p.motion_nav.async_work import (
            AsyncComputationScope, AsyncWorkIdentity, AsyncWorkKind,
            AsyncWorkLifecycle, AsyncWorkWindow, WorkCheck,
        )
        scope = AsyncComputationScope("world", "task", 1)
        identity = AsyncWorkIdentity(scope, "owner", AsyncWorkKind.PLANNING, "request", 1)
        work = AsyncWorkLifecycle()
        work.begin(identity, AsyncWorkWindow(0, 100, 200))
        self.assertIs(work.check(identity, 101, current_scope=scope), WorkCheck.READY)
        self.assertIs(work.check(replace(identity, subject_id="other"), 101,
                current_scope=scope), WorkCheck.OTHER_WORK,
            )
        self.assertIs(work.check(identity, 101, current_scope=replace(scope, generation=2)),
            WorkCheck.STALE_SCOPE)
        self.assertIs(work.check(identity, 200, current_scope=scope), WorkCheck.EXPIRED)
        self.assertTrue(work.try_apply(identity, 101, current_scope=scope))
        self.assertIs(work.check(identity, 102, current_scope=scope), WorkCheck.DUPLICATE)
        self.assertFalse(work.try_apply(identity, 102, current_scope=scope))
        work.finish(identity, "completed", 103)
        self.assertIs(work.check(identity, 104, current_scope=scope), WorkCheck.FINISHED)

    def test_matching_old_planning_work_and_result_do_not_validate_each_other(self):
        from mc2p.motion_nav.async_work import ComputationInvalidationCause
        world = _world()
        request = _request(world)
        planner = _InlinePlanner(hold_first=True)
        session = NavigationSession("scope-gate", session_fixtures.NavigationSessionTests().profiles(),
            planner_worker=planner, motion_worker=session_fixtures._InlineMotionWorker(), clock_ns=lambda: 1_000_000_000)
        current = frame(world, 0, (-.5, 1., .5))
        try:
            session.start(request, current)
            session.propose(current, None, 2_000_000_000)
            owner = session._planning_coordinator
            old = owner.work_identity
            session._goal_requests.invalidate_computation(ComputationInvalidationCause.BASIS_INVALIDATED)
            planner.hold_first = False
            session.propose(frame(world, 1, (-.5, 1., .5)), None, 2_000_000_000)
            self.assertFalse(planner.jobs, "the old result must actually reach the gate")
            self.assertTrue(any(record.identity == old and record.disposition.value == "discarded_late"
                                for record in owner.async_diagnostics.admissions))
            self.assertFalse(any(record.identity == old and record.disposition.value == "applied"
                                 for record in owner.async_diagnostics.admissions))
            self.assertIsNone(session._active_route)
        finally:
            session.close()

    def test_matching_old_motion_work_and_result_use_explicit_current_scope(self):
        from tests.motion_nav import test_r27_async_admission as motion
        from mc2p.motion_nav.motion_worker import _execute_job
        worker = motion.DeferredMotionWorker()
        owner, current, anchor, world, ledger = motion.gap_owner(worker)
        scope = owner.computation_scope
        owner.decide(current, anchor, ledger, world, changed_cells=(), current_scope=scope)
        result = _execute_job(worker.jobs[-1])
        worker.results.append(result)
        self.assertIsNotNone(result.solve_result.proof)
        delivery_tick = result.solve_result.proof.execution_window.earliest_start_tick - 1
        anchor = replace(anchor, movement_tick_id=delivery_tick,
            physics_state=replace(anchor.physics_state, movement_tick_id=delivery_tick))
        owner.decide(current, anchor, ledger, world, changed_cells=(),
                     current_scope=replace(scope, generation=scope.generation + 1))
        self.assertFalse(any(record.identity == result.work_identity and record.disposition.value == "applied"
                             for record in owner.async_diagnostics.admissions))
        self.assertNotEqual(owner.async_diagnostics.active_identity, result.work_identity)

    def test_dispatched_placement_confirmation_keeps_original_scope(self):
        from tests.motion_nav import test_b11_block_placement as placement
        from mc2p.motion_nav.navigation_owners import GoalRequestLedger
        from mc2p.motion_nav.async_work import ComputationInvalidationCause
        requirement = placement.requirement()
        goal_ledger = GoalRequestLedger()
        scope = goal_ledger.bind_computation_scope(requirement.goal_id, requirement.world_session)
        clock = [150_000_000]
        transaction = placement.BlockPlacementTransaction(requirement,
            computation_scope=scope, clock_ns=lambda: clock[0])
        adapter = placement.NavigationObservationAdapter()
        snapshot = placement.observation(1, count=3)
        proposal = transaction.propose(snapshot, adapter.ingest(snapshot))
        transaction.register_dispatch(proposal, selected=True, receipt_status="pending_confirmation",
            receipt_reason="block_use_dispatched", control_sequence=7)
        original = transaction.work_identity
        goal_ledger.invalidate_computation(ComputationInvalidationCause.CANCELLED)
        snapshot = placement.observation(2, count=2, destination="minecraft:dirt")
        clock[0] = snapshot.received_at_monotonic_ns
        confirmed = transaction.propose(snapshot, adapter.ingest(snapshot))
        self.assertIs(confirmed.state, placement.PlacementState.COMPLETE)
        self.assertEqual(transaction.last_admission.identity, original)
        self.assertEqual(original.scope, scope)

    def test_same_task_rebuild_preserves_generation_and_requires_new_anchor(self):
        from mc2p.motion_nav.navigation_lifecycle import NavigationTransitionAction
        world = _world()
        request = _request(world)
        session = NavigationSession("scope-before-rebuild", session_fixtures.NavigationSessionTests().profiles(),
            planner_worker=_InlinePlanner(hold_first=True), motion_worker=session_fixtures._InlineMotionWorker(), clock_ns=lambda: 1_000_000_000)
        continuation = None
        try:
            current = frame(world, 0, (-.5, 1., .5))
            session.start(request, current)
            scope = session.current_computation_scope
            session._transition(NavigationTransitionAction.MARK_FAILED, "bounded-end")
            evidence = session.same_task_continuation_evidence()
            self.assertIsNotNone(evidence)
            continuation = session.rebuild_same_task("scope-after-rebuild", evidence)
            self.assertIsNone(continuation._request)
            self.assertEqual(continuation.current_computation_scope.task_id, scope.task_id)
            self.assertEqual(continuation.current_computation_scope.generation, scope.generation + 1)
            self.assertEqual(session.current_computation_scope, scope)
        finally:
            session.close()
            if continuation is not None:
                continuation.close()


def goal_revision_retirement_probe(*, submitted: bool) -> dict:
    """Exercise public start/update_goal; read the owner's diagnostic events."""
    world = _world()
    request = _request(world)
    current = frame(world, 0, (-.5, 1., .5))
    planner = _InlinePlanner(hold_first=True)
    session = NavigationSession(
        "a0-retirement", session_fixtures.NavigationSessionTests().profiles(),
        planner_worker=planner, motion_worker=session_fixtures._InlineMotionWorker(), clock_ns=lambda: 1_000_000_000,
        snapshot_cells_per_step=10_000 if submitted else 1,
    )
    try:
        session.start(request, current)
        owner = session._planning_coordinator
        # Advance one batch: with a budget of one this is construction in
        # progress, not merely an allocated builder that has never done work.
        session.propose(current, None, 2_000_000_000)
        before = owner.async_diagnostics
        diagnostics = owner.diagnostics(current)
        jobs_before = len(planner.jobs)
        session.update_goal(request.goal_id, 2, _goal((.5, 1., .5)))
        after = owner.async_diagnostics
        finishes = [event for event in after.events
                    if event.operation == "finish"
                    and event.identity == before.active_identity]
        return {
            "phase": "submitted" if submitted else "snapshot_building",
            "submitted_request_before": diagnostics.submitted_request_id,
            "jobs_before_revision": jobs_before,
            "resources_before_revision": [name for _, name in before.resources],
            "active_identity_replaced": before.active_identity != after.active_identity,
            "old_work_finished": bool(finishes),
            "retirement_causes": [event.cause for event in finishes],
            "new_goal_revision": owner.request.goal_revision,
        }
    finally:
        session.close()


def same_task_successor_probe() -> dict:
    """Exercise the same-task successor boundary without preserving a defect."""
    world = _world()
    request = _request(world)
    current = frame(world, 0, (-.5, 1., .5))
    session = NavigationSession(
        "a0-task-original", session_fixtures.NavigationSessionTests().profiles(),
        planner_worker=_InlinePlanner(), motion_worker=_InlineMotionWorker(), clock_ns=lambda: 1_000_000_000,
    )
    successor = None
    try:
        session.start_goal(request.goal_id, 1, _goal((-.5, 1., .5)), current)
        session.propose(current, None, 2_000_000_000)
        original_ledger = session._retry_ledger
        original_risk = session._risk_ledger
        terminal_before = session.report.terminal
        missing_id_rejected = False
        try:
            successor = session.spawn_successor("a0-task-successor")
        except TypeError:
            missing_id_rejected = True
            try:
                successor = session.spawn_successor(
                    "a0-task-successor", task_id=original_ledger.task_id,
                )
            except ContractViolation:
                return {
                    "original_terminal": terminal_before,
                    "same_task_id_accepted": False,
                    "new_retry_ledger": False,
                    "new_risk_ledger": False,
                    "old_session_closed": session._closed,
                    "task_id_required_at_spawn": missing_id_rejected,
                }
        successor.start(request, current)
        return {
            "original_terminal": terminal_before,
            "same_task_id_accepted": successor._retry_ledger.task_id == original_ledger.task_id,
            "new_retry_ledger": successor._retry_ledger is not original_ledger,
            "new_risk_ledger": successor._risk_ledger is not original_risk,
            "old_session_closed": session._closed,
            "task_id_required_at_spawn": missing_id_rejected,
        }
    finally:
        if successor is not None:
            successor.close()
        session.close()


class OldNegativeResultGuards(unittest.TestCase):
    def check_old_negative(self, status, *, missing=False):
        world = _world(unknown=frozenset({(0, 1, 0)}) if missing else frozenset())
        request = _request(world)
        current = frame(world, 0, (-.5, 1., .5))
        planner = _DeferredPlanner()
        owner = planning_fixtures.PlanningCoordinatorTests().coordinator(planner)
        owner.begin(request, current, permit=_permit(), state_anchor=None,
                    remaining_damage_budget=request.damage_budget)
        owner.advance(current, state_anchor=None, edge_probe=None,
                      remaining_damage_budget=request.damage_budget, current_scope=owner._request_ledger.current_computation_scope)
        old_candidate = planner._candidate
        if missing:
            self.assertIsNotNone(old_candidate.information_need)
            self.assertIs(old_candidate.status, status)
        else:
            old_candidate = replace(old_candidate, status=status, path=(),
                                    segments=(), ground_traversal_plans=(),
                                    planner_states=(), total_cost_seconds=None,
                                    total_cost_ticks=None, final_resources=None)
        revised = replace(request, sequence=2, request_id="a0-new-request",
                          goal_revision=2)
        owner.begin(revised, current,
                    permit=replace(_permit(), permit_id="a0-new-permit", goal_revision=2),
                    state_anchor=None, remaining_damage_budget=revised.damage_budget)
        # Delivery transport is injected; begin/advance and admission use the
        # production owner. Its current submission is established by advance.
        owner.advance(current, state_anchor=None, edge_probe=None,
                      remaining_damage_budget=revised.damage_budget, current_scope=owner._request_ledger.current_computation_scope)
        active = owner.async_diagnostics.active_identity
        planner._candidate = old_candidate
        planner._defer = False
        update = owner.advance(current, state_anchor=None, edge_probe=None,
                               remaining_damage_budget=revised.damage_budget, current_scope=owner._request_ledger.current_computation_scope)
        self.assertIs(update.kind, PlanningUpdateKind.RUNNING)
        self.assertIsNone(update.failure)
        self.assertIsNone(update.information_need)
        self.assertEqual(update.request_id, revised.request_id)
        self.assertEqual(update.goal_revision, revised.goal_revision)
        self.assertEqual(owner.async_diagnostics.active_identity, active)
        self.assertEqual(owner.diagnostics(current).submitted_request_id, revised.request_id)
        self.assertEqual(owner.local_attempt_failures, 0)

    def test_old_no_route_does_not_fail_revised_goal(self):
        self.check_old_negative(SurfacePlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE)

    def test_old_information_does_not_request_old_facts_for_revised_goal(self):
        self.check_old_negative(SurfacePlanningStatus.NO_KNOWN_ROUTE, missing=True)

    def test_old_budget_timeout_does_not_spend_revised_goal_retry(self):
        self.check_old_negative(SurfacePlanningStatus.TIMEOUT)


if __name__ == "__main__":
    unittest.main()
