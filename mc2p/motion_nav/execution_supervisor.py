"""One-frame body ownership for navigation's route and edge acquisition."""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.body_control import (
    BodyControlDecision, BodyControlActivity, BodyControlPhase,
    BodyController, HandoffDisposition, HandoffEvidence,
    StopCause,
)
from mc2p.motion_nav.probe_body_controller import ProbeBodyController, ProbeOutcome
from mc2p.motion_nav.route_body_controller import RouteAdvance, RouteControl
from mc2p.motion_nav.action_route_executor import ActionRouteDecision, ActionRouteState
from mc2p.motion_nav.landing_edge_probe import LandingEdgeProbeState
from mc2p.motion_nav.online_motion import (
    InputApplicationLedger, InputApplicationStatus, InputResponsibilityDisposition,
    StateAnchor, assess_input_responsibility,
    input_application_has_finished, project_movement_command,
)
from mc2p.contracts.motion_activation import (
    ActivationRejectReason, ActivationResult, ActivationStatus,
    PreparedActivationTransaction, StreamActivationBinding,
    PreparedActivationAcceptance, PreparedActivationDelivery, FirstStreamApplication,
    StreamApplicationResult, StreamApplicationStatus, StreamApplicationReason,
    PreparedStreamDispatch,
)
from mc2p.motion_nav.prepared_motion import (
    PreparedActionHandoff, PreparedHandoffApproval, PreparedHandoffRejectReason,
    PreparedHandoffStatus, PreparedQueryStatus,
)
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.async_work import AsyncComputationScope
from mc2p.motion_nav.safe_ground_control import verified_ground_rollout
from mc2p.motion_nav.route_validation import (
    ActiveRouteValidation,
    ActiveRouteValidationDisposition,
    RouteValidationBudget,
)
from mc2p.motion_nav.world_model import BlockPos, WorldQueryCache


_TERMINAL_DECISIONS = frozenset({
    ActionRouteState.COMPLETE, ActionRouteState.CANCELLED,
    ActionRouteState.FAILED, ActionRouteState.BLOCKED,
    ActionRouteState.UNSUPPORTED, ActionRouteState.INPUT_LOST,
    ActionRouteState.NEEDS_REPLAN,
})
_REJECTED_CANDIDATE_STATES = _TERMINAL_DECISIONS - {
    ActionRouteState.COMPLETE, ActionRouteState.CANCELLED,
}


def prepare_action_activation(control: RouteControl, approval: PreparedHandoffApproval,
                              binding: StreamActivationBinding) -> PreparedActivationTransaction | ActivationResult:
    """Bind an existing permission to actual Runtime facts, without committing."""
    if type(control) is not RouteControl or type(binding) is not StreamActivationBinding:
        raise ContractViolation("action activation preparation requires typed control and binding")
    supervisor = control.executor._prepared_handoff_supervisor
    if (type(supervisor) is not ExecutionSupervisor
            or not ExecutionSupervisor.prepared_approval_is_current(supervisor, approval, control=control)):
        return ActivationResult._from_native_references(
            ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.HANDOFF)
    first = control.prepared_action_proposal(approval)
    mismatch = ActivationStatus.ACTIVATION_MISMATCH
    if first is None:
        return ActivationResult._from_native_references(mismatch, ActivationRejectReason.HANDOFF)
    if binding.generation_id != first.generation_id:
        return ActivationResult._from_native_references(ActivationStatus.STALE_GENERATION, ActivationRejectReason.GENERATION)
    for name, reason in (("proof_token", ActivationRejectReason.PROOF), ("owner_id", ActivationRejectReason.OWNER),
                         ("ledger_id", ActivationRejectReason.LEDGER), ("world_session_id", ActivationRejectReason.WORLD_SESSION)):
        if getattr(binding, name) != getattr(first, name):
            return ActivationResult._from_native_references(mismatch, reason)
    proposal = approval.proposal
    if binding.requested_window != first.requested_window or binding.submitted_movement_tick != proposal.entry_state.movement_tick_id:
        return ActivationResult._from_native_references(mismatch, ActivationRejectReason.WINDOW)
    handle = control.executor._prepared_handle(proposal.new_action_index)
    yaw = proposal.entry_state.yaw_radians + math.radians(binding.compound_action.look.yaw_delta_degrees)
    projected = project_movement_command(proposal.entry_state, binding.compound_action.movement,
                                         movement_yaw_radians=yaw)
    if (binding.compound_action.valid_for_ticks != 1 or handle is None
            or projected.tick_input != handle.hot_view.inputs[0]):
        return ActivationResult._from_native_references(mismatch, ActivationRejectReason.MOVEMENT)
    return PreparedActivationTransaction._from_native_references(
        transaction_token="transaction-" + uuid4().hex, binding=binding, old_action=proposal.current_command,
        new_action=binding.compound_action, permission_token=approval.approval_token, handoff_token=proposal.handoff_token)


class BodySelectionKind(StrEnum):
    ROUTE = "route"
    INCUMBENT_PREFIX = "incumbent_prefix"
    CANDIDATE_WAIT = "candidate_wait"
    PROBE_STOP = "probe_stop"


