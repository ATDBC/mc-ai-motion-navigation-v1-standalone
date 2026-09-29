"""Runtime bridge for one formal navigation session.

The bridge owns only the ordered input source and the one Runtime control
frame per public tick.  Planning, route validity and execution state remain
inside ``NavigationSession``.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.intent_source import (
    ControlFrameProposalV1, IntentSourceV1, OrderedIntentV1,
)
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.observation_request_v3 import merge_observation_requests
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.contracts.task import (
    ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0,
)
from mc2p.motion_nav.movement_transition import GoalState
from mc2p.motion_nav.body_control import HandoffDisposition
from mc2p.motion_nav.navigation_session import (
    NavigationSessionPort, NavigationSessionProposal, NavigationSessionState,
)
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.runtime.player_runtime_v1 import (
    PlayerRuntimeV1, RuntimeStateV1, RuntimeStepResultV1,
)


_STEP_WINDOW_NS = 500_000_000


class _OwnerDeadlineExpired(ContractViolation):
    """The caller's current control authority expired before dispatch."""


@dataclass(frozen=True, slots=True)
class NavigationFrameDiagnostics:
    proposed_intents: tuple[str, ...]
    proposed_movement_intents: tuple[str, ...]
    selected_intents: tuple[tuple[str, str], ...]
    candidate_intents: tuple[str, ...]
    suppressed_intents: tuple[tuple[str, str], ...]
    action_request_sequence: int | None
    action_movement: MovementV1 | None
    action_deadline_ns: int | None


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
        session.attach_observation_adapter(
            runtime.navigation_observation_adapter,
        )
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
        self._prepared_deadline_ns: int | None = None
        self._prepared_proposal: NavigationSessionProposal | None = None
        self._last_frame_diagnostics: NavigationFrameDiagnostics | None = None
        self._last_runtime_failure: str | None = None

    @property
    def last_frame_diagnostics(self) -> NavigationFrameDiagnostics | None:
        return self._last_frame_diagnostics

    @property
    def last_runtime_failure(self) -> str | None:
        return self._last_runtime_failure

    @property
    def has_prepared_frame(self) -> bool:
        return self._prepared_deadline_ns is not None

    @property
    def goal_id(self) -> str | None:
        return self._goal_id

    @property
    def goal_revision(self) -> int | None:
        return self._goal_revision

    def start(
        self,
        goal_id: str,
        goal_revision: int,
        goal: GoalState,
        now_ns: int,
        *,
        damage_budget: TaskDamageBudget | None = None,
        task_id: str | None = None,
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
                self.session.start_goal(
                    goal_id, goal_revision, goal, frame,
                    damage_budget=damage_budget,
                    task_id=task_id,
                )
            else:
                self.session.update_goal(
                    goal_id, goal_revision, goal,
                    damage_budget=damage_budget,
                    task_id=task_id,
                )
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
        *,
        damage_budget: TaskDamageBudget | None = None,
    ) -> None:
        require_nonnegative_int(now_ns, "runtime navigation goal update time")
        if self.source is None or self._goal_id is None:
            raise ContractViolation("runtime navigation driver has no active goal")
        if (goal_id != self._goal_id or type(goal_revision) is not int
                or self._goal_revision is None
                or goal_revision <= self._goal_revision
                or type(goal) is not GoalState):
            raise ContractViolation("runtime navigation goal update is stale")
        # Runtime owns the live world projection and ingests the observation
        # returned after every control frame.  The session may still refer to
        # the frame used to produce that control frame, especially while it is
        # waiting for a world interaction.  Re-anchor before querying support
        # for the revised goal so an expired immutable view is never reused.
        self.session.ingest(self.runtime.observation)
        self.session.update_goal(
            goal_id, goal_revision, goal, damage_budget=damage_budget,
        )
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
            "interaction_required",
        }:
            raise ContractViolation("runtime navigation driver cannot tick")
        try:
            proposals = self.prepare_proposals(owner_deadline_ns)
        except _OwnerDeadlineExpired:
            # Losing the caller's current lease is an expected runtime event,
            # not a broken navigation contract.  Cancel the task through the
            # existing body handoff path so an airborne controller keeps its
            # landing responsibility and a grounded task ends cleanly.
            return self.stop(profile, "owner_heartbeat_lost")
        assert self._prepared_deadline_ns is not None
        deadline = self._prepared_deadline_ns
        try:
            result = self.runtime.control_frame(
                self._task(deadline), profile, deadline, proposals=proposals,
            )
        except BaseException:
            self.discard_prepared()
            raise
        self.adopt_result(result)
        return result

    def prepare_proposals(
        self,
        owner_deadline_ns: int,
        *,
        conditioned_look: OrderedIntentV1 | None = None,
    ) -> tuple[ControlFrameProposalV1, ...]:
        """Prepare navigation for a parent-owned shared Runtime control frame."""
        if self._prepared_deadline_ns is not None:
            raise ContractViolation("runtime navigation already has a prepared frame")
        if self.source is None or self.state in {
            "ready", "success", "failed", "cancelled", "stopped",
            "interaction_required",
        }:
            raise ContractViolation("runtime navigation driver cannot prepare")
        now = self._clock()
        require_nonnegative_int(owner_deadline_ns, "runtime navigation owner deadline")
        deadline = min(owner_deadline_ns, now + _STEP_WINDOW_NS)
        if deadline <= now:
            raise _OwnerDeadlineExpired(
                "runtime navigation action window expired"
            )
        observation = self.runtime.observation
        frame = self.session.ingest(observation)
        ledger = self.runtime.input_ledger
        anchor = self.session.execution_anchor(observation, ledger)
        conditioned_yaw_delta = None
        conditioned_look_intent_id = None
        if conditioned_look is not None:
            if (type(conditioned_look) is not OrderedIntentV1
                    or conditioned_look.intent.look is None):
                raise ContractViolation(
                    "navigation conditioning requires a typed look intent"
                )
            conditioned_yaw_delta = (
                conditioned_look.intent.look.yaw_delta_degrees
            )
            conditioned_look_intent_id = conditioned_look.intent.intent_id
        try:
            proposal = self.session.propose(
                frame, anchor, deadline, input_ledger=ledger,
                conditioned_yaw_delta_degrees=conditioned_yaw_delta,
                conditioned_look_intent_id=conditioned_look_intent_id,
            )
        except ContractViolation:
            # Direct session and simulator calls still expose programmer
            # errors.  The formal driver keeps the current body owner and
            # converts the fault into a bounded safe stop.
            self.session.handle_internal_contract_failure(
                "navigation_internal_contract_failure",
            )
            proposal = self.session.propose(
                frame, anchor, deadline, input_ledger=ledger,
                conditioned_yaw_delta_degrees=conditioned_yaw_delta,
                conditioned_look_intent_id=conditioned_look_intent_id,
            )
        control = proposal.control_frame
        if control is None:
            control = ControlFrameProposalV1(
                observation_request=self._observation_request,
            )
        else:
            # The session binds its world query to the decision it just made.
            # Fetching the session request again here can observe a different
            # planning state and combine two individually valid 128-cell
            # batches into one invalid frame.  Add only the bridge-owned needs
            # (for example entity tracking) to that already-bound request.
            control = ControlFrameProposalV1(
                control.intents,
                merge_observation_requests((
                    control.observation_request,
                    self._observation_request,
                )),
                control.task_events,
            )
        if (self.source is not None
                and not any(
                    envelope.intent.movement is not None
                    for envelope in control.intents
                )):
            # Movement intents may persist until their bounded expiry.  A
            # session frame that deliberately claims no movement must revoke
            # its earlier lease before arbitration; otherwise an old neutral
            # or moving intent can be selected again with almost no deadline
            # left and fail the whole Runtime during the backend tick.
            self.runtime.cancel_source(self.source.source_id)
        proposals = (control,)
        self._prepared_deadline_ns = deadline
        self._prepared_proposal = proposal
        return proposals

    def adopt_result(self, result: RuntimeStepResultV1) -> None:
        """Adopt the one Runtime result produced from ``prepare_proposals``."""
        if self._prepared_deadline_ns is None:
            raise ContractViolation("runtime navigation has no prepared frame")
        if type(result) is not RuntimeStepResultV1:
            raise ContractViolation("runtime navigation result is invalid")
        proposal = self._prepared_proposal
        self._prepared_deadline_ns = None
        self._prepared_proposal = None
        intents = (() if proposal is None or proposal.control_frame is None
                   else proposal.control_frame.intents)
        decision = result.decision
        self._last_frame_diagnostics = NavigationFrameDiagnostics(
            tuple(item.intent.intent_id for item in intents),
            tuple(item.intent.intent_id for item in intents
                  if item.intent.movement is not None),
            () if decision is None else decision.selected_intents,
            () if decision is None else decision.candidate_intent_ids,
            () if decision is None else decision.suppressed_intents,
            None if decision is None else decision.action.request_sequence_id,
            None if decision is None else decision.action.movement,
            None if decision is None else decision.action.deadline_monotonic_ns,
        )
        if result.report.failure is not None:
            self._last_runtime_failure = (
                f"{result.report.failure.code.value}:"
                f"{result.report.failure.message}"
            )
            self.state, self.reason = "failed", "runtime_failure"
        else:
            if (not self.session.report.terminal
                    and proposal is not None
                    and proposal.route_decision is not None
                    and proposal.control_frame is not None
                    and result.decision is not None):
                navigation_intents = {
                    ordered.intent.intent_id
                    for ordered in proposal.control_frame.intents
                }
                selected_movement = {
                    intent_id for group, intent_id
                    in result.decision.selected_intents
                    if group == "movement"
                }
                if navigation_intents.intersection(selected_movement):
                    self.session.register_verified_submission(
                        proposal,
                        control_sequence=(
                            result.decision.action.request_sequence_id
                        ),
                        actual_movement=result.decision.action.movement,
                    )
                elif type(result.observation) is ObservationSnapshotV3:
                    self.session.reject_unselected_route_proposal(
                        proposal, self.session.ingest(result.observation),
                    )
            self._sync_report()
        if self.state in {"failed", "cancelled"}:
            if not self._release_if_quiescent():
                self.state = "stopping"
                self.reason = "body_handoff_waiting_for_evidence"

    def discard_prepared(self) -> None:
        """Forget a proposal when the parent did not advance Runtime."""
        if self._prepared_deadline_ns is None:
            raise ContractViolation("runtime navigation has no prepared frame")
        proposal = self._prepared_proposal
        if proposal is not None:
            self.session.discard_prepared_proposal(
                proposal, self.session.ingest(self.runtime.observation),
            )
        self._prepared_deadline_ns = None
        self._prepared_proposal = None

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
        report = self.session.report
        if report.state not in {
            NavigationSessionState.COMPLETE,
            NavigationSessionState.CANCELLED,
            NavigationSessionState.FAILED,
            NavigationSessionState.CLOSED,
        }:
            self.session.ingest(self.runtime.observation)
            self.session.cancel(reason)
        self._sync_report()
        if self.state == "stopping" or not self._release_if_quiescent():
            if self.runtime.state is not RuntimeStateV1.READY:
                self.state, self.reason = "failed", "control_unavailable"
                raise ContractViolation(
                    "CONTROL_UNAVAILABLE: navigation cannot continue safe stop"
                )
            self.state = "stopping"
            self.reason = "body_handoff_waiting_for_evidence"
            return self.tick(profile, self._clock() + _STEP_WINDOW_NS)
        now = self._clock()
        deadline = now + _STEP_WINDOW_NS
        result = self.runtime.control_frame(
            self._task(deadline, reason), profile, deadline,
            proposals=(ControlFrameProposalV1(
                observation_request=self._current_observation_request(),
            ),),
        )
        return result

    def _current_observation_request(self) -> ObservationRequestV3:
        return merge_observation_requests((
            self._observation_request,
            self.session.observation_request(),
        ))

    def release(self, reason: str) -> bool:
        """Release a terminal session, or begin cancellation without abandoning it."""
        if not isinstance(reason, str) or not reason.strip():
            raise ContractViolation("runtime navigation release requires reason")
        if self.source is None:
            raise ContractViolation("runtime navigation driver has no input owner")
        if self._prepared_deadline_ns is not None:
            raise ContractViolation("prepared navigation must be adopted or discarded")
        report = self.session.report
        if report.state not in {
            NavigationSessionState.COMPLETE,
            NavigationSessionState.CANCELLED,
            NavigationSessionState.FAILED,
            NavigationSessionState.CLOSED,
        }:
            self.session.ingest(self.runtime.observation)
            self.session.cancel(reason)
        self._sync_report()
        if self.state == "stopping":
            return False
        if not self._release_if_quiescent():
            self.state = "stopping"
            self.reason = "body_handoff_waiting_for_evidence"
            return False
        self.reason = reason
        return True

    def _release_if_quiescent(self) -> bool:
        if self.runtime.state is not RuntimeStateV1.READY:
            self.state, self.reason = "failed", "control_unavailable"
            return False
        evidence = self.session.body_handoff(
            self.runtime.observation, self.runtime.input_ledger,
        )
        if evidence.disposition is not HandoffDisposition.QUIESCENT:
            return False
        self._release_source()
        return True

    def transfer_to_successor(
        self, successor: "ExternalMotionRecoveryDriver", reason: str,
    ) -> None:
        """Transfer to an active recovery controller registered in this Runtime."""
        from mc2p.skills.external_motion_recovery_driver import (
            ExternalMotionRecoveryDriver,
        )
        if (type(successor) is not ExternalMotionRecoveryDriver
                or successor.runtime is not self.runtime
                or successor.source is None
                or successor.report.state not in {"running", "braking"}
                or not self.runtime.has_ordered_source(successor.source)
                or successor.source.episode_id
                    != self.runtime.observation.episode_id):
            raise ContractViolation("navigation successor has no active body owner")
        if not isinstance(reason, str) or not reason.strip():
            raise ContractViolation("runtime navigation transfer requires reason")
        if self.source is None:
            raise ContractViolation("runtime navigation driver has no input owner")
        if self._prepared_deadline_ns is not None:
            raise ContractViolation("prepared navigation must be adopted or discarded")
        if not self.session.report.terminal:
            self.session.ingest(self.runtime.observation)
            self.session.cancel(reason)
        self._release_source()
        self.state = "stopped"
        self.reason = reason

    def suspend_for_interaction(self) -> bool:
        """Release movement input while preserving the final navigation goal."""
        if (self.source is None
                or self.session.report.state is not NavigationSessionState.REQUIRES_INTERACTION
                or self._prepared_deadline_ns is not None):
            raise ContractViolation("navigation has no ready world interaction")
        evidence = self.session.body_handoff(
            self.runtime.observation, self.runtime.input_ledger,
        )
        if evidence.disposition is not HandoffDisposition.QUIESCENT:
            self.state = "stopping"
            self.reason = "interaction_body_handoff_pending"
            return False
        self._release_source()
        self.state = "interaction_suspended"
        self.reason = "world_interaction_owns_input"
        return True

    def resume_after_interaction(self) -> None:
        """Rebind after the confirmed world change has reached Runtime's world owner."""
        if self.source is not None or self.state != "interaction_suspended":
            raise ContractViolation("navigation is not suspended for interaction")
        source = self.runtime.register_ordered_source("navigation-session")
        self.session.bind_source(source)
        self.source = source
        self.session.ingest(self.runtime.observation)
        self._sync_report()

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
            NavigationSessionState.CANCELLING: "stopping",
            NavigationSessionState.COMPLETE: "success",
            NavigationSessionState.CANCELLED: "cancelled",
            NavigationSessionState.FAILED: "failed",
            NavigationSessionState.CLOSED: "failed",
            NavigationSessionState.REQUIRES_INTERACTION: "interaction_required",
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
