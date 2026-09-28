from __future__ import annotations

import unittest

from mc2p.motion_nav.navigation_lifecycle import (
    NavigationLifecycle,
    NavigationSessionEvent,
    NavigationSessionState,
    SessionEventPolicy,
    SESSION_EVENT_TABLE,
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

    def test_cancel_from_execution_is_handled_and_state_is_owned_here(self):
        lifecycle = NavigationLifecycle(NavigationSessionState.EXECUTING)
        self.assertIs(
            lifecycle.record(NavigationSessionEvent.CANCEL),
            SessionEventPolicy.HANDLE,
        )
        lifecycle.set_state(NavigationSessionState.STOPPING)
        self.assertIs(lifecycle.state, NavigationSessionState.STOPPING)


if __name__ == "__main__":
    unittest.main()
