"""D059-A keeps one typed route-validation result for the current frame."""
from __future__ import annotations

from dataclasses import replace
import json
import unittest

from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
from mc2p.motion_nav.execution_supervisor import ExecutionSupervisor
from mc2p.motion_nav.online_motion import InputApplicationLedger
from mc2p.motion_nav.route_body_controller import RouteControl
from mc2p.motion_nav.route_validation import (
    ActiveRouteValidationDisposition,
    ActiveRouteValidationReason,
    DependencyOwnerKind,
)
from mc2p.motion_nav.world_model import BlockGeometry, ObservationStamp
from tests.motion_nav.test_b07_step_transition import frame as make_frame
from tests.motion_nav.test_b07_step_route import step_profile
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from tests.motion_nav.test_b07_surface_planning import flat_surface_world
from tests.motion_nav.test_d058_validation_plan import _admit, _candidate
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_navigation_session import _ground_anchor
from tests.sim.runner import Scenario, lane, run


class D059ValidationDiagnosticsTests(unittest.TestCase):
    def _control(self, request_id: str):
        world = flat_surface_world(5)
        request, candidate = _candidate(
            world, (1, 1), (3, 1), request_id=request_id,
        )
        route = _admit(
            world, request, candidate, candidate.path[0].position,
        )
        current = make_frame(world, 3, candidate.path[0].position)
        executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
        )
        executor.start(route.action_route, current)
        return world, route, RouteControl(route, executor), current

    @staticmethod
    def _replayable_dependency(route):
        plan = route.validation_plan
        owners = {owner.owner_id: owner for owner in plan.owners}
        return next(
            item.position
            for item in plan.dependency_provenance
            if all(
                owners[owner_ref].kind is DependencyOwnerKind.WALK_LEG
                for owner_ref in item.owner_refs
            )
        )

    def test_supervisor_stamps_each_advance_and_replaces_the_previous_frame(self):
        world, route, control, current = self._control("d059-frame-sequence")
        supervisor = ExecutionSupervisor()
        ledger = InputApplicationLedger()
        self.assertTrue(supervisor.offer_route(
            control, current, ledger, _ground_anchor(current),
        ))

        first = supervisor.advance_body(
            current, ledger, _ground_anchor(current),
        ).route_validation
        changed = self._replayable_dependency(route)
        next_frame = replace(
            current,
            body=replace(current.body, sequence_id=4),
            changed_cells=(changed,),
        )
        second = supervisor.advance_body(
            next_frame, ledger, _ground_anchor(next_frame),
        ).route_validation

        self.assertEqual(getattr(first, "observation_sequence_id", None), 3)
        self.assertIs(
            first.incumbent.disposition,
            ActiveRouteValidationDisposition.UNAFFECTED,
        )
        self.assertEqual(getattr(second, "observation_sequence_id", None), 4)
        self.assertIs(
            second.incumbent.disposition,
            ActiveRouteValidationDisposition.CONTINUE,
        )
        self.assertEqual(getattr(first, "observation_sequence_id", None), 3)

    def test_supervisor_preserves_incumbent_and_pending_continue_and_stop(self):
        world, route, incumbent, current = self._control("d059-pending-results")
        changed = self._replayable_dependency(route)
        current = replace(current, changed_cells=(changed,))
        ledger = InputApplicationLedger()
        supervisor = ExecutionSupervisor()
        self.assertTrue(supervisor.offer_route(
            incumbent, current, ledger, _ground_anchor(current),
        ))
        pending_route = replace(
            route,
            route_id="d059-pending-continue",
            source_request_id="d059-pending-continue-request",
        )
        pending_executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
        )
        pending_executor.start(pending_route.action_route, current)
        self.assertTrue(supervisor.offer_route(
            RouteControl(pending_route, pending_executor),
            current, ledger, _ground_anchor(current),
        ))

        continued = supervisor.advance_body(
            current, ledger, _ground_anchor(current),
        ).route_validation

        self.assertEqual(continued.observation_sequence_id, 3)
        self.assertIs(
            continued.incumbent.disposition,
            ActiveRouteValidationDisposition.CONTINUE,
        )
        self.assertIs(
            continued.pending.disposition,
            ActiveRouteValidationDisposition.CONTINUE,
        )

        world, route, incumbent, current = self._control("d059-pending-stop")
        changed = self._replayable_dependency(route)
        current = replace(current, changed_cells=(changed,))
        ledger = InputApplicationLedger()
        supervisor = ExecutionSupervisor()
        self.assertTrue(supervisor.offer_route(
            incumbent, current, ledger, _ground_anchor(current),
        ))
        pending_route = replace(
            route,
            route_id="d059-pending-stop-route",
            source_request_id="d059-pending-stop-request",
            validation_plan=None,
        )
        pending_executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
        )
        pending_executor.start(pending_route.action_route, current)
        self.assertTrue(supervisor.offer_route(
            RouteControl(pending_route, pending_executor),
            current, ledger, _ground_anchor(current),
        ))

        stopped = supervisor.advance_body(
            current, ledger, _ground_anchor(current),
        ).route_validation

        self.assertEqual(stopped.observation_sequence_id, 3)
        self.assertIs(
            stopped.incumbent.disposition,
            ActiveRouteValidationDisposition.CONTINUE,
        )
        self.assertIs(
            stopped.pending.disposition,
            ActiveRouteValidationDisposition.STOP,
        )
        self.assertIs(
            stopped.pending.reason,
            ActiveRouteValidationReason.PLAN_UNAVAILABLE,
        )

    def test_supervisor_preserves_incumbent_stop_result(self):
        world, route, control, current = self._control("d059-incumbent-stop")
        collision_air = next(
            position
            for recipe in route.validation_plan.recipes
            for position in recipe.dependencies
            if (position[1] >= 1
                and world.view().cell(position).knowledge.value == "air")
        )
        world.observe_blocks(
            ObservationStamp(
                world.session, 4, 4, "test-clock", 200_000_000,
            ),
            {collision_air: BlockGeometry.full_cube("minecraft:stone")},
        )
        changed = replace(
            make_frame(world, 4, current.body.position),
            changed_cells=(collision_air,),
        )
        supervisor = ExecutionSupervisor()
        ledger = InputApplicationLedger()
        self.assertTrue(supervisor.offer_route(
            control, current, ledger, _ground_anchor(current),
        ))

        validation = supervisor.advance_body(
            changed, ledger, _ground_anchor(changed),
        ).route_validation

        self.assertEqual(validation.observation_sequence_id, 4)
        self.assertIs(
            validation.incumbent.disposition,
            ActiveRouteValidationDisposition.STOP,
        )

    def test_simulation_json_round_trip_has_only_current_frame_validation(self):
        result = run(Scenario(
            "d059-validation-json",
            lane([[63]] * 6, width=3),
            (.5, 64.0, .5),
            (.5, 64.0, 4.5),
            max_ticks=120,
        ), after_terminal_ticks=2)

        validation_rows = [
            row for row in result.trace if row.get("route_validation") is not None
        ]
        self.assertTrue(validation_rows)
        persisted = json.loads(json.dumps(validation_rows[0]["route_validation"]))
        self.assertEqual(
            persisted["observation_sequence_id"],
            validation_rows[0]["observation_sequence"] - 1,
        )
        self.assertIn(
            persisted["incumbent"]["disposition"],
            {"unaffected", "continue", "stop"},
        )
        self.assertIn("identity", persisted["incumbent"])
        self.assertIn("affected_cells", persisted["incumbent"])
        self.assertIn("refreshed_dependencies", persisted["incumbent"])
        self.assertIn("missing_cells", persisted["incumbent"])
        self.assertIn("queries_used", persisted["incumbent"])
        self.assertEqual(
            [row["route_validation"]["observation_sequence_id"]
             for row in validation_rows],
            [row["observation_sequence"] - 1 for row in validation_rows],
        )
        self.assertIsNone(result.trace[-1]["route_validation"])
        self.assertIn("local_direct_admission", result.trace[0])


if __name__ == "__main__":
    unittest.main()
