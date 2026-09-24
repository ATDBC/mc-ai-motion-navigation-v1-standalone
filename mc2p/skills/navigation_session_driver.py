"""Runtime bridge for one formal navigation session.

The bridge owns only the ordered input source and the one Runtime control
frame per public tick.  Planning, route validity and execution state remain
inside ``NavigationSession``.
"""
from __future__ import annotations

import json
import time
from typing import Callable

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.intent_source import ControlFrameProposalV1, IntentSourceV1
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.observation_request_v3 import merge_observation_requests
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.contracts.task import (
    ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0,
)
from mc2p.motion_nav.movement_transition import GoalState
from mc2p.motion_nav.navigation_session import (
    NavigationSessionPort, NavigationSessionState,
)
from mc2p.runtime.player_runtime_v1 import (
    PlayerRuntimeV1, RuntimeStateV1, RuntimeStepResultV1,
)


_STEP_WINDOW_NS = 500_000_000


class RuntimeNavigationDriver:
    """Submit session proposals through Runtime's sole control-frame entry."""

    def __init__(
        self,
        runtime: PlayerRuntimeV1,
        session: NavigationSessionPort,
        *,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        observation_request: ObservationRequestV3 | None = None,
    ) -> None:
        if type(runtime) is not PlayerRuntimeV1:
            raise ContractViolation("runtime navigation requires formal Runtime")
        if not isinstance(session, NavigationSessionPort):
            raise ContractViolation("runtime navigation requires NavigationSession")
        if runtime.state is not RuntimeStateV1.READY:
            raise ContractViolation("runtime navigation requires ready Runtime")
        if type(runtime.observation) is not ObservationSnapshotV3:
            raise ContractViolation("runtime navigation requires Observation V3")
        if (observation_request is not None
                and type(observation_request) is not ObservationRequestV3):
            raise ContractViolation("runtime navigation observation request is invalid")
        self.runtime = runtime
        self.session = session
        self._clock = clock_ns
        self._observation_request = observation_request or ObservationRequestV3(
            "navigation_v1"
        )
        self.source: IntentSourceV1 | None = None
        self.state = "ready"
        self.reason = "not_started"
        self._goal_id: str | None = None
        self._goal_revision: int | None = None
        self._goal: GoalState | None = None

    def start(
        self,
        goal_id: str,
        goal_revision: int,
        goal: GoalState,
        now_ns: int,
    ) -> None:
        require_nonnegative_int(now_ns, "runtime navigation start time")
        if self.source is not None or self.state not in {"ready", "stopped"}:
            raise ContractViolation("runtime navigation driver already owns input")
        if type(goal) is not GoalState:
            raise ContractViolation("runtime navigation requires GoalState")
        frame = self.session.ingest(self.runtime.observation)
        self.source = self.runtime.register_ordered_source("navigation-session")
        self.session.bind_source(self.source)
        report = self.session.report
        try:
            if report.goal_id is None:
                self.session.start_goal(goal_id, goal_revision, goal, frame)
            else:
                self.session.update_goal(goal_id, goal_revision, goal)
        except BaseException:
            self._release_source()
            raise
        self._goal_id, self._goal_revision, self._goal = (
            goal_id, goal_revision, goal,
        )
        self._sync_report()

    def replace_goal(
        self,
        goal_id: str,
        goal_revision: int,
        goal: GoalState,
        now_ns: int,
    ) -> None:
        require_nonnegative_int(now_ns, "runtime navigation goal update time")
        if self.source is None or self._goal_id is None:
            raise ContractViolation("runtime navigation driver has no active goal")
        if (goal_id != self._goal_id or type(goal_revision) is not int
                or self._goal_revision is None
                or goal_revision <= self._goal_revision
                or type(goal) is not GoalState):
            raise ContractViolation("runtime navigation goal update is stale")
        self.session.update_goal(goal_id, goal_revision, goal)
        self._goal_revision, self._goal = goal_revision, goal
        self._sync_report()

    def tick(
        self,
        profile: BehaviorProfileV0,
        owner_deadline_ns: int,
    ) -> RuntimeStepResultV1:
        if type(profile) is not BehaviorProfileV0:
            raise ContractViolation("runtime navigation tick requires BehaviorProfileV0")
        if self.source is None or self.state in {
            "ready", "success", "failed", "cancelled", "stopped",
        }:
            raise ContractViolation("runtime navigation driver cannot tick")
        now = self._clock()
        require_nonnegative_int(owner_deadline_ns, "runtime navigation owner deadline")
        deadline = min(owner_deadline_ns, now + _STEP_WINDOW_NS)
        if deadline <= now:
            raise ContractViolation("runtime navigation action window expired")
        frame = self.session.ingest(self.runtime.observation)
        proposal = self.session.propose(frame, None, deadline)
        proposals = tuple(item for item in (
            proposal.control_frame,
            ControlFrameProposalV1(
                observation_request=self._observation_request,
            ),
        ) if item is not None)
        result = self.runtime.control_frame(
            self._task(deadline), profile, deadline, proposals=proposals,
        )
        if result.report.failure is not None:
            self.state, self.reason = "failed", "runtime_failure"
        else:
            self._sync_report()
        return result

    def stop(
        self,
        profile: BehaviorProfileV0,
        reason: str,
    ) -> RuntimeStepResultV1:
        if type(profile) is not BehaviorProfileV0 or not isinstance(reason, str) \
                or not reason.strip():
            raise ContractViolation("runtime navigation stop requires profile and reason")
        if self.source is None:
            raise ContractViolation("runtime navigation driver has no input owner")
        self.release(reason)
        now = self._clock()
        deadline = now + _STEP_WINDOW_NS
        result = self.runtime.control_frame(
            self._task(deadline, reason), profile, deadline,
            proposals=(ControlFrameProposalV1(
                observation_request=self._current_observation_request(),
            ),),
        )
        self.state = "stopped"
        self.reason = reason
        return result

    def _current_observation_request(self) -> ObservationRequestV3:
        return merge_observation_requests((
            self._observation_request,
            self.session.observation_request(),
        ))

    def release(self, reason: str) -> None:
        """Relinquish ownership after a tick without advancing Runtime again."""
        if not isinstance(reason, str) or not reason.strip():
            raise ContractViolation("runtime navigation release requires reason")
        if self.source is None:
            raise ContractViolation("runtime navigation driver has no input owner")
        report = self.session.report
        if report.state not in {
            NavigationSessionState.COMPLETE,
            NavigationSessionState.CANCELLED,
            NavigationSessionState.FAILED,
            NavigationSessionState.CLOSED,
        }:
            self.session.cancel(reason)
        self._release_source()
        self.state = "stopped"
        self.reason = reason

    def _release_source(self) -> None:
        source = self.source
        if source is None:
            return
        if self.runtime.state is RuntimeStateV1.READY:
            self.runtime.cancel_source(source.source_id)
            self.runtime.unregister_ordered_source(source)
        self.session.unbind_source(source)
        self.source = None

    def _sync_report(self) -> None:
        report = self.session.report
        mapping = {
            NavigationSessionState.COMPLETE: "success",
            NavigationSessionState.CANCELLED: "cancelled",
            NavigationSessionState.FAILED: "failed",
            NavigationSessionState.CLOSED: "failed",
        }
        self.state = mapping.get(report.state, "running")
        self.reason = report.reason

    def _task(self, deadline_ns: int, reason: str | None = None) -> TaskIntentV0:
        return TaskIntentV0(
            task_id="navigation-session-runtime",
            task_type="navigate_goal",
            parameters_json=json.dumps({
                "goal_id": self._goal_id,
                "goal_revision": self._goal_revision,
                "reason": reason,
            }, sort_keys=True, separators=(",", ":")),
            success_criteria=(SuccessCriterionV0(
                "navigation_goal_reached", ComparisonOperatorV0.EQUAL,
                1, "boolean",
            ),),
            priority=100,
            deadline_monotonic_ns=deadline_ns,
            interruptible=True,
            max_risk=0.0,
        )
