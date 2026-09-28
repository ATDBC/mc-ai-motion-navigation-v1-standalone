from __future__ import annotations

import ast
from pathlib import Path
import unittest

from mc2p.motion_nav.navigation_lifecycle import (
    NavigationLifecycle,
    NavigationSessionEvent,
    NavigationSessionState,
    NavigationTransition,
    NavigationTransitionAction,
    SessionEventPolicy,
    SESSION_EVENT_TABLE,
    SESSION_TRANSITION_TABLE,
)


class NavigationLifecycleTests(unittest.TestCase):
    def test_every_state_event_pair_has_an_explicit_policy(self):
        self.assertEqual(
            set(SESSION_EVENT_TABLE),
            {(state, event) for state in NavigationSessionState
             for event in NavigationSessionEvent},
        )

    def test_old_results_are_ignored_after_terminal_state(self):
        lifecycle = NavigationLifecycle(NavigationSessionState.COMPLETE)
        self.assertIs(
            lifecycle.record(NavigationSessionEvent.PLANNER_RESULT),
            SessionEventPolicy.IGNORE,
        )
        self.assertIs(lifecycle.state, NavigationSessionState.COMPLETE)

    def test_cancel_from_execution_is_handled_and_transition_decides_state(self):
        lifecycle = NavigationLifecycle(NavigationSessionState.EXECUTING)
        self.assertIs(
            lifecycle.record(NavigationSessionEvent.CANCEL),
            SessionEventPolicy.HANDLE,
        )
        transition = lifecycle.transition(
            NavigationTransitionAction.BEGIN_STOPPING,
        )
        self.assertEqual(
            transition,
            NavigationTransition(
                previous=NavigationSessionState.EXECUTING,
                action=NavigationTransitionAction.BEGIN_STOPPING,
                current=NavigationSessionState.STOPPING,
            ),
        )
        self.assertIs(lifecycle.state, NavigationSessionState.STOPPING)

    def test_terminal_states_cannot_be_left(self):
        for state in (
            NavigationSessionState.COMPLETE,
            NavigationSessionState.CANCELLED,
            NavigationSessionState.FAILED,
            NavigationSessionState.CLOSED,
        ):
            with self.subTest(state=state):
                lifecycle = NavigationLifecycle(state)
                with self.assertRaisesRegex(Exception, "terminal"):
                    lifecycle.transition(
                        NavigationTransitionAction.BEGIN_EXECUTION,
                    )
                self.assertIs(lifecycle.state, state)

    def test_stopping_requires_an_explicit_handoff_to_resume(self):
        lifecycle = NavigationLifecycle(NavigationSessionState.STOPPING)
        with self.assertRaisesRegex(Exception, "not allowed"):
            lifecycle.transition(NavigationTransitionAction.BEGIN_EXECUTION)
        lifecycle.transition(
            NavigationTransitionAction.RESUME_EXECUTION_AFTER_HANDOFF,
        )
        self.assertIs(lifecycle.state, NavigationSessionState.EXECUTING)

    def test_every_transition_table_entry_has_one_fixed_destination(self):
        self.assertTrue(SESSION_TRANSITION_TABLE)
        for (state, action), target in SESSION_TRANSITION_TABLE.items():
            with self.subTest(state=state, action=action):
                self.assertIs(type(state), NavigationSessionState)
                self.assertIs(type(action), NavigationTransitionAction)
                self.assertIs(type(target), NavigationSessionState)

    def test_session_has_no_direct_task_state_assignments(self):
        source = Path("mc2p/motion_nav/navigation_session.py").read_text(
            encoding="utf-8",
        )
        tree = ast.parse(source)
        writes = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            targets = (
                node.targets if isinstance(node, ast.Assign)
                else (node.target,)
            )
            for target in targets:
                if (isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                        and target.attr == "_state"):
                    writes.append(node.lineno)
        self.assertEqual(writes, [])


if __name__ == "__main__":
    unittest.main()
