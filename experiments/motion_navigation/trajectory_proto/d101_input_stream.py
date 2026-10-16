"""Isolated D101 contracts; no scheduling, wall clock or production owner."""
from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum
import math

from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int
from mc2p.motion_nav.physics_types import PhysicsState, TickInput


class StreamStartBranch(StrEnum):
    ON_TIME = "ON_TIME"
    LATE_ONE = "LATE_ONE"


def _identity(value: tuple, name: str) -> None:
    """Require recursively immutable identity values, including version tuples."""
    if type(value) is not tuple or not value:
        raise ContractViolation(f"{name} must be a nonempty immutable tuple")
    for item in value:
        if type(item) is tuple:
            _identity(item, name)
        elif type(item) not in (str, int):
            raise ContractViolation(f"{name} must contain only strings, integers and tuples")


@dataclass(frozen=True, slots=True)
class StreamGeneration:
    generation_id: str
    owner_id: str
    request_id: str
    action_index: int
    requested_start_tick: int
    commands: tuple[TickInput, ...]
    dependency_identity: tuple
    first_control_sequence: int

    def __post_init__(self) -> None:
        for name in ("generation_id", "owner_id", "request_id"):
            require_identifier(getattr(self, name), name)
        for name in ("action_index", "requested_start_tick", "first_control_sequence"):
            require_nonnegative_int(getattr(self, name), name)
        if (type(self.commands) is not tuple or not self.commands
                or any(type(command) is not TickInput for command in self.commands)):
            raise ContractViolation("commands must be a nonempty immutable TickInput tuple")
        _identity(self.dependency_identity, "dependency identity")


@dataclass(frozen=True, slots=True)
class FirstApplicationReceipt:
    control_sequence: int
    effect_tick: int
    command: TickInput

    def __post_init__(self) -> None:
        require_nonnegative_int(self.control_sequence, "control sequence")
        require_nonnegative_int(self.effect_tick, "effect tick")
        if type(self.command) is not TickInput:
            raise ContractViolation("first application requires a typed command")


class KeyNodeCheckKind(StrEnum):
    COMPARE_ONLY = "COMPARE_ONLY"
    REPROOF_REQUIRED = "REPROOF_REQUIRED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class KeyNodeCheck:
    kind: KeyNodeCheckKind
    reason: str
    full_proof_calls: int
    compared_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.kind) is not KeyNodeCheckKind:
            raise ContractViolation("key-node result requires a typed check kind")
        require_identifier(self.reason, "check reason")
        require_nonnegative_int(self.full_proof_calls, "full proof calls")
        if (type(self.compared_fields) is not tuple
                or any(type(name) is not str for name in self.compared_fields)):
            raise ContractViolation("compared fields must be immutable field names")


def _bounds(lower: tuple, upper: tuple, size: int, name: str) -> None:
    if any(type(value) is not tuple or len(value) != size for value in (lower, upper)):
        raise ContractViolation(f"{name} bounds must be immutable {size}-tuples")
    if (any(type(value) not in (float, int) or not math.isfinite(value)
            for value in lower + upper) or any(lo > hi for lo, hi in zip(lower, upper))):
        raise ContractViolation(f"{name} bounds must be finite and ordered")


@dataclass(frozen=True, slots=True)
class KeyNodeEnvelope:
    """Ranges already covered by evidence, never expanded by this comparator.

    All other PhysicsState fields match reference_state exactly. In particular,
    pose and on_ground are not inferred from a diagnostic external-force label.
    Geometry support facts remain bound by generation.dependency_identity.
    """

    generation: StreamGeneration
    start_branch: StreamStartBranch
    command_index: int
    reference_state: PhysicsState
    position_min: tuple[float, float, float]
    position_max: tuple[float, float, float]
    velocity_min: tuple[float, float, float]
    velocity_max: tuple[float, float, float]
    yaw_range: tuple[float, float]
    pitch_range: tuple[float, float]
    commitment_command_index: int | None

    def __post_init__(self) -> None:
        if type(self.generation) is not StreamGeneration or type(self.reference_state) is not PhysicsState:
            raise ContractViolation("key node requires a typed generation and reference state")
        if type(self.start_branch) is not StreamStartBranch:
            raise ContractViolation("key node requires a typed start branch")
        require_nonnegative_int(self.command_index, "key node command index")
        if self.command_index >= len(self.generation.commands):
            raise ContractViolation("key node is outside its generation")
        if self.commitment_command_index is not None:
            require_nonnegative_int(self.commitment_command_index, "commitment command index")
            if self.commitment_command_index >= len(self.generation.commands):
                raise ContractViolation("commitment boundary is outside its generation")
        effect_tick = (self.generation.requested_start_tick + self.command_index
                       + int(self.start_branch is StreamStartBranch.LATE_ONE))
        if self.reference_state.movement_tick_id != effect_tick:
            raise ContractViolation("reference state must identify the selected key node tick")
        _bounds(self.position_min, self.position_max, 3, "position")
        _bounds(self.velocity_min, self.velocity_max, 3, "velocity")
        for name in ("yaw_range", "pitch_range"):
            value = getattr(self, name)
            if type(value) is not tuple or len(value) != 2:
                raise ContractViolation(f"{name} requires an immutable pair")
            _bounds((value[0],), (value[1],), 1, name)

    @property
    def commitment_phase(self) -> str:
        if self.commitment_command_index is None or self.command_index < self.commitment_command_index:
            return "pre_commit"
        if self.command_index == self.commitment_command_index:
            return "commit_boundary"
        return "post_commit"


