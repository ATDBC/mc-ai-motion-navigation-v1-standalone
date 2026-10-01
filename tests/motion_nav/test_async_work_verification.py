"""R27 review regressions through public notification and driver interfaces."""
from dataclasses import replace
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys
import io
from contextlib import redirect_stdout

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import FieldStatusV0
from mc2p.contracts.observation import Vec3V0
from mc2p.motion_nav.async_work import (
    AsyncOwnerDiagnostics, AsyncWorkEvent, AsyncWorkIdentity,
    AsyncWorkKind, AsyncWorkWindow,
)
from mc2p.motion_nav.navigation_session import NavigationSession
from mc2p.motion_nav.planning_coordinator import PlanningUpdateKind
from mc2p.motion_nav.world_interaction import PlacementState
from tests.motion_nav import test_b11_world_change_navigation as placement
from tests.motion_nav import test_planning_coordinator as planning
from tests.sim.async_monitor import AsyncInvariantMonitor
from tests.sim.runner import run
from tests.sim.scenarios import SCENARIOS


class AsyncWorkVerificationTests(unittest.TestCase):
    def test_foreign_world_frame_cannot_acquire_current_information(self):
        from mc2p.motion_nav.world_model import WorldKnowledge, WorldSessionId, ObservationStamp, BlockGeometry
        world = planning._world(unknown=frozenset({(0, 0, 0)}))
        request = planning._request(world)
        owner = planning.PlanningCoordinatorTests().coordinator()
        current = planning.frame(world, 0, (-.5, 1., .5))
        owner.begin(request, current, permit=planning._permit(), state_anchor=None,
                    remaining_damage_budget=request.damage_budget)
        notification = owner.advance(current, state_anchor=None, edge_probe=None,
                                     remaining_damage_budget=request.damage_budget)
        foreign = WorldKnowledge(WorldSessionId("foreign-world"))
        foreign.observe_blocks(ObservationStamp(foreign.session, 1, 1, "clock", 1),
                               {(0, 0, 0): BlockGeometry.full_cube("minecraft:stone")})
        before = owner.async_diagnostics
        result = owner.reconcile_information(notification, planning.frame(foreign, 1, (-.5, 1., .5)))
        self.assertIs(result.kind, PlanningUpdateKind.DISCARDED)
        self.assertEqual(owner.async_diagnostics, before)

    def test_calculator_body_stop_matrix_and_normal_placement(self):
        from tests.sim.async_work_sequences import run_placement_sequence
        for mode in ("complete", "failed", "cancelled"):
            for body in ("inertia", "pending", "air"):
                for repeats in (1, 3):
                    with self.subTest(mode=mode, body=body, repeats=repeats):
                        result = run_placement_sequence(1, mode=mode, body=body, repeated=repeats)
                        self.assertTrue(result["passed"], {k:v for k,v in result.items() if k != "trace"})
        normal = run_placement_sequence(1, mode="normal")
        self.assertTrue(normal["passed"], normal["trace"][-1])

    def test_fixed_fabric_helper_drains_body_after_business_completion(self):
        from tests.sim.async_work_sequences import placement_fixture
        from mc2p.skills.block_placement_driver import RuntimeBlockPlacementDriver
        from scripts import b11_world_change_runtime as script
        clock, backend, runtime, session, parent, _ = placement_fixture()
        try:
            parent.cancel("before_helper")
            profiles = session.profiles
            frame = runtime.navigation_observation_adapter.latest_frame
            def factory(runtime, transaction, **kwargs):
                return RuntimeBlockPlacementDriver(runtime, transaction, clock_ns=runtime.monotonic_ns, **kwargs)
            with patch.object(script, "RuntimeBlockPlacementDriver", factory), patch.object(script, "FEET_Y", 64):
                report = script._run_fixed(runtime, {"trial_id": "normal-fixed-helper"},
                    BehaviorProfileV0(), frame, 20_000_000_000, profiles, lambda: None)
            self.assertEqual(report["confirmed_placements"], 1)
            self.assertEqual(backend.state.pose, "standing")
            self.assertTrue(backend.state.on_ground)
            self.assertEqual(len(backend.placements), 1)
        finally:
            session.close()
            runtime.close()

    def test_old_information_notification_after_cancel_is_discarded(self):
        world = planning._world(unknown=frozenset({(0, 0, 0)}))
        request = planning._request(world)
        owner = planning.PlanningCoordinatorTests().coordinator()
        current = planning.frame(world, 0, (-.5, 1., .5))
        owner.begin(request, current, permit=planning._permit(), state_anchor=None,
                    remaining_damage_budget=request.damage_budget)
        notification = owner.advance(current, state_anchor=None, edge_probe=None,
                                     remaining_damage_budget=request.damage_budget)
        self.assertIs(notification.kind, PlanningUpdateKind.NEEDS_INFORMATION)
        owner.cancel_work("user_cancel")
        result = owner.reconcile_information(notification, current)
        self.assertIs(result.kind, PlanningUpdateKind.DISCARDED)
        self.assertEqual(result.attempt_id, notification.attempt_id)
        self.assertIsNone(owner.current_information_update)
        self.assertEqual(result.information_identity, notification.information_identity)

    def test_repeated_cancel_preserves_terminal_placement_and_body_owner(self):
        for mode in ("complete", "failed", "cancelled"):
            with self.subTest(mode=mode):
                fixture = placement.WorldChangeNavigationIntegrationTests()
                clock, backend, driver = fixture._fixture()
                original = backend.observation

                def observed(**kwargs):
                    snapshot = original(**kwargs)
                    if not backend.placed_cells:
                        return snapshot
                    own = snapshot.self_state.value
                    snapshot = replace(snapshot, self_state=replace(snapshot.self_state,
                        value=replace(own, velocity=Vec3V0(.04, own.velocity.y, 0.))))
                    if mode == "cancelled":
                        snapshot = replace(snapshot, inventory=replace(snapshot.inventory,
                            status=FieldStatusV0.MISSING, reason_code="delivery_gap", value=None))
                    elif mode == "failed":
                        perception = snapshot.perception.value
                        snapshot = replace(snapshot, perception=replace(snapshot.perception,
                            value=replace(perception, blocks=tuple(
                                replace(block, block_id="minecraft:stone")
                                if block.position == (1, 63, 0) else block
                                for block in perception.blocks))))
                    return snapshot

                backend.observation = observed
                try:
                    for _ in range(100):
                        driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
                        if (mode == "cancelled" and driver.placement is not None
                                and backend.placed_cells
                                and not driver.placement.transaction.report.terminal):
                            driver.cancel("first_cancel")
                        active = driver.placement
                        if (active is not None and active.transaction.report.terminal
                                and active.source is not None):
                            before = active.transaction.report.state
                            expected = {"complete": PlacementState.COMPLETE,
                                        "failed": PlacementState.FAILED,
                                        "cancelled": PlacementState.CANCELLED}[mode]
                            self.assertIs(before, expected)
                            try:
                                driver.cancel("cancel_during_body_stop")
                            except Exception as error:
                                self.fail(f"legal stop rejected: {type(error).__name__}: {error}")
                            driver.cancel("repeat_cancel")
                            self.assertIs(active.transaction.report.state, before)
                            self.assertIsNotNone(active.source)
                            self.assertFalse(driver.report.terminal)
                            clicks = sum(action.operation is not None for action in backend.actions)
                            backend.observation = original
                            for _ in range(30):
                                if driver.report.terminal:
                                    break
                                driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
                            self.assertTrue(driver.report.terminal)
                            self.assertIsNone(active.source)
                            self.assertEqual(driver.report.state, "cancelled")
                            self.assertEqual(driver.report.confirmed_placements,
                                             1 if before is PlacementState.COMPLETE else 0)
                            driver.cancel("already_released")
                            self.assertEqual(driver.report.confirmed_placements,
                                             1 if before is PlacementState.COMPLETE else 0)
                            self.assertEqual(sum(action.operation is not None for action in backend.actions), clicks)
                            break
                    else:
                        self.fail("terminal business with retained body responsibility was not reached")
                finally:
                    fixture.doCleanups()

    def test_missing_formal_async_records_cannot_pass(self):
        scenario = next(item for item in SCENARIOS if item.name == "flat_walk")
        self.assertEqual(run(scenario).verdict, "PASS")
        with patch.object(NavigationSession, "async_work_diagnostics", property(lambda self: ())):
            missing = run(scenario)
        self.assertEqual(missing.outcome, "success")
        self.assertEqual(missing.verdict, "FAIL")

    def test_duplicate_receivers_are_detected_before_event_deduplication(self):
        identity = AsyncWorkIdentity("world", "task", "owner", AsyncWorkKind.MOTION_SOLVE, "edge", 1)
        window = AsyncWorkWindow(1, 100, 200)
        owner = AsyncOwnerDiagnostics("owner", identity, window,
            (AsyncWorkEvent(identity, window, "begin", 100),), ())
        monitor = AsyncInvariantMonitor()
        monitor.check((owner,), (identity,))
        self.assertFalse(monitor.violations)
        monitor.check((owner,), (identity,))  # Normal repeated reading in a later frame.
        self.assertFalse(monitor.violations)
        monitor.check((owner, owner), (identity,))
        self.assertTrue(any(rule == "I22" for rule, _ in monitor.violations))

    def test_foreign_receiver_is_rejected(self):
        identity = AsyncWorkIdentity("world", "task", "owner", AsyncWorkKind.MOTION_SOLVE, "edge", 1)
        window = AsyncWorkWindow(1, 100, 200)
        owner = AsyncOwnerDiagnostics("foreign", identity, window,
            (AsyncWorkEvent(identity, window, "begin", 100),), ())
        monitor = AsyncInvariantMonitor()
        monitor.check((owner,), (identity,))
        self.assertTrue(any(rule == "I22" for rule, _ in monitor.violations))

    def test_missing_each_required_event_cannot_pass_normal_task(self):
        scenario = next(item for item in SCENARIOS if item.name == "flat_walk")
        getter = NavigationSession.async_work_diagnostics.fget
        for removed in ("begin", "apply", "finish"):
            with self.subTest(removed=removed):
                def diagnostics(session):
                    return tuple(replace(owner,
                        events=tuple(event for event in owner.events if event.operation != removed),
                        admissions=() if removed == "apply" else owner.admissions)
                        for owner in getter(session))
                with patch.object(NavigationSession, "async_work_diagnostics", property(diagnostics)):
                    result = run(scenario)
                self.assertEqual(result.outcome, "success")
                self.assertFalse(result.verification_complete)
                self.assertEqual(result.verdict, "FAIL")

    def test_no_work_requires_declaration_and_independent_zero_activity(self):
        from tests.sim.async_monitor import AsyncCoverageRequirement, ObservedAsyncActivity
        monitor = AsyncInvariantMonitor()
        no_work = AsyncCoverageRequirement(required_kinds=(), no_async_work=True)
        self.assertTrue(monitor.finalize(no_work, ()).complete)
        self.assertFalse(monitor.finalize(None, ()).complete)
        with self.assertRaises(ValueError):
            AsyncCoverageRequirement(required_kinds=())
        identity = AsyncWorkIdentity("world", "task", "owner", AsyncWorkKind.PLANNING, "request", 1)
        self.assertFalse(monitor.finalize(no_work, (ObservedAsyncActivity(identity, "submit"),)).complete)

    def test_all_four_top_level_commands_reject_missing_evidence(self):
        from tests.sim import run_navigation_matrix as matrix
        from tests.sim import run_navigation_seed_scan as seeds
        from tests.sim import run_navigation_interrupt_matrix as interrupts
        from tests.sim import run_navigation_event_sequences as sequences
        from tests.sim.event_sequences import SequenceOutcome
        scenario = next(item for item in SCENARIOS if item.name == "flat_walk")
        with patch.object(NavigationSession, "async_work_diagnostics", property(lambda self: ())):
            missing = run(scenario)
        self.assertEqual(missing.outcome, "success")
        root = Path("tests/sim/manifests")
        documents = [json.loads((root / name).read_text("utf-8")) for name in (
            "navigation-coordination-s0a-calibrated.json", "navigation-coordination-s5-seeds.json",
            "navigation-coordination-interrupt-late.json", "navigation-coordination-event-sequences.json")]
        documents[0]["cases"] = documents[0]["cases"][:1]
        documents[1].update(seed_start=1, seed_end=1,
            families=[{"scenario": "flat_walk", "late_probability": 0.,
                       "allowed_results": [["success", "goal_state_satisfied"]]}])
        documents[2].update(interrupt_tick_start=30, interrupt_tick_end=30,
            scenarios=[{"name": "flat_walk", "revised_goal": [.5, 64., .5]}])
        documents[3].update(seeds=[1], scenarios=["flat_walk"], event_count=0,
            allowed_results={"flat_walk": [["success", "goal_state_satisfied"]]})
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for index, (module, document) in enumerate(zip((matrix, seeds, interrupts, sequences), documents)):
                with self.subTest(entry=module.__name__):
                    path = folder / f"manifest-{index}.json"
                    path.write_text(json.dumps(document), encoding="utf-8")
                    output_arg = "--output" if module in (matrix, seeds) else "--output-root"
                    command = [module.__name__, "--manifest", str(path), output_arg, str(folder / f"output-{index}")]
                    callable_name = "run_sequence" if module is sequences else "run"
                    callback = (lambda sequence: SequenceOutcome(sequence, missing)) if module is sequences else (lambda *a, **kw: missing)
                    with patch.object(module, callable_name, callback), patch.object(sys, "argv", command), redirect_stdout(io.StringIO()):
                        self.assertEqual(module.main(), 1)

    def test_old_notification_does_not_mutate_a_new_attempt(self):
        from mc2p.motion_nav.planning_coordinator import PlanningAttemptPermit
        world = planning._world(unknown=frozenset({(0, 0, 0)}))
        request = planning._request(world)
        owner = planning.PlanningCoordinatorTests().coordinator()
        current = planning.frame(world, 0, (-.5, 1., .5))
        owner.begin(request, current, permit=planning._permit(), state_anchor=None,
                    remaining_damage_budget=request.damage_budget)
        old = owner.advance(current, state_anchor=None, edge_probe=None,
                            remaining_damage_budget=request.damage_budget)
        owner.cancel_work("replacement")
        successor = replace(request, sequence=request.sequence + 1, request_id="replacement-request")
        permit = replace(planning._permit(), permit_id="replacement-permit")
        owner.begin(successor, current, permit=permit, state_anchor=None,
                    remaining_damage_budget=successor.damage_budget)
        new = owner.advance(current, state_anchor=None, edge_probe=None,
                            remaining_damage_budget=successor.damage_budget)
        before = owner.async_diagnostics
        for _ in range(3):
            discarded = owner.reconcile_information(old, current)
            self.assertIs(discarded.kind, PlanningUpdateKind.DISCARDED)
            self.assertEqual(discarded.attempt_id, old.attempt_id)
            self.assertEqual(discarded.information_identity, old.information_identity)
        self.assertEqual(owner.async_diagnostics, before)
        self.assertEqual(owner.current_information_update, new)

    def test_old_information_deliveries_after_each_retirement_and_receiver(self):
        for cause in ("cancelled", "closed", "replaced", "expired"):
            for receiver in ("cleared", "planning", "information"):
                with self.subTest(cause=cause, receiver=receiver):
                    clock = [1_000_000_000]
                    world = planning._world(unknown=frozenset({(0, 0, 0)}))
                    request = planning._request(world)
                    owner = planning.PlanningCoordinatorTests().coordinator(clock_ns=lambda: clock[0])
                    current = planning.frame(world, 0, (-.5, 1., .5))
                    owner.begin(request, current, permit=planning._permit(), state_anchor=None,
                                remaining_damage_budget=request.damage_budget)
                    old = owner.advance(current, state_anchor=None, edge_probe=None,
                                        remaining_damage_budget=request.damage_budget)
                    if cause == "expired":
                        clock[0] += 2_100_000_000
                        self.assertIs(owner.reconcile_information(old, current).kind, PlanningUpdateKind.FAILED)
                    else:
                        owner.cancel_work(cause)
                    if receiver != "cleared":
                        successor = replace(request, sequence=request.sequence + 1, request_id="replacement-request")
                        owner.begin(successor, current, permit=replace(planning._permit(), permit_id="next"),
                                    state_anchor=None, remaining_damage_budget=successor.damage_budget)
                        if receiver == "information":
                            owner.advance(current, state_anchor=None, edge_probe=None,
                                          remaining_damage_budget=successor.damage_budget)
                    before = owner.async_diagnostics
                    for _ in range(3):
                        result = owner.reconcile_information(old, current)
                        self.assertIs(result.kind, PlanningUpdateKind.DISCARDED)
                        self.assertEqual(result.attempt_id, old.attempt_id)
                        self.assertEqual(result.information_identity, old.information_identity)
                    self.assertEqual(owner.async_diagnostics, before)

    def test_normal_notification_acquires_current_fact_and_rejects_foreign_owner(self):
        from mc2p.contracts.common import ContractViolation
        from mc2p.motion_nav.world_model import ObservationStamp, BlockGeometry
        world = planning._world(unknown=frozenset({(0, 0, 0)}))
        request = planning._request(world)
        owner = planning.PlanningCoordinatorTests().coordinator()
        current = planning.frame(world, 0, (-.5, 1., .5))
        owner.begin(request, current, permit=planning._permit(), state_anchor=None,
                    remaining_damage_budget=request.damage_budget)
        notification = owner.advance(current, state_anchor=None, edge_probe=None,
                                     remaining_damage_budget=request.damage_budget)
        foreign_id = replace(notification.information_identity, owner_instance_id="foreign")
        foreign = replace(notification, attempt_id=foreign_id.key, information_identity=foreign_id)
        before = owner.async_diagnostics
        self.assertIs(owner.reconcile_information(foreign, current).kind, PlanningUpdateKind.DISCARDED)
        self.assertEqual(owner.async_diagnostics, before)
        with self.assertRaises(ContractViolation):
            owner.reconcile_information(notification.information_need, current)
        world.observe_blocks(ObservationStamp(world.session, 2, 2, "clock", 2),
                             {(0, 0, 0): BlockGeometry.full_cube("minecraft:stone")})
        acquired = owner.reconcile_information(notification, planning.frame(world, 1, (-.5, 1., .5)))
        self.assertIs(acquired.kind, PlanningUpdateKind.INFORMATION_ACQUIRED)
        self.assertFalse(owner.has_owned_work)


if __name__ == "__main__":
    unittest.main()
