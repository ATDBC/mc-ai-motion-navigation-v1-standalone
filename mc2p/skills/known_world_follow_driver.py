"""Thin known-world player target manager over formal navigation."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math

from mc2p.contracts.common import (
    ContractViolation, FieldStatusV0, require_identifier, require_nonnegative_int,
)
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.motion_nav.goal_observation import ObservedGoalStatus
from mc2p.motion_nav.goal_reach_policy import GoalReachPolicy
from mc2p.motion_nav.goal_planning_policy import GoalPlanningPolicy
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
from mc2p.motion_nav.runtime_adapter import world_session_from_observation
from mc2p.motion_nav.world_model import Aabb, WorldSessionId
from mc2p.skills.follow_types import FollowEntity, FollowView
from mc2p.skills.local_perception import project_follow_view
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver


FOLLOW_HOLD_DISTANCE_BLOCKS = 2.5
FOLLOW_REVISION_DISTANCE_BLOCKS = .5
FOLLOW_MAX_REVISION_INTERVAL_NS = 250_000_000
_POSITION_EPSILON_BLOCKS = 1.0e-9


class KnownWorldFollowState(StrEnum):
    READY = "ready"
    FOLLOWING = "following"
    NEEDS_TARGET_OBSERVATION = "needs_target_observation"
    STOPPING = "stopping"
    ENDED = "ended"


class KnownWorldFollowStatus(StrEnum):
    STARTED = "started"
    REVISED = "revised"
    UNCHANGED = "unchanged"
    THROTTLED = "throttled"
    DUPLICATE_OBSERVATION = "duplicate_observation"
    STALE_OBSERVATION = "stale_observation"
    OBSERVATION_REWRITTEN = "observation_rewritten"
    OBSERVATION_UNAVAILABLE = "observation_unavailable"
    NEEDS_TARGET_OBSERVATION = "needs_target_observation"
    TARGET_INVALID = "target_invalid"
    TARGET_DEAD = "target_dead"
    SESSION_CHANGED = "session_changed"
    NAVIGATION_ENDED = "navigation_ended"
    CANCELLED = "cancelled"
    CANCEL_PENDING = "cancel_pending"
    NOT_ACTIVE = "not_active"


@dataclass(frozen=True, slots=True)
class KnownWorldFollowResult:
    status: KnownWorldFollowStatus
    state: KnownWorldFollowState
    revision: int
    observation_sequence_id: int | None
    submitted_target_position: Vec3V0 | None
    pending_end_status: KnownWorldFollowStatus | None = None


class KnownWorldFollowDriver:
    """Own target identity and revisions; leave all motion to navigation."""

    def __init__(self, navigation: RuntimeNavigationDriver, *, task_id: str,
                 goal_id: str, target_track_id: str) -> None:
        if not isinstance(navigation, RuntimeNavigationDriver):
            raise ContractViolation("known-world follow requires RuntimeNavigationDriver")
        for value, name in ((task_id, "follow task id"),
                            (goal_id, "follow goal id"),
                            (target_track_id, "follow target track id")):
            require_identifier(value, name)
        self._navigation = navigation
        self.task_id = task_id
        self.goal_id = goal_id
        self.target_track_id = target_track_id
        self._state = KnownWorldFollowState.READY
        self._revision = 0
        self._world_session: WorldSessionId | None = None
        self._controller_clock_id: str | None = None
        self._last_observation: ObservationSnapshotV3 | None = None
        self._last_now_ns: int | None = None
        self._last_submitted_position: Vec3V0 | None = None
        self._last_submitted_at_ns: int | None = None
        self._pending_end_status: KnownWorldFollowStatus | None = None
        self._pending_end_reason: str | None = None

    @property
    def state(self) -> KnownWorldFollowState:
        return self._state

    @property
    def revision(self) -> int:
        return self._revision

    def _observation(self) -> ObservationSnapshotV3:
        observation = self._navigation.runtime.observation
        if type(observation) is not ObservationSnapshotV3:
            raise ContractViolation("known-world follow requires current Observation V3")
        return observation

    def _result(self, status: KnownWorldFollowStatus,
                observation: ObservationSnapshotV3 | None = None) -> KnownWorldFollowResult:
        return KnownWorldFollowResult(
            status, self._state, self._revision,
            None if observation is None else observation.sequence_id,
            self._last_submitted_position,
            self._pending_end_status,
        )

    @staticmethod
    def _goal(target: Vec3V0) -> GoalState:
        # GoalState is axis-aligned. The inscribed square stays within the
        # frozen radial hold distance at its corners.
        half_extent = FOLLOW_HOLD_DISTANCE_BLOCKS / math.sqrt(2.0)
        return GoalState(
            Aabb(target.x - half_extent, target.y - .1, target.z - half_extent,
                 target.x + half_extent, target.y + .1, target.z + half_extent),
            GoalSupport.SOLID, frozenset({MovementMode.WALK}),
            frozenset({"standing"}), .6,
        )

    def _target(self, view: FollowView, observation: ObservationSnapshotV3,
                ) -> tuple[KnownWorldFollowStatus | None, FollowEntity | None]:
        tracked_group = observation.tracked_entity
        tracked = tracked_group.value
        if (tracked_group.status is not FieldStatusV0.VALID
                or tracked is None
                or tracked.track_id != self.target_track_id
                or not tracked.is_loaded):
            return KnownWorldFollowStatus.NEEDS_TARGET_OBSERVATION, None
        if tracked.entity_type != "minecraft:player":
            return KnownWorldFollowStatus.TARGET_INVALID, None
        if tracked.is_dead:
            return KnownWorldFollowStatus.TARGET_DEAD, None
        target = next((entity for entity in view.entities
                       if entity.track_id == self.target_track_id), None)
        if target is None:
            return KnownWorldFollowStatus.NEEDS_TARGET_OBSERVATION, None
        if target.entity_type != "minecraft:player":
            return KnownWorldFollowStatus.TARGET_INVALID, None
        return None, target

    def start(self, now_ns: int) -> KnownWorldFollowResult:
        require_nonnegative_int(now_ns, "known-world follow start time")
        if self._revision or self._state is KnownWorldFollowState.ENDED:
            return self._result(KnownWorldFollowStatus.NOT_ACTIVE)
        observation = self._observation()
        view = project_follow_view(observation, now_ns,
                                   observation.controller_clock_id)
        if not view.available:
            self._state = KnownWorldFollowState.NEEDS_TARGET_OBSERVATION
            return self._result(KnownWorldFollowStatus.OBSERVATION_UNAVAILABLE,
                                observation)
        status, target = self._target(view, observation)
        if status is not None:
            self._state = (KnownWorldFollowState.NEEDS_TARGET_OBSERVATION
                           if status is KnownWorldFollowStatus.NEEDS_TARGET_OBSERVATION
                           else KnownWorldFollowState.ENDED)
            return self._result(status, observation)
        assert target is not None
        self._navigation.start(
            self.goal_id, 1, self._goal(target.position), now_ns,
            task_id=self.task_id,
            reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
            planning_policy=(
                GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND
            ),
        )
        self._revision = 1
        self._world_session = world_session_from_observation(observation)
        self._controller_clock_id = observation.controller_clock_id
        self._last_observation = observation
        self._last_now_ns = now_ns
        self._last_submitted_position = target.position
        self._last_submitted_at_ns = now_ns
        self._state = KnownWorldFollowState.FOLLOWING
        return self._result(KnownWorldFollowStatus.STARTED, observation)

    def _pending_end(self, status: KnownWorldFollowStatus, reason: str,
                     observation: ObservationSnapshotV3 | None) -> KnownWorldFollowResult:
        self._pending_end_status = status
        self._pending_end_reason = reason
        self._state = KnownWorldFollowState.STOPPING
        return self._result(KnownWorldFollowStatus.CANCEL_PENDING, observation)

    def _clear_pending_end(self) -> KnownWorldFollowStatus:
        status = self._pending_end_status or KnownWorldFollowStatus.NAVIGATION_ENDED
        self._pending_end_status = None
        self._pending_end_reason = None
        return status

    def _end(self, status: KnownWorldFollowStatus,
             observation: ObservationSnapshotV3 | None,
             reason: str) -> KnownWorldFollowResult:
        if self._navigation.source is None:
            self._state = KnownWorldFollowState.ENDED
            return self._result(status, observation)
        if self._navigation.has_prepared_frame:
            return self._pending_end(status, reason, observation)
        if not self._navigation.release(reason):
            return self._pending_end(status, reason, observation)
        self._state = KnownWorldFollowState.ENDED
        return self._result(status, observation)

    def _retry_pending_end(
        self, observation: ObservationSnapshotV3,
    ) -> KnownWorldFollowResult:
        if self._navigation.source is None:
            self._state = KnownWorldFollowState.ENDED
            return self._result(self._clear_pending_end(), observation)
        if self._navigation.has_prepared_frame:
            return self._result(KnownWorldFollowStatus.CANCEL_PENDING,
                                observation)
        assert self._pending_end_reason is not None
        if not self._navigation.release(self._pending_end_reason):
            return self._result(KnownWorldFollowStatus.CANCEL_PENDING,
                                observation)
        self._state = KnownWorldFollowState.ENDED
        return self._result(self._clear_pending_end(), observation)

    def update(self, now_ns: int) -> KnownWorldFollowResult:
        require_nonnegative_int(now_ns, "known-world follow update time")
        if self._state is KnownWorldFollowState.STOPPING:
            return self._retry_pending_end(self._observation())
        if self._revision == 0 or self._state in {
            KnownWorldFollowState.STOPPING, KnownWorldFollowState.ENDED,
        }:
            return self._result(KnownWorldFollowStatus.NOT_ACTIVE)

        observation = self._observation()
        report = self._navigation.session.report
        if report.terminal:
            return self._end(KnownWorldFollowStatus.NAVIGATION_ENDED, observation,
                             "known_world_follow_navigation_ended")
        assert self._last_now_ns is not None
        if now_ns < self._last_now_ns:
            return self._end(KnownWorldFollowStatus.SESSION_CHANGED, observation,
                             "known_world_follow_clock_changed")
        self._last_now_ns = now_ns
        if (world_session_from_observation(observation) != self._world_session
                or observation.controller_clock_id != self._controller_clock_id):
            return self._end(KnownWorldFollowStatus.SESSION_CHANGED, observation,
                             "known_world_follow_session_changed")

        assert self._last_observation is not None
        previous = self._last_observation
        if observation.sequence_id < previous.sequence_id:
            return self._result(KnownWorldFollowStatus.STALE_OBSERVATION,
                                observation)
        if observation.sequence_id == previous.sequence_id:
            if observation != previous:
                return self._end(KnownWorldFollowStatus.OBSERVATION_REWRITTEN,
                                 observation,
                                 "known_world_follow_observation_rewritten")
            duplicate_view = project_follow_view(
                observation, now_ns, self._controller_clock_id,
            )
            if not duplicate_view.available:
                self._state = KnownWorldFollowState.NEEDS_TARGET_OBSERVATION
                return self._result(
                    KnownWorldFollowStatus.OBSERVATION_UNAVAILABLE,
                    observation,
                )
            return self._result(KnownWorldFollowStatus.DUPLICATE_OBSERVATION,
                                observation)
        if (observation.received_at_monotonic_ns
                < previous.received_at_monotonic_ns
                or observation.request_started_at_monotonic_ns
                < previous.request_started_at_monotonic_ns
                or observation.client_sample.started_at_monotonic_ns
                < previous.client_sample.completed_at_monotonic_ns):
            return self._end(KnownWorldFollowStatus.SESSION_CHANGED, observation,
                             "known_world_follow_observation_clock_changed")

        view = project_follow_view(observation, now_ns,
                                   self._controller_clock_id)
        if not view.available:
            self._last_observation = observation
            self._state = KnownWorldFollowState.NEEDS_TARGET_OBSERVATION
            return self._result(KnownWorldFollowStatus.OBSERVATION_UNAVAILABLE,
                                observation)
        self._last_observation = observation
        status, target = self._target(view, observation)
        if status is KnownWorldFollowStatus.NEEDS_TARGET_OBSERVATION:
            self._state = KnownWorldFollowState.NEEDS_TARGET_OBSERVATION
            return self._result(status, observation)
        if status is not None:
            return self._end(status, observation,
                             "known_world_follow_target_invalid")

        assert target is not None and view.own is not None
        assert self._last_submitted_position is not None
        assert self._last_submitted_at_ns is not None
        self._state = KnownWorldFollowState.FOLLOWING
        submitted = self._last_submitted_position
        displacement = math.dist(
            (target.position.x, target.position.y, target.position.z),
            (submitted.x, submitted.y, submitted.z),
        )
        if displacement <= _POSITION_EPSILON_BLOCKS:
            return self._result(KnownWorldFollowStatus.UNCHANGED, observation)
        target_distance = math.hypot(target.position.x - view.own.position.x,
                                     target.position.z - view.own.position.z)
        satisfied = (
            report.observed_goal_status is ObservedGoalStatus.SATISFIED
        )
        if (satisfied and target_distance
                <= FOLLOW_HOLD_DISTANCE_BLOCKS + _POSITION_EPSILON_BLOCKS):
            return self._result(KnownWorldFollowStatus.THROTTLED, observation)
        left_satisfied_hold = (
            satisfied
            and target_distance > FOLLOW_HOLD_DISTANCE_BLOCKS
        )
        interval_elapsed = (now_ns - self._last_submitted_at_ns
                            >= FOLLOW_MAX_REVISION_INTERVAL_NS)
        if (displacement < FOLLOW_REVISION_DISTANCE_BLOCKS
                and not left_satisfied_hold and not interval_elapsed):
            return self._result(KnownWorldFollowStatus.THROTTLED, observation)

        next_revision = self._revision + 1
        accepted = self._navigation.replace_goal(
            self.goal_id, next_revision, self._goal(target.position), now_ns,
        )
        if not accepted:
            return self._end(KnownWorldFollowStatus.NAVIGATION_ENDED, observation,
                             "known_world_follow_navigation_ended")
        self._revision = next_revision
        self._last_submitted_position = target.position
        self._last_submitted_at_ns = now_ns
        return self._result(KnownWorldFollowStatus.REVISED, observation)

    def cancel(self) -> KnownWorldFollowResult:
        observation = None if self._revision == 0 else self._observation()
        if self._revision == 0:
            self._state = KnownWorldFollowState.ENDED
            return self._result(KnownWorldFollowStatus.CANCELLED)
        if self._state in {KnownWorldFollowState.STOPPING,
                           KnownWorldFollowState.ENDED}:
            return self._result(KnownWorldFollowStatus.NOT_ACTIVE, observation)
        return self._end(KnownWorldFollowStatus.CANCELLED, observation,
                         "known_world_follow_cancelled")
