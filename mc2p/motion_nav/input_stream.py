"""Bounded tracker for Runtime-activated strict input streams.

This records receipt facts only. It does not create generations, register
proofs, assign Runtime sequences, or take ownership of the player's body.
"""
from __future__ import annotations

from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.motion_activation import (
    ActivationRejectReason, ActivationResult, ActivationStatus, FirstStreamApplication,
    InputStreamCommandFact, StreamActivationBinding, StreamApplicationReason,
    StreamApplicationResult, StreamApplicationStatus, StreamStartBranch,
)
from mc2p.motion_nav.online_motion import ProjectionStatus, project_movement_command
from mc2p.motion_nav.prepared_motion import StreamGeneration


class InputStreamTracker:
    __slots__ = ("_generation", "_binding", "_first_application", "_start_branch", "_command_facts")

    def __init__(self, generation: StreamGeneration) -> None:
        if type(generation) is not StreamGeneration:
            raise ContractViolation("stream tracker requires a typed generation")
        self._generation = generation
        self._binding: StreamActivationBinding | None = None
        self._first_application: FirstStreamApplication | None = None
        self._start_branch: StreamStartBranch | None = None
        self._command_facts: list[InputStreamCommandFact] = []

    @classmethod
    def start(cls, generation: StreamGeneration) -> InputStreamTracker:
        return cls(generation)

    @property
    def generation(self) -> StreamGeneration:
        return self._generation

    @property
    def start_branch(self) -> StreamStartBranch | None:
        return self._start_branch

    @property
    def command_facts(self) -> tuple[InputStreamCommandFact, ...]:
        return tuple(self._command_facts)

    @property
    def activation_binding(self) -> StreamActivationBinding | None:
        return self._binding

    def bind_activation(self, binding: StreamActivationBinding) -> ActivationResult:
        if type(binding) is not StreamActivationBinding:
            raise ContractViolation("activation requires a typed binding")
        if binding.generation_id != self.generation.generation_id:
            return ActivationResult._from_native_references(ActivationStatus.STALE_GENERATION, ActivationRejectReason.GENERATION)
        if binding.owner_id != self.generation.owner_id:
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.OWNER)
        if binding.world_session_id != self.generation.entry_identity[0].session.value:
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.WORLD_SESSION)
        window = self.generation.requested_window
        if (binding.requested_window.earliest_tick != window.earliest_start_tick
                or binding.requested_window.latest_tick != window.latest_start_tick):
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.WINDOW)
        first = self.generation.expected_inputs[0]
        projection = project_movement_command(self.generation.entry_identity[0], binding.compound_action.movement,
                                              movement_yaw_radians=first.movement_yaw_radians)
        if projection.status is not ProjectionStatus.READY or projection.tick_input != first:
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.MOVEMENT)
        if self._binding is not None:
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.ALREADY_BOUND)
        self._binding = binding
        return ActivationResult._from_native_references(ActivationStatus.ACTIVATED)

    def _result(self, status: StreamApplicationStatus,
                reason: StreamApplicationReason | None = None) -> StreamApplicationResult:
        return StreamApplicationResult._from_native_references(status, self._start_branch, reason)

    def _matches_input(self, application: FirstStreamApplication, index: int) -> bool:
        projection = project_movement_command(self.generation.entry_identity[0], application.movement,
                                              movement_yaw_radians=application.movement_yaw_radians)
        return projection.status is ProjectionStatus.READY and projection.tick_input == self.generation.expected_inputs[index]

    def record_first_application(self, application: FirstStreamApplication) -> StreamApplicationResult:
        if type(application) is not FirstStreamApplication:
            raise ContractViolation("stream application requires a typed receipt")
        rejected = StreamApplicationStatus.REJECTED
        if self._first_application is not None:
            return self._result(rejected, StreamApplicationReason.DUPLICATE)
        if self._binding is None:
            return self._result(rejected, StreamApplicationReason.ACTIVATION_MISSING)
        if application.generation_id != self.generation.generation_id:
            return self._result(rejected, StreamApplicationReason.GENERATION)
        if application.control_sequence != self._binding.first_control_sequence:
            return self._result(rejected, StreamApplicationReason.SEQUENCE)
        if not self._matches_input(application, 0):
            return self._result(rejected, StreamApplicationReason.INPUTS)
        window = self.generation.requested_window
        offset = application.effect_tick - window.earliest_start_tick
        if offset not in (0, 1) or not window.allows_start(application.effect_tick):
            return self._result(rejected, StreamApplicationReason.TIMING)
        self._first_application = application
        self._start_branch = StreamStartBranch.ON_TIME if offset == 0 else StreamStartBranch.LATE_ONE
        self._command_facts.append(InputStreamCommandFact._from_native_references(application.control_sequence, application.generation_id, 0))
        return self._result(StreamApplicationStatus.APPLIED)

    def record_application(self, application: FirstStreamApplication, *, command_index: int) -> StreamApplicationResult:
        if type(application) is not FirstStreamApplication:
            raise ContractViolation("stream application requires a typed receipt")
        require_nonnegative_int(command_index, "command index")
        deviation = StreamApplicationStatus.DEVIATION
        if self._first_application is None:
            return self._result(StreamApplicationStatus.REJECTED, StreamApplicationReason.FIRST_APPLICATION_MISSING)
        if application.generation_id != self.generation.generation_id:
            return self._result(deviation, StreamApplicationReason.GENERATION)
        if command_index != len(self._command_facts) or command_index >= len(self.generation.expected_inputs):
            return self._result(deviation, StreamApplicationReason.COMMAND_INDEX)
        if application.control_sequence <= self._command_facts[-1].sequence:
            return self._result(deviation, StreamApplicationReason.SEQUENCE)
        if application.effect_tick != self.expected_effect_tick(command_index):
            return self._result(deviation, StreamApplicationReason.TIMING)
        if not self._matches_input(application, command_index):
            return self._result(deviation, StreamApplicationReason.INPUTS)
        self._command_facts.append(InputStreamCommandFact._from_native_references(application.control_sequence, application.generation_id, command_index))
        return self._result(StreamApplicationStatus.APPLIED)

    def expected_effect_tick(self, command_index: int) -> int:
        require_nonnegative_int(command_index, "command index")
        if command_index >= len(self.generation.expected_inputs):
            raise ContractViolation("command index is outside this stream")
        if self._first_application is None:
            raise ContractViolation("first actual application has not selected the stream start branch")
        return self._first_application.effect_tick + command_index
