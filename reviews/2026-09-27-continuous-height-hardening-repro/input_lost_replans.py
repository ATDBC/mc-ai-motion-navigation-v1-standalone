"""Round-17 review: are replans after input loss or a stalled traversal bounded?

The acceptance record (continuous-height-ground-movement.md section 6) requires
"cancel and input loss land safely first, then return the corresponding
terminal state".  5fa2f33 maps ActionRouteState.INPUT_LOST to a replan from the
current position, like NEEDS_REPLAN (e.g. ground_traversal_stalled).  The
architecture (continuous-height-ground-movement-v1.md, line 185) also caps
re-verification for the same cause, body state and world revision.  This
script lets an executor report the same state on every frame, with the body
and world unchanged, and prints the session state after each frame.

Run from the repository root of a 5fa2f33 checkout:
    PYTHONPATH=. python -B <this script>
"""
from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.action_route_executor import ActionRouteDecision, ActionRouteState
from mc2p.motion_nav.navigation_session import NavigationSession
from mc2p.motion_nav.world_model import BlockGeometry
from tests.motion_nav.test_b07_step_transition import frame
from tests.motion_nav.test_navigation_session import (
    NavigationSessionTests, _InlinePlanner, _goal, _known_world, _nodes, _source,
)


class RepeatingExecutor:
    """Reports the same non-success state every frame."""

    state = ActionRouteState.INPUT_LOST
    reason = "input_application_unconfirmed"

    def cancel(self):
        pass

    def requires_safe_handoff(self, _frame):
        return False

    def decide(self, _frame, **_):
        return ActionRouteDecision(self.state, MovementV1(), None,
                                   1, 0, self.reason, (), 0)




def run(state, reason):
    RepeatingExecutor.state, RepeatingExecutor.reason = state, reason
    world = _known_world({(x, 0, 0): BlockGeometry.full_cube("minecraft:stone") for x in range(4)})
    start, goal = _nodes(world, (0, 3))
    session = NavigationSession("input-lost", NavigationSessionTests.profiles(None),
                                planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000)
    session.bind_source(_source())
    session.start_goal("task", 1, _goal(goal.position), frame(world, 0, start.position))
    restarts = 0
    for sequence in range(1, 13):
        if session._executor is not None:
            session._executor = RepeatingExecutor()
        before = None if session._request is None else session._request.sequence
        proposal = session.propose(frame(world, sequence, start.position), None, 2_000_000_000)
        after = None if session._request is None else session._request.sequence
        restarts += int(after != before)
        print(f"  frame {sequence:>2}: {proposal.report.state.value:<12} {str(proposal.report.reason):<30} "
              f"request sequence {after}")
    print(f"  planning requests issued after {reason}: {restarts}; final state {proposal.report.state.value}")


for state, reason in ((ActionRouteState.INPUT_LOST, "input_application_unconfirmed"),
                      (ActionRouteState.NEEDS_REPLAN, "ground_traversal_stalled")):
    print(state.value)
    run(state, reason)
