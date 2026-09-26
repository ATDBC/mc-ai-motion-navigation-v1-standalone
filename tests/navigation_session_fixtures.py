"""Small deterministic NavigationSession port used by C1 driver tests."""
from __future__ import annotations

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, LookV1, MovementV1
from mc2p.contracts.intent_source import (
    ControlFrameProposalV1, OrderedIntentV1, ordered_intent_id,
)
from mc2p.motion_nav.navigation_session import (
    ExternalMotionReentryDecision, ExternalMotionReentryStatus,
    NavigationSessionProposal, NavigationSessionReport, NavigationSessionState,
)
from mc2p.motion_nav.motion_residual import (
    MotionResidualResult, MotionResidualStatus,
)
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, PhysicsState
from mc2p.motion_nav.world_model import WorldSessionId
from mc2p.contracts.observation_request_v3 import ObservationRequestV3


class FakeNavigationSession:
    def __init__(self, *, cancel_steps=0):
        self.observation_adapter = None
        self.source = None
        self.goal_id = None
        self.goal_revision = None
        self.state = NavigationSessionState.READY
        self.reason = "not_started"
        self.starts = []
        self.updates = []
        self.frames = []
        self.movement = MovementV1(forward=1)
        self.look: LookV1 | None = None
        self.movement_look_tolerance_degrees = 0.0
        self.movement_observed_yaw_limit_degrees = None
        self.proposal_sequence = 0
        self._motion_baseline = None
        self.request_positions = ()
        self.execution_anchor_token = object()
        self.execution_anchor_requests = []
        self.proposal_anchors = []
        self.proposal_ledgers = []
        self.route_decision = None
        self.verified_submissions = []
        self.cancel_steps = cancel_steps
        self.cancel_remaining = 0

    def attach_observation_adapter(self, adapter):
        if self.frames and adapter is not self.observation_adapter:
            raise AssertionError("test session world owner changed after use")
        self.observation_adapter = adapter

    @property
    def report(self):
        return NavigationSessionReport(
            "fake-session", self.state, self.reason,
            self.goal_id, self.goal_revision, "request" if self.goal_id else None,
            len(self.starts) + len(self.updates), None, None, (),
            self.state in {
                NavigationSessionState.COMPLETE,
                NavigationSessionState.CANCELLED,
                NavigationSessionState.FAILED,
            },
        )

    def bind_source(self, source):
        self.source = source

    def unbind_source(self, source):
        if source != self.source:
            raise AssertionError("wrong source")
        self.source = None

    def ingest(self, snapshot):
        frame = type("Frame", (), {
            "body": type("Body", (), {"sequence_id": snapshot.sequence_id})(),
        })()
        self.frames.append(snapshot.sequence_id)
        return frame

    def motion_residual(self, snapshot, ledger):
        """Deterministic test double; real sessions replay the Fabric input ledger."""
        own = snapshot.self_state.value
        previous = self._motion_baseline
        self._motion_baseline = snapshot
        if previous is None or own is None or own.movement_tick_id is None:
            return None
        before = previous.self_state.value
        damaged = (
            own.hurt_animation_ticks > 0
            and (own.health_points + own.absorption_points)
                < (before.health_points + before.absorption_points)
        )
        session = WorldSessionId("fake:episode:clock")
        predicted = PhysicsState(
            JAVA_1_21_RULESET.ruleset_id, JAVA_1_21_RULESET.state_schema,
            session, own.movement_tick_id,
            (own.position.x, own.position.y, own.position.z),
            (own.velocity.x, own.velocity.y, own.velocity.z),
            0.0, 0.0, "standing", 0.6, 1.8, own.is_on_ground,
            own.horizontal_collision, own.vertical_collision,
            own.is_sprinting, own.is_sneaking, 0, own.fall_distance_blocks,
            0.1, 0.6, 0.08, 0.42, own.food_points,
            own.saturation_points, own.game_mode, (), False, False,
            False, False, False, False,
        )
        return MotionResidualResult(
            MotionResidualStatus.DEVIATION if damaged
            else MotionResidualStatus.MATCHED,
            before.movement_tick_id, own.movement_tick_id,
            predicted, 0.4 if damaged else 0.0,
            0.2 if damaged else 0.0,
        )

    def execution_anchor(self, snapshot, ledger):
        self.execution_anchor_requests.append((snapshot.sequence_id, ledger))
        return self.execution_anchor_token

    def external_motion_reentry(self, snapshot):
        own = snapshot.self_state.value
        can_continue = (
            own is not None and own.is_on_ground
            and self.state is NavigationSessionState.EXECUTING
        )
        return ExternalMotionReentryDecision(
            ExternalMotionReentryStatus.CONTINUE_NAVIGATION
            if can_continue else ExternalMotionReentryStatus.REQUIRES_BODY_RECOVERY,
            "fake_route_reentry" if can_continue else "fake_body_recovery",
        )

    def observation_request(self, *, max_positions=128):
        return ObservationRequestV3(
            "navigation_v1", tuple(self.request_positions[:max_positions]),
        )

    def start_goal(self, goal_id, revision, goal_state, frame, **_):
        self.goal_id, self.goal_revision = goal_id, revision
        self.starts.append((goal_id, revision, goal_state))
        self.state, self.reason = NavigationSessionState.EXECUTING, "route_admitted"

    def update_goal(self, goal_id, revision, goal_state):
        if goal_id != self.goal_id or revision <= self.goal_revision:
            raise AssertionError("stale goal update")
        self.goal_revision = revision
        self.updates.append((goal_id, revision, goal_state))
        self.state, self.reason = NavigationSessionState.EXECUTING, "goal_revised"

    def propose(self, frame, anchor, deadline_ns, *, input_ledger=None):
        source = self.source
        self.proposal_anchors.append(anchor)
        self.proposal_ledgers.append(input_ledger)
        self.proposal_sequence += 1
        if self.state is NavigationSessionState.CANCELLING:
            self.cancel_remaining -= 1
            if self.cancel_remaining <= 0:
                self.state = NavigationSessionState.CANCELLED
        intent = ActionIntentV1(
            ordered_intent_id(source, self.proposal_sequence),
            source.source_id, source.episode_id,
            frame.body.sequence_id, ActionPriorityV0.TASK,
            deadline_ns - 500_000_000, deadline_ns,
            movement=self.movement, look=self.look, valid_for_ticks=1,
            movement_requires_look=(
                self.look is not None and self.movement != MovementV1()
            ),
            movement_look_tolerance_degrees=(
                self.movement_look_tolerance_degrees
                if self.look is not None and self.movement != MovementV1()
                else 0.0
            ),
            movement_observed_yaw_limit_degrees=(
                self.movement_observed_yaw_limit_degrees
                if self.movement != MovementV1() else None
            ),
        )
        return NavigationSessionProposal(
            ControlFrameProposalV1((OrderedIntentV1(
                source, self.proposal_sequence, intent,
            ),), self.observation_request()),
            self.report,
            self.route_decision,
        )

    def register_verified_submission(self, proposal, *, control_sequence):
        self.verified_submissions.append((proposal, control_sequence))

    def cancel(self, reason):
        self.reason = reason
        if self.cancel_steps:
            self.cancel_remaining = self.cancel_steps
            self.state = NavigationSessionState.CANCELLING
        else:
            self.state = NavigationSessionState.CANCELLED