@dataclass(frozen=True, slots=True)
class BodyFrameAdvance:
    """Raw route facts; Session still owns candidate recovery and risk."""

    route_advance: RouteAdvance
    waiting_candidate: RouteAdvance | None = None
    rejected_candidate: RouteAdvance | None = None
    route_validation: BodyRouteValidation | None = None


@dataclass(frozen=True, slots=True)
class BodyRouteValidation:
    observation_sequence_id: int
    incumbent: ActiveRouteValidation | None
    pending: ActiveRouteValidation | None


@dataclass(frozen=True, slots=True)
class BodyFrameResult:
    """The one controller chosen for this frame; selection is not takeover."""

    kind: BodySelectionKind
    controller: RouteControl | ProbeBodyController
    route_advance: RouteAdvance
    decision: ActionRouteDecision
    incumbent_route: RouteControl | None
    pending_route: RouteControl | None
    handoff: HandoffEvidence | None = None
    waiting_candidate: RouteAdvance | None = None


class ExecutionSupervisor:
    """Own the active acquisition while an admitted route remains queued."""

    def __init__(self) -> None:
        self._probe: ProbeBodyController | None = None
        self._last_handoff: HandoffEvidence | None = None
        self._last_handoff_controller: BodyController | None = None
        self._route: RouteControl | None = None
        self._pending_route: RouteControl | None = None
        self._route_input_floor = 0
        self._pending_route_input_floor = 0
        self._support_fraction: float | None = None
        self._async_history = deque(maxlen=64)
        self._prepared_approval: PreparedHandoffApproval | None = None
        self._prepared_approval_control: RouteControl | None = None
        self._prepared_approval_frame: NavigationFrame | None = None

    def prepared_approval_is_current(self, approval: PreparedHandoffApproval,
                                     *, control: RouteControl | None = None) -> bool:
        """Check this caller's control against the one permission issued here."""
        if (type(approval) is not PreparedHandoffApproval or approval is not self._prepared_approval
                or type(control) is not RouteControl or control is not self._prepared_approval_control
                or self._route is not control
                or self._probe is not None and self._probe.probe.owned):
            return False
        proposal = approval.proposal
        if (proposal.owner_id != control.owner_id or proposal.route_id != control.route.route_id
                or proposal.route_revision != control.route.route_revision
                or proposal.world_session_id != control.route.world_session
                or proposal.old_action_index != control.action_index):
            return False
        executor = control.executor
        if executor._prepared_handoff_supervisor is not self:
            return False
        anchor, frame, ledger = executor.preparation_anchor, executor._last_prepared_frame, executor.preparation_ledger
        return (anchor is not None and frame is not None and ledger is not None
                and ledger.latest_movement_tick_id == approval.proposal.entry_state.movement_tick_id
                and executor.stage_prepared_action(frame, anchor) == approval.proposal)

    def approve_action_handoff(self, control: RouteControl, proposal: PreparedActionHandoff,
                               frame: NavigationFrame, ledger: InputApplicationLedger) -> PreparedHandoffApproval:
        """Compare real owner/entry/command facts; permission acquires no body use."""
        if (type(control) is not RouteControl or type(proposal) is not PreparedActionHandoff
                or type(frame) is not NavigationFrame or type(ledger) is not InputApplicationLedger):
            raise ContractViolation("prepared handoff approval requires typed current facts")
        reason = None
        if (self._route is not control or self._probe is not None and self._probe.probe.owned
                or control.executor._prepared_handoff_supervisor is not None
                    and control.executor._prepared_handoff_supervisor is not self
                or proposal.owner_id != control.owner_id or proposal.route_id != control.route.route_id
                or proposal.route_revision != control.route.route_revision
                or proposal.old_action_index != control.action_index):
            reason = PreparedHandoffRejectReason.OWNER
        elif not proposal.boundary_reached:
            reason = PreparedHandoffRejectReason.BOUNDARY
        elif ledger is not control.executor.preparation_ledger or ledger.latest_movement_tick_id != proposal.entry_state.movement_tick_id:
            reason = PreparedHandoffRejectReason.LEDGER
        elif proposal.entry_result is not PreparedQueryStatus.READY_MATCH:
            reason = (PreparedHandoffRejectReason.TIMING if proposal.entry_result in {
                PreparedQueryStatus.OVERDUE, PreparedQueryStatus.TIMING_MISMATCH} else PreparedHandoffRejectReason.ENTRY)
        else:
            anchor = control.executor.preparation_anchor
            fresh = None if anchor is None else control.executor.stage_prepared_action(frame, anchor)
            if fresh != proposal:
                reason = PreparedHandoffRejectReason.ENTRY
            elif any(not input_application_has_finished(record, by_tick=proposal.entry_state.movement_tick_id)
                     for record in proposal.conflicting_commands):
                reason = PreparedHandoffRejectReason.COMMANDS
        if reason is not None:
            return PreparedHandoffApproval(PreparedHandoffStatus.REJECTED, proposal, reason=reason)
        approval = PreparedHandoffApproval(PreparedHandoffStatus.APPROVED, proposal, "approval-" + uuid4().hex)
        self._prepared_approval = approval
        self._prepared_approval_control = control
        self._prepared_approval_frame = frame
        # No predicate-binding API: the actual issuer lends its own identity
        # only after it has stored this exact permission for this body owner.
        control.executor._prepared_handoff_supervisor = self
        return approval

    def prepare_action_activation(self, control: RouteControl, approval: PreparedHandoffApproval,
                                  binding: StreamActivationBinding) -> PreparedActivationTransaction | ActivationResult:
        if self._route is not control:
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.OWNER)
        if not ExecutionSupervisor.prepared_approval_is_current(self, approval, control=control):
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.HANDOFF)
        return prepare_action_activation(control, approval, binding)

    def prepare_activation(self, binding: StreamActivationBinding):
        control, approval = self._prepared_approval_control, self._prepared_approval
        if control is None or approval is None:
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.HANDOFF)
        return self.prepare_action_activation(control, approval, binding)

    def _activation_is_current(self, transaction: PreparedActivationTransaction) -> bool:
        control, approval = self._prepared_approval_control, self._prepared_approval
        if (type(transaction) is not PreparedActivationTransaction or approval is None or control is None
                or not self.prepared_approval_is_current(approval, control=control)
                or transaction.permission_token != approval.approval_token
                or transaction.handoff_token != approval.proposal.handoff_token):
            return False
        checked = self.prepare_action_activation(control, approval, transaction.binding)
        return (type(checked) is PreparedActivationTransaction
                and checked.old_action == transaction.old_action
                and checked.new_action is transaction.new_action)

    def accept_activation(self, transaction: PreparedActivationTransaction):
        if not self._activation_is_current(transaction):
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.HANDOFF)
        control, proposal = self._prepared_approval_control, self._prepared_approval.proposal
        registry = control.activation_registry
        handle = control.executor._prepared_handle(proposal.new_action_index)
        if registry is None or handle is None:
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.PROOF)
        if not self._refresh_activation_observation(control, handle):
            return ActivationResult._from_native_references(ActivationStatus.STALE_GENERATION, ActivationRejectReason.GENERATION)
        binding = transaction.binding
        handoff = HandoffEvidence(control.owner_id, control.executor.preparation_anchor.session,
            HandoffDisposition.TRANSFERABLE, proposal.observation_sequence_id,
            binding.submitted_movement_tick, binding.compound_action.movement, "prepared_action_selected",
            control.owner_id, binding.first_control_sequence, proposal.route_revision, proposal.new_action_index)
        accepted = registry.accept_activation(handle, transaction, handoff)
        if (type(accepted) is PreparedActivationAcceptance and not self._activation_is_current(transaction)):
            registry.abort_activation(accepted)
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.HANDOFF)
        return accepted

    @staticmethod
    def _refresh_activation_observation(control, handle):
        executor, coordinator = control.executor, control.coordinator
        anchor, ledger, frame = executor.preparation_anchor, executor.preparation_ledger, executor._last_prepared_frame
        if coordinator is None or anchor is None or ledger is None or frame is None or frame.world._owner is None:
            return False
        versions = frame.world._owner.dependency_versions(tuple(position for position, _ in handle.dependency_versions))
        remaining = max(0., coordinator.damage_budget.maximum_expected_damage_points
                        - executor.completed_movement_damage_points)
        return control.activation_registry.refresh_activation_observation(handle,
            scope=coordinator.computation_scope, state=anchor.physics_state, dependency_versions=versions,
            ledger_id=ledger.ledger_identity, damage_remaining=remaining)

    def deliver_activation(self, transaction: PreparedActivationTransaction, accepted: PreparedActivationAcceptance):
        control = self._prepared_approval_control
        if not self._activation_is_current(transaction):
            if control is not None and control.activation_registry is not None:
                control.activation_registry.abort_activation(accepted)
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.HANDOFF)
        handle = control.executor._prepared_handle(self._prepared_approval.proposal.new_action_index)
        delivered = control.executor.deliver_prepared_activation(handle, transaction, accepted)
        if type(delivered) is ActivationResult:
            control.activation_registry.abort_activation(accepted)
            control.executor.abort_prepared_activation()
        return delivered

    def commit_activation(self, transaction: PreparedActivationTransaction, accepted: PreparedActivationAcceptance,
                          delivered: PreparedActivationDelivery) -> ActivationResult:
        control = self._prepared_approval_control
        if not self._activation_is_current(transaction):
            if control is not None and control.activation_registry is not None:
                control.activation_registry.abort_activation(accepted)
                control.executor.abort_prepared_activation()
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.HANDOFF)
        handle = control.executor._prepared_handle(self._prepared_approval.proposal.new_action_index)
        if handle is None or not self._refresh_activation_observation(control, handle):
            control.activation_registry.abort_activation(accepted)
            control.executor.abort_prepared_activation()
            return ActivationResult._from_native_references(ActivationStatus.STALE_GENERATION, ActivationRejectReason.GENERATION)
        frame = self._prepared_approval_frame
        if (type(frame) is not NavigationFrame
                or frame.body.sequence_id
                    != self._prepared_approval.proposal.observation_sequence_id):
            control.activation_registry.abort_activation(accepted)
            control.executor.abort_prepared_activation()
            return ActivationResult._from_native_references(
                ActivationStatus.ACTIVATION_MISMATCH,
                ActivationRejectReason.HANDOFF,
            )
        commit = control.prepare_prepared_activation_commit(
            delivered,
            frame=frame,
            observation_sequence_id=(
                self._prepared_approval.proposal.observation_sequence_id
            ),
        )
        if type(commit) is ActivationResult:
            control.activation_registry.abort_activation(accepted)
            control.executor.abort_prepared_activation()
            return commit
        result = control.activation_registry.commit_activation(
            accepted, delivered, action_commit=commit,
        )
        if result.status is not ActivationStatus.ACTIVATED:
            control.activation_registry.abort_activation(accepted)
            control.executor.abort_prepared_activation()
        else:
            commit.finalize()
        return result

    @property
    def active_activation(self):
        control = self._route
        controller = None if control is None else control.executor.prepared_controller
        return None if controller is None else controller.stream_tracker.activation_binding

    @property
    def next_command_index(self) -> int:
        control = self._route
        controller = None if control is None else control.executor.prepared_controller
        return 0 if controller is None else controller._command_index

    def record_application(self, application: FirstStreamApplication) -> StreamApplicationResult:
        control = self._route
        controller = None if control is None else control.executor.prepared_controller
        ledger = None if control is None else control.executor.preparation_ledger
        fact = None if ledger is None else ledger.stream_command(application.control_sequence)
        if (controller is None or fact is None
                or fact.generation_id != application.generation_id
                or controller.prepared_handle.generation_id != application.generation_id):
            return StreamApplicationResult._from_native_references(StreamApplicationStatus.REJECTED, reason=StreamApplicationReason.GENERATION)
        record = ledger.record(application.control_sequence)
        sample = next((sample for sample in record.samples if sample.movement_tick_id == application.effect_tick), None)
        movement = application.movement
        if (record.status not in {InputApplicationStatus.APPLIED, InputApplicationStatus.APPLIED_OUTSIDE_WINDOW}
                or sample is None or sample.state not in {"leased", "neutral"}
                or (sample.forward, sample.strafe, sample.jump, sample.sneak, sample.sprint)
                != (movement.forward, movement.strafe, movement.jump, movement.sneak, movement.sprint)):
            return controller.record_missing_application(command_index=fact.command_index)
        return controller.record_application(application, command_index=fact.command_index)

    def record_missing_application(self, *, command_index: int):
        controller = None if self._route is None else self._route.executor.prepared_controller
        return None if controller is None else controller.record_missing_application(command_index=command_index)

    def abort_activation(self, accepted):
        control = self._prepared_approval_control
        if control is not None and control.activation_registry is not None:
            control.activation_registry.abort_activation(accepted)
            control.executor.abort_prepared_activation()

    def record_command(self, action):
        """Associate the next actual command with the already executing proof."""
        control = self._route
        controller = None if control is None else control.executor.prepared_controller
        if controller is None or controller.state.value != "running":
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.HANDOFF)
        handle, tracker, index = controller.prepared_handle, controller.stream_tracker, controller._command_index
        if tracker.start_branch is None or index >= len(handle.hot_view.commands):
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.WINDOW)
        ledger = control.executor.preparation_ledger
        command = handle.hot_view.commands[index]
        if (ledger is None or ledger.ledger_identity != handle.hot_view.ledger_id
                or ledger.latest_movement_tick_id + 1 != tracker.expected_effect_tick(index)
                or action.movement != command.movement or action.valid_for_ticks != 1):
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.MOVEMENT)
        result = control.activation_registry.record_command(handle, command_index=index, action=action)
        from mc2p.motion_nav.prepared_motion import ReferenceStatus
        return ActivationResult._from_native_references(
            ActivationStatus.ACTIVATED if result.status is ReferenceStatus.RETAINED else ActivationStatus.ACTIVATION_MISMATCH,
            None if result.status is ReferenceStatus.RETAINED else ActivationRejectReason.PROOF)

    @property
    def async_work_diagnostics(self):
        current = tuple(control.coordinator.async_diagnostics
                        for control in (self._route, self._pending_route)
                        if control is not None and control.coordinator is not None)
        return tuple(self._async_history) + current

    def _retire_control_work(self, control, cause) -> None:
        control.request_stop(cause)
        if control.coordinator is not None:
            self._async_history.append(control.coordinator.async_diagnostics)

    @property
    def route(self) -> RouteControl | None:
        return self._pending_route or self._route

    @property
    def incumbent_route(self) -> RouteControl | None:
        return self._route

    @property
    def effective_dependencies(self) -> tuple[BlockPos, ...]:
        return tuple(sorted({
            position
            for control in (self._route, self._pending_route)
            if control is not None
            for position in control.effective_dependencies
        }))

    def has_owned_body_control(self, *, route_source_bound: bool) -> bool:
        """Read actual retained objects, independent of diagnostic labels."""
        return (self._probe is not None and self._probe.probe.owned) or (
            self._route is not None and route_source_bound)

    def activities(self, frame: NavigationFrame, decision=None, *, source_bound=True):
        """Report current owned objects; past handoffs confer no ownership."""
        result = []
        if self._probe is not None and self._probe.probe.owned:
            result.append(BodyControlActivity(frame.session, frame.body.sequence_id,
                self._probe.owner_id, None, None, None,
                BodyControlPhase.STOPPING if self._probe.probe.state is LandingEdgeProbeState.STOPPING
                else BodyControlPhase.ACQUISITION))
        if self._route is not None and source_bound:
            result.append(self._route.activity(frame, decision))
        return tuple(result)

    @property
    def has_pending_route(self) -> bool:
        return self._pending_route is not None

    def control_by_id(self, route_id: str) -> RouteControl | None:
        for control in (self._pending_route, self._route):
            if control is not None and control.route.route_id == route_id:
                return control
        return None

    def discard_pending_route(self) -> None:
        if self._pending_route is not None:
            self._retire_control_work(self._pending_route, StopCause.ROUTE_REPLACED)
        self._pending_route = None
        self._pending_route_input_floor = 0

    def advance_body(
        self, frame: NavigationFrame,
        ledger: InputApplicationLedger | None,
        anchor: StateAnchor | None, *,
        conditioned_yaw_delta_degrees: float | None = None,
        allow_grounded_reprepare: bool = False,
        result_poll_sequence: int | None = None,
        current_scope: AsyncComputationScope | None = None,
    ) -> BodyFrameAdvance:
        validation = self._validate_routes(frame)
        control = self.route
        if control is None:
            raise ContractViolation("body advancement requires an admitted route")
        if self._probe is not None and self._probe.probe.owned:
            # Probe control is proved under the observed view, including the
            # route's first command that has yet to win Runtime selection.
            conditioned_yaw_delta_degrees = None
        advance = control.advance(
            frame, ledger, anchor,
            conditioned_yaw_delta_degrees=conditioned_yaw_delta_degrees,
            allow_grounded_reprepare=(allow_grounded_reprepare
                                     and not self.has_pending_route),
            result_poll_sequence=result_poll_sequence,
            current_scope=current_scope,
        )
        progress = advance.progress_validation
        if (progress is not None
                and progress.disposition
                    is ActiveRouteValidationDisposition.STOP):
            if control is self._pending_route:
                self._retire_control_work(
                    control, StopCause.DEPENDENCY_CHANGED,
                )
                self._pending_route = None
                self._pending_route_input_floor = 0
                validation = BodyRouteValidation(
                    frame.body.sequence_id, validation.incumbent, progress,
                )
                return self._advance_incumbent_prefix(
                    advance, frame, ledger, anchor,
                    conditioned_yaw_delta_degrees=(
                        conditioned_yaw_delta_degrees
                    ),
                    result_poll_sequence=result_poll_sequence,
                    current_scope=current_scope,
                    route_validation=validation,
                )
            control.request_stop(StopCause.DEPENDENCY_CHANGED)
            advance = control.stop_protection(advance, frame, anchor)
            validation = BodyRouteValidation(
                frame.body.sequence_id, progress, validation.pending,
            )
        if self._pending_route is None or advance.decision.submit_input:
            return BodyFrameAdvance(advance, route_validation=validation)
        if advance.decision.state in _REJECTED_CANDIDATE_STATES:
            self.discard_pending_route()
            # Session must route this fact before the incumbent advances:
            # denied recovery may have requested stopping in the same frame.
            return BodyFrameAdvance(
                advance, advance, advance, validation,
            )
        return self._advance_incumbent_prefix(
            advance, frame, ledger, anchor,
            conditioned_yaw_delta_degrees=conditioned_yaw_delta_degrees,
            result_poll_sequence=result_poll_sequence,
            current_scope=current_scope,
            route_validation=validation,
        )

    def _validate_routes(self, frame: NavigationFrame) -> BodyRouteValidation:
        """Validate incumbent then pending under one per-frame query budget."""
        if type(frame) is not NavigationFrame:
            raise ContractViolation("route validation requires a navigation frame")
        incumbent_control = self._route
        pending_control = self._pending_route
        budget = (
            None if not frame.changed_cells else RouteValidationBudget(4)
        )
        cache = (
            None if not frame.changed_cells else WorldQueryCache(frame.world)
        )

        def validate(
            control: RouteControl | None,
        ) -> ActiveRouteValidation | None:
            if control is None:
                return None
            assert control.tracker is not None
            kwargs = {
                "ground_profile": getattr(
                    control.executor, "ground_profile", None,
                ),
                "action_index": control.action_index,
                "expected_identity": control.validation_identity,
            }
            if frame.changed_cells:
                kwargs.update(query_cache=cache, budget=budget)
            return control.tracker.validate(
                frame.world, frame.changed_cells, **kwargs,
            )

        incumbent = validate(incumbent_control)
        if (incumbent is not None
                and incumbent.disposition
                    is ActiveRouteValidationDisposition.STOP):
            assert incumbent_control is not None
            incumbent_control.request_stop(StopCause.DEPENDENCY_CHANGED)
        pending = validate(pending_control)
        if (pending is not None
                and pending.disposition
                    is ActiveRouteValidationDisposition.STOP):
            assert pending_control is not None
            self._retire_control_work(
                pending_control, StopCause.DEPENDENCY_CHANGED,
            )
            self._pending_route = None
            self._pending_route_input_floor = 0
        return BodyRouteValidation(
            frame.body.sequence_id, incumbent, pending,
        )

    def continue_rejected_candidate(
        self, advance: BodyFrameAdvance, frame: NavigationFrame,
        ledger: InputApplicationLedger | None,
        anchor: StateAnchor | None, *,
        conditioned_yaw_delta_degrees: float | None = None,
        result_poll_sequence: int | None = None,
        current_scope: AsyncComputationScope | None = None,
    ) -> BodyFrameAdvance:
        """Resume only after Session routes the typed candidate rejection."""
        if (advance.rejected_candidate is None
                or advance.route_advance is not advance.rejected_candidate):
            raise ContractViolation("continuation requires an unadvanced rejection")
        if self._probe is not None and self._probe.probe.owned:
            conditioned_yaw_delta_degrees = None
        return self._advance_incumbent_prefix(
            advance.rejected_candidate, frame, ledger, anchor,
            conditioned_yaw_delta_degrees=conditioned_yaw_delta_degrees,
            result_poll_sequence=result_poll_sequence,
            current_scope=current_scope,
            rejected=advance.rejected_candidate,
            route_validation=advance.route_validation,
        )

    def _advance_incumbent_prefix(
        self, candidate: RouteAdvance, frame: NavigationFrame,
        ledger: InputApplicationLedger | None,
        anchor: StateAnchor | None, *,
        conditioned_yaw_delta_degrees: float | None,
        result_poll_sequence: int | None,
        rejected: RouteAdvance | None = None,
        current_scope: AsyncComputationScope | None = None,
        route_validation: BodyRouteValidation | None = None,
    ) -> BodyFrameAdvance:
        incumbent = self._route
        assert incumbent is not None
        # A waiting successor never retires the existing input owner.
        prefix = incumbent.advance(
            frame, ledger, anchor,
            conditioned_yaw_delta_degrees=conditioned_yaw_delta_degrees,
            allow_grounded_reprepare=False,
            result_poll_sequence=result_poll_sequence,
            current_scope=current_scope,
        )
        progress = prefix.progress_validation
        if (progress is not None
                and progress.disposition
                    is ActiveRouteValidationDisposition.STOP):
            incumbent.request_stop(StopCause.DEPENDENCY_CHANGED)
            prefix = incumbent.stop_protection(prefix, frame, anchor)
            route_validation = BodyRouteValidation(
                frame.body.sequence_id,
                progress,
                (None if route_validation is None
                 else route_validation.pending),
            )
        return BodyFrameAdvance(
            prefix, candidate, rejected, route_validation,
        )

    def select_body(
        self, advance: BodyFrameAdvance, checked_decision: ActionRouteDecision,
        frame: NavigationFrame,
        ledger: InputApplicationLedger | None,
        anchor: StateAnchor | None,
    ) -> BodyFrameResult:
        """Select after Session applies task risk; never publish task results."""
        if (advance.rejected_candidate is not None
                and advance.route_advance is advance.rejected_candidate):
            raise ContractViolation("candidate rejection must be routed before body selection")
        control = advance.route_advance.control
        if (self._probe is not None and self._probe.probe.owned
                and self._probe.probe.state is LandingEdgeProbeState.STOPPING):
            # Planning can request a probe stop after this frame's initial
            # handoff pass. Its protection takes precedence over route facts,
            # including a neutral RUNNING route without a waiting candidate.
            return BodyFrameResult(BodySelectionKind.PROBE_STOP, self._probe,
                advance.route_advance, checked_decision,
                self._route, self._pending_route,
                waiting_candidate=advance.waiting_candidate)
        if advance.waiting_candidate is None:
            return BodyFrameResult(BodySelectionKind.ROUTE, control,
                advance.route_advance, checked_decision,
                self._route, self._pending_route)
        release = None
        if checked_decision.state in _TERMINAL_DECISIONS:
            if self._probe is not None and self._probe.probe.owned:
                return BodyFrameResult(BodySelectionKind.PROBE_STOP, self._probe,
                    advance.route_advance, checked_decision,
                    self._route, self._pending_route,
                    waiting_candidate=advance.waiting_candidate)
            release = self.incumbent_release_evidence(frame, ledger, anchor)
            if release.disposition is HandoffDisposition.QUIESCENT:
                return BodyFrameResult(BodySelectionKind.CANDIDATE_WAIT, control,
                    advance.route_advance, checked_decision,
                    self._route, self._pending_route, release,
                    advance.waiting_candidate)
        return BodyFrameResult(BodySelectionKind.INCUMBENT_PREFIX, control,
            advance.route_advance, checked_decision,
            self._route, self._pending_route, release,
            advance.waiting_candidate)

    def stop_protection(
        self, advance: BodyFrameAdvance, frame: NavigationFrame,
        ledger: InputApplicationLedger | None,
        anchor: StateAnchor | None, *,
        current_scope: AsyncComputationScope | None = None,
    ) -> BodyFrameAdvance:
        """Keep the real incumbent after Session accepts the ending request."""
        incumbent = self._route
        if incumbent is None:
            raise ContractViolation("stop protection requires a retained incumbent")
        if incumbent is advance.route_advance.control:
            protected = incumbent.stop_protection(advance.route_advance, frame, anchor)
        else:
            # A refused pending route never owned input.  Its predecessor was
            # not advanced this frame and now supplies its normal cancel step.
            protected = incumbent.advance(frame, ledger, anchor, current_scope=current_scope)
        return BodyFrameAdvance(
            protected, route_validation=advance.route_validation,
        )

    def request_route_stop(self, cause: StopCause) -> None:
        self.discard_pending_route()
        if self._route is not None:
            self._route.request_stop(cause)

    def offer_route(
        self, candidate: RouteControl, frame: NavigationFrame,
        ledger: InputApplicationLedger | None = None,
        anchor: StateAnchor | None = None,
    ) -> bool:
        if type(candidate) is not RouteControl:
            raise ContractViolation("route successor must be typed")
        if candidate.route.world_session != frame.session.value:
            raise ContractViolation("route successor belongs to another world")
        if self._pending_route is candidate:
            return True
        if self._pending_route is not None:
            # A pending route has not won Runtime arbitration, so none of its
            # commands can own the body.  A newer admitted route may replace
            # that candidate while the incumbent keeps moving.  Retaining the
            # older candidate would either execute a superseded target or make
            # moving-target goal revisions crash the whole Runtime.
            self.discard_pending_route()
        if self._route is None:
            self._route = candidate
            self._route_input_floor = max(
                (record.control_sequence for record in ledger.snapshot()),
                default=0,
            ) if ledger is not None else 0
            return True
        responsibility = assess_input_responsibility(
            ledger, anchor,
            previous_sequence_floor=self._route_input_floor,
        )
        if (ledger is None or anchor is None
                or responsibility.disposition not in {
                    InputResponsibilityDisposition.CLEAR,
                    InputResponsibilityDisposition.TRANSFERABLE_FROM_CURRENT_ANCHOR,
                }
                or self._route.requires_safe_handoff(frame)):
            return False
        self._pending_route = candidate
        self._pending_route_input_floor = max(
            (record.control_sequence for record in ledger.snapshot()),
            default=0,
        )
        return True

    def adopt_route_selection(
        self, frame: NavigationFrame, *, selected: bool,
        control_sequence: int | None = None,
        movement: MovementV1 | None = None,
        movement_tick_id: int | None = None,
    ) -> HandoffEvidence | None:
        successor = self._pending_route
        if successor is None:
            return None
        if not selected:
            self.discard_pending_route()
            return None
        if (control_sequence is None or movement is None
                or movement == MovementV1()):
            raise ContractViolation("route transfer requires selected movement")
        predecessor = self._route
        assert predecessor is not None
        handoff = HandoffEvidence(
            predecessor.owner_id, frame.session,
            HandoffDisposition.TRANSFERABLE,
            frame.body.sequence_id, movement_tick_id, movement,
            "selected_successor_route_command",
            successor.route.route_id, control_sequence,
            successor.route.route_revision,
            successor.action_index,
        )
        self._retire_control_work(predecessor, StopCause.ROUTE_REPLACED)
        self._route = successor
        self._route_input_floor = self._pending_route_input_floor
        self._pending_route = None
        self._last_handoff = handoff
        return handoff

    def evaluate_quiescence(
        self, frame: NavigationFrame,
        ledger: InputApplicationLedger | None,
        anchor: StateAnchor | None,
    ) -> HandoffEvidence:
        route = self._route
        probe = self._probe
        owner = (
            probe.owner_id
            if probe is not None and probe.probe.owned else
            (route.owner_id if route is not None else "navigation-session")
        )
        reason = "body_owner_still_active"
        quiescent = False
        if probe is not None and probe.probe.owned:
            evidence = probe.safe_to_release(
                frame, ledger, anchor, input_floor=0,
            )
            self._last_handoff = evidence
            self._last_handoff_controller = probe
            return evidence
        if route is not None:
            evidence = route.safe_to_release(
                frame, ledger, anchor, input_floor=self._route_input_floor,
            )
            self._last_handoff = evidence
            self._last_handoff_controller = route
            return evidence
        if (ledger is None or anchor is None
                or anchor.session != frame.session
                or anchor.observation_sequence_id != frame.body.sequence_id):
            reason = "current_body_or_input_evidence_missing"
        elif assess_input_responsibility(
            ledger, anchor,
            previous_sequence_floor=self._route_input_floor,
        ).disposition not in {
            InputResponsibilityDisposition.CLEAR,
            InputResponsibilityDisposition.TRANSFERABLE_FROM_CURRENT_ANCHOR,
        }:
            reason = "input_responsibility_unresolved"
        elif math.hypot(
            frame.body.velocity_blocks_per_second[0],
            frame.body.velocity_blocks_per_second[2],
        ) > .10:
            reason = "current_body_still_moving"
        else:
            predicted = verified_ground_rollout(
                frame, anchor.physics_state, MovementV1(),
                control_ticks=0, tail_ticks=8, minimum_support=.01,
            )
            if predicted is None:
                reason = "released_input_tail_unproven"
            elif math.hypot(
                predicted.velocity_blocks_per_tick[0],
                predicted.velocity_blocks_per_tick[2],
            ) * 20.0 > .10:
                reason = "released_input_tail_still_moving"
            else:
                quiescent = True
                reason = "supported_released_input_tail_verified"
        handoff = HandoffEvidence(
            owner, frame.session,
            HandoffDisposition.QUIESCENT if quiescent
            else HandoffDisposition.RETAIN,
            frame.body.sequence_id,
            None if ledger is None else ledger.latest_movement_tick_id,
            MovementV1(), reason,
        )
        self._last_handoff = handoff
        self._last_handoff_controller = None
        return handoff

    def incumbent_release_evidence(
        self, frame: NavigationFrame,
        ledger: InputApplicationLedger | None,
        anchor: StateAnchor | None,
    ) -> HandoffEvidence:
        """Check only the installed predecessor while a successor waits."""
        if self._route is None:
            raise ContractViolation("execution supervisor has no incumbent route")
        evidence = self._route.safe_to_release(
            frame, ledger, anchor, input_floor=self._route_input_floor,
        )
        self._last_handoff = evidence
        self._last_handoff_controller = self._route
        return evidence

    def retire_route(
        self, frame: NavigationFrame,
        ledger: InputApplicationLedger | None,
        anchor: StateAnchor | None,
    ) -> bool:
        if self._pending_route is not None:
            return False
        if self._route is None:
            return True
        evidence = self.evaluate_quiescence(frame, ledger, anchor)
        if (evidence.disposition is not HandoffDisposition.QUIESCENT
                or evidence.owner_id != self._route.owner_id):
            return False
        self._retire_control_work(self._route, StopCause.CLOSED)
        self._route = None
        self._route_input_floor = 0
        return True

    @property
    def probe(self):
        return None if self._probe is None else self._probe.probe

    @probe.setter
    def probe(self, value) -> None:
        if (self._probe is not None and self._probe.probe.owned
                and value is not self._probe.probe):
            raise ContractViolation("cannot discard an active body acquisition")
        self._probe = None if value is None else ProbeBodyController(value)
        self._last_handoff = None
        self._last_handoff_controller = None

    @property
    def last_handoff(self) -> HandoffEvidence | None:
        return self._last_handoff

    def request_probe_stop(self, cause: StopCause) -> bool:
        if type(cause) is not StopCause:
            raise ContractViolation("body stop cause must be typed")
        if self._probe is None or not self._probe.probe.owned:
            return False
        self._probe.request_stop(cause)
        self._last_handoff = None
        self._last_handoff_controller = None
        return True

    def decide_probe(
        self, frame: NavigationFrame,
        ledger: InputApplicationLedger | None,
        anchor: StateAnchor | None,
    ) -> BodyControlDecision:
        controller = self._probe
        if controller is None:
            raise ContractViolation("supervisor has no edge acquisition")
        decision = controller.decide(frame, ledger, anchor)
        self._last_handoff = decision.handoff
        self._last_handoff_controller = controller
        return decision

    def probe_outcome(self, frame: NavigationFrame) -> ProbeOutcome | None:
        return None if self._probe is None else self._probe.outcome(frame)

    def finish_probe_release(self, frame: NavigationFrame) -> ProbeOutcome | None:
        if self._probe is None or not self._probe.probe.finish_release(frame):
            return None
        return self._probe.outcome(frame)

    def retire_quiescent_probe(self, frame: NavigationFrame) -> None:
        if self._probe is None:
            return
        if (self._last_handoff is None
                or self._last_handoff_controller is not self._probe
                or self._last_handoff.world_session != frame.session
                or self._last_handoff.observation_sequence_id
                    != frame.body.sequence_id
                or self._last_handoff.owner_id != self._probe.owner_id
                or self._last_handoff.disposition is not
                    HandoffDisposition.QUIESCENT):
            raise ContractViolation("edge acquisition has no release evidence")
        self._probe.probe.end(self._last_handoff.reason)
        self._probe = None
        self._last_handoff_controller = None

    def transfer_probe_to_route(
        self, frame: NavigationFrame, *, route_id: str,
        route_revision: int, action_index: int,
        movement: MovementV1, control_sequence: int,
        movement_tick_id: int | None,
    ) -> HandoffEvidence:
        controller = self._probe
        if (controller is None or not controller.probe.ready
                or type(movement) is not MovementV1
                or movement == MovementV1()
                or not controller.probe.belongs_to_action(
                    route_id, route_revision, action_index,
                )):
            raise ContractViolation("probe transfer requires a selected route movement")
        handoff = HandoffEvidence(
            controller.owner_id,
            frame.session, HandoffDisposition.TRANSFERABLE,
            frame.body.sequence_id, movement_tick_id, movement,
            "selected_route_command_owns_next_input", route_id,
            control_sequence, route_revision, action_index,
        )
        controller.probe.end("selected_route_command_handoff")
        self._probe = None
        self._last_handoff = handoff
        self._last_handoff_controller = None
        return handoff

    @property
    def support_fraction(self) -> float | None:
        return self._support_fraction

    def record_support_fraction(self, value: float | None) -> None:
        if (value is not None
                and (type(value) not in (int, float)
                     or not math.isfinite(float(value))
                     or not 0.0 <= float(value) <= 1.0)):
            raise ContractViolation("support fraction must be in [0, 1]")
        self._support_fraction = None if value is None else float(value)