class InputStreamTracker:
    """One stream retains its first actual application and a single start offset."""

    def __init__(self, generation: StreamGeneration):
        if type(generation) is not StreamGeneration:
            raise ContractViolation("tracker requires a typed generation")
        self._generation = generation
        self._first_application: FirstApplicationReceipt | None = None
        self._start_branch: StreamStartBranch | None = None

    @property
    def generation(self) -> StreamGeneration:
        return self._generation

    @classmethod
    def start(cls, generation: StreamGeneration) -> InputStreamTracker:
        return cls(generation)

    def record_first_application(self, receipt: FirstApplicationReceipt) -> StreamStartBranch:
        if type(receipt) is not FirstApplicationReceipt:
            raise ContractViolation("first application requires a typed receipt")
        if self._first_application is not None:
            raise ContractViolation("first application is already recorded")
        if receipt.control_sequence != self.generation.first_control_sequence:
            raise ContractViolation("receipt is not the first command of this generation")
        if receipt.command != self.generation.commands[0]:
            raise ContractViolation("first applied command differs from the generation")
        offset = receipt.effect_tick - self.generation.requested_start_tick
        if offset not in (0, 1):
            raise ContractViolation("first application must be on time or one tick late")
        self._first_application = receipt
        self._start_branch = StreamStartBranch.ON_TIME if offset == 0 else StreamStartBranch.LATE_ONE
        return self._start_branch

    def expected_effect_tick(self, command_index: int) -> int:
        require_nonnegative_int(command_index, "command index")
        if command_index >= len(self.generation.commands):
            raise ContractViolation("command index is outside this generation")
        if self._first_application is None:
            raise ContractViolation("first application has not selected the stream branch")
        return self._first_application.effect_tick + command_index

    def compare_key_node(self, observed: PhysicsState, envelope: KeyNodeEnvelope, *,
                         generation_id: str, dependency_identity: tuple) -> KeyNodeCheck:
        """Compare existing evidence; reproof is a result, never a hidden call."""
        if type(observed) is not PhysicsState or type(envelope) is not KeyNodeEnvelope:
            raise ContractViolation("key-node comparison requires typed state and envelope")
        require_identifier(generation_id, "generation id")
        _identity(dependency_identity, "dependency identity")
        compared: list[str] = []

        def result(kind: KeyNodeCheckKind, reason: str) -> KeyNodeCheck:
            return KeyNodeCheck(kind, reason, 0, tuple(compared))

        compared.append("generation_id")
        if generation_id != self.generation.generation_id:
            return result(KeyNodeCheckKind.REJECTED, "stale_generation")
        compared.append("generation")
        if envelope.generation != self.generation:
            return result(KeyNodeCheckKind.REPROOF_REQUIRED, "generation_binding_changed")
        compared.append("dependency_identity")
        if dependency_identity != self.generation.dependency_identity:
            return result(KeyNodeCheckKind.REPROOF_REQUIRED, "dependency_changed")
        compared.append("start_branch")
        if self._start_branch is None:
            return result(KeyNodeCheckKind.REJECTED, "first_application_missing")
        if envelope.start_branch is not self._start_branch:
            return result(KeyNodeCheckKind.REPROOF_REQUIRED, "start_branch_changed")
        compared.append("movement_tick_id")
        if observed.movement_tick_id != self.expected_effect_tick(envelope.command_index):
            return result(KeyNodeCheckKind.REPROOF_REQUIRED, "different_key_node_tick")
        # The tick identifies the node; its independent body ranges still decide
        # whether the old evidence applies. No clamping or extra tolerance.
        compared.append("commitment_phase")
        ranges = {
            "position": (envelope.position_min, envelope.position_max),
            "velocity_blocks_per_tick": (envelope.velocity_min, envelope.velocity_max),
            "yaw_radians": ((envelope.yaw_range[0],), (envelope.yaw_range[1],)),
            "pitch_radians": ((envelope.pitch_range[0],), (envelope.pitch_range[1],)),
        }
        for field in fields(PhysicsState):
            name = field.name
            if name == "movement_tick_id":
                continue
            compared.append(name)
            actual = getattr(observed, name)
            if name in ranges:
                lower, upper = ranges[name]
                values = actual if type(actual) is tuple else (actual,)
                matches = all(lo <= value <= hi for lo, value, hi in zip(lower, values, upper))
            else:
                matches = actual == getattr(envelope.reference_state, name)
            if not matches:
                return result(KeyNodeCheckKind.REPROOF_REQUIRED, f"outside_envelope:{name}")
        return result(KeyNodeCheckKind.COMPARE_ONLY, "matching_key_node")
