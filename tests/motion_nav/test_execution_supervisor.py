"""Terminal body release uses current evidence without stale input poisoning."""
from __future__ import annotations

from dataclasses import replace
import ast
from pathlib import Path
import unittest

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.action_route_executor import ActionRouteState
from mc2p.motion_nav.body_control import HandoffDisposition
from mc2p.motion_nav.execution_supervisor import ExecutionSupervisor, RouteControl
from mc2p.motion_nav.navigation_session import (
    NavigationSession, NavigationSessionProfiles,
)
from mc2p.motion_nav.online_motion import InputApplicationLedger
from mc2p.motion_nav.world_model import BlockGeometry
from tests.motion_nav.test_b07_step_route import frame
from tests.motion_nav.test_b07_step_route import step_profile
from tests.motion_nav.test_b10_online_motion import action, sample
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_navigation_session import (
    _InlinePlanner, _RepeatingRecoveryExecutor,
    _goal, _ground_anchor, _known_world, _nodes,
    _InlineMotionWorker,
)


class ExecutionSupervisorTests(unittest.TestCase):
    @staticmethod
    def _executor(route, state, reason):
        executor = _RepeatingRecoveryExecutor(state, reason)
        executor.route = route.action_route
        return executor

    def test_supervisor_does_not_import_concrete_body_controllers(self):
        source = Path("mc2p/motion_nav/execution_supervisor.py").read_text(
            encoding="utf-8",
        )
        tree = ast.parse(source)
        concrete = {
            "ActionRouteExecutor", "LandingEdgeProbe",
            "MotionRouteCoordinator",
        }
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        self.assertEqual(imported.intersection(concrete), set())

    def _control(self):
        world = _known_world({
            (x, 0, 0): BlockGeometry.full_cube("minecraft:stone")
            for x in range(4)
        })
        start, goal = _nodes(world, (0, 3))
        session = NavigationSession(
            "supervisor-evidence", NavigationSessionProfiles(
                ordinary_profile(), jump_profile(), step_profile(),
            ),
            planner_worker=_InlinePlanner(), motion_worker=_InlineMotionWorker(), clock_ns=lambda: 1_000_000_000,
        )
        initial = frame(world, 0, start.position)
        session.start_goal("task", 1, _goal(goal.position), initial)
        session.propose(initial, None, 2_000_000_000)
        route = session.active_route
        self.assertIsNotNone(route)
        control = RouteControl(
            route,
            self._executor(route, ActionRouteState.CANCELLED, "cancelled"),
        )
        current = frame(world, 6, start.position)
        return control, current, _ground_anchor(current)

    def test_reanchored_old_ambiguity_does_not_poison_new_route(self):
        control, current, anchor = self._control()
        ledger = InputApplicationLedger()
        ledger.submit(current.session, action(1), requested_first_tick=4)
        ledger.mark_ambiguous(1, at_tick=4)
        ledger.submit(current.session, action(2, MovementV1()),
                      requested_first_tick=5)
        ledger.observe_sample(sample(2, 5, forward=0.0,
                                     sample_state="neutral"))
        supervisor = ExecutionSupervisor()
        self.assertTrue(supervisor.offer_route(control, current, ledger, anchor))

        evidence = supervisor.evaluate_quiescence(current, ledger, anchor)
        self.assertIs(evidence.disposition, HandoffDisposition.QUIESCENT)
        self.assertTrue(supervisor.retire_route(current, ledger, anchor))

    def test_current_ambiguity_and_missing_ledger_retain_route(self):
        control, current, anchor = self._control()
        ledger = InputApplicationLedger()
        supervisor = ExecutionSupervisor()
        self.assertTrue(supervisor.offer_route(control, current, ledger, anchor))
        self.assertFalse(supervisor.retire_route(current, None, anchor))
        ledger.submit(current.session, action(3), requested_first_tick=6)
        ledger.mark_ambiguous(3, at_tick=6)

        evidence = supervisor.evaluate_quiescence(current, ledger, anchor)
        self.assertIs(evidence.disposition, HandoffDisposition.RETAIN)
        self.assertFalse(supervisor.retire_route(current, ledger, anchor))

    def test_quiescent_release_requires_the_current_body_to_be_stable(self):
        control, current, _ = self._control()
        moving = replace(
            current,
            body=replace(
                current.body,
                velocity_blocks_per_second=(0.0, 0.0, 2.0),
            ),
        )
        anchor = _ground_anchor(moving)
        supervisor = ExecutionSupervisor()
        self.assertTrue(supervisor.offer_route(
            control, moving, InputApplicationLedger(), anchor,
        ))

        evidence = supervisor.evaluate_quiescence(
            moving, InputApplicationLedger(), anchor,
        )

        self.assertIs(evidence.disposition, HandoffDisposition.RETAIN)
        self.assertEqual(evidence.reason, "current_body_still_moving")

    def test_newer_unselected_successor_replaces_the_pending_candidate(self):
        incumbent, current, anchor = self._control()
        ledger = InputApplicationLedger()
        supervisor = ExecutionSupervisor()
        self.assertTrue(supervisor.offer_route(
            incumbent, current, ledger, anchor,
        ))
        first_route = replace(
            incumbent.route, source_request_id="request-2",
        )
        first = RouteControl(first_route, self._executor(
            first_route, ActionRouteState.RUNNING, "first",
        ))
        second_route = replace(
            incumbent.route, source_request_id="request-3",
        )
        second = RouteControl(second_route, self._executor(
            second_route, ActionRouteState.RUNNING, "second",
        ))
        self.assertTrue(supervisor.offer_route(first, current, ledger, anchor))

        self.assertTrue(supervisor.offer_route(second, current, ledger, anchor))

        self.assertIs(supervisor.incumbent_route, incumbent)
        self.assertIs(supervisor.route, second)


if __name__ == "__main__":
    unittest.main()
