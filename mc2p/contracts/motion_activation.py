"""Immutable facts at the Runtime's final movement arbitration boundary.

These contracts deliberately do not import motion_nav. Constructing a value
does not register a proof, acquire body responsibility, or send a command.
"""
from __future__ import annotations

from collections.abc import MutableMapping, MutableSequence, MutableSet
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum, StrEnum
import math
import sys
from types import MemberDescriptorType, UnionType
from typing import Protocol, get_args, get_origin, get_type_hints, runtime_checkable

from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementTickWindowV1, MovementV1
from mc2p.contracts.common import (
    ContractViolation, require_finite, require_identifier, require_nonnegative_int,
)


def require_immutable_graph(value: object) -> None:
    """Reject mutable containers even when hidden inside a frozen dataclass.

    MappingProxyType is rejected too: its backing mapping can still change.
    Arbitrary owner/port objects are not serializable preparation data.
    """
    active: set[int] = set()
    checked: set[int] = set()

    def visit(node: object) -> None:
        if node is None or type(node) in (str, bytes, bool, int):
            return
        if type(node) is float:
            if not math.isfinite(node):
                raise ContractViolation("immutable graph contains a nonfinite number")
            return
        if isinstance(node, (MutableSequence, MutableMapping, MutableSet, list, dict, set, bytearray)):
            raise ContractViolation("immutable graph cannot contain mutable container storage or subclasses")
        if isinstance(node, Enum):
            kind = type(node)
            module = sys.modules.get(kind.__module__)
            if (not kind.__module__.startswith("mc2p.") or module is None
                    or vars(module).get(kind.__name__) is not kind):
                raise ContractViolation("immutable graph accepts only project-defined enum members")
            # Standard Enum members have these interpreter-owned entries. A
            # project enum is a trusted constant, not an arbitrary data carrier.
            storage = vars(node)
            if (set(storage) != {"_value_", "_name_", "__objclass__", "_sort_order_"}
                    or storage["__objclass__"] is not kind
                    or type(storage["_name_"]) is not str
                    or type(storage["_sort_order_"]) is not int
                    or storage["_sort_order_"] < 0
                    or kind.__members__.get(storage["_name_"]) is not node):
                raise ContractViolation("project enum contains nonstandard member storage")
            if any(base.__dict__.get("__slots__", ())
                   or any(type(item) is MemberDescriptorType for item in vars(base).values())
                   for base in kind.__mro__):
                raise ContractViolation("project enum cannot contain additional instance slots")
            if type(node.value) not in (str, bytes, bool, int, float, type(None)):
                raise ContractViolation("project enum value must be an ordinary immutable scalar")
            visit(node.value)
            return
        identity = id(node)
        if identity in active:
            raise ContractViolation("immutable graph contains a cycle")
        if identity in checked:
            return
        active.add(identity)
        if type(node) in (tuple, frozenset):
            for item in node:
                visit(item)
        elif is_dataclass(node) and not isinstance(node, type):
            if not node.__dataclass_params__.frozen:
                raise ContractViolation("immutable graph contains an unfrozen dataclass")
            if hasattr(node, "__dict__"):
                raise ContractViolation("immutable graph dataclasses cannot have a writable instance dictionary")
            # The preparation schema declares no opaque scalar/value bases.
            # Only object and explicitly frozen dataclass layers are allowed;
            # C-level container storage is not visible as Python slot fields.
            for base in type(node).__mro__:
                if base is object:
                    continue
                parameters = vars(base).get("__dataclass_params__")
                if parameters is None or not parameters.frozen:
                    raise ContractViolation("immutable graph dataclass has an undeclared or unfrozen storage base")
            declared = {field.name for field in fields(node)}
            for base in type(node).__mro__:
                for name, descriptor in vars(base).items():
                    if type(descriptor) is not MemberDescriptorType:
                        continue
                    # Inspect actual descriptors, including mangled private
                    # slots and a base slot shadowed by a subclass field.
                    if name not in declared:
                        raise ContractViolation("immutable graph dataclass contains a nonfield instance slot")
                    try:
                        stored = descriptor.__get__(node, type(node))
                    except AttributeError:
                        continue  # An unset shadowed base slot holds no object.
                    visit(stored)
            for field in fields(node):
                visit(getattr(node, field.name))
        else:
            raise ContractViolation("immutable graph contains a mutable or opaque object")
        active.remove(identity)
        checked.add(identity)

    visit(value)


def _matches(value: object, annotation: object) -> bool:
    if annotation is object:
        return True
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is UnionType:
        return any(_matches(value, item) for item in args)
    if origin is tuple:
        if type(value) is not tuple:
            return False
        if len(args) == 2 and args[1] is Ellipsis:
            return all(_matches(item, args[0]) for item in value)
        return len(value) == len(args) and all(_matches(item, kind) for item, kind in zip(value, args))
    if origin is frozenset:
        return (type(value) is frozenset and len(args) == 1
                and all(_matches(item, args[0]) for item in value))
    if annotation is float:
        return type(value) in (int, float)
    return type(value) is annotation


def validate_frozen_value(value: object) -> None:
    """Shared constructor validation, never a hot-path query operation."""
    require_immutable_graph(value)
    for name, kind in get_type_hints(type(value)).items():
        if not _matches(getattr(value, name), kind):
            raise ContractViolation(f"{name} must match its declared contract type")


def _freeze_native_fields(kind, values):
    """Return only a checked, fixed-schema native activation value."""
    if not any(kind is native for native in (ActivationResult, StreamActivationProposal, PreparedActivationTransaction,
            StreamActivationBinding, PreparedActivationAcceptance, PreparedActivationDelivery,
            FirstStreamApplication, InputStreamCommandFact, StreamApplicationResult)):
        raise ContractViolation("trusted construction supports only bounded activation facts")
    if type(values) is not dict or len(values) != len(fields(kind)) or set(values) != {field.name for field in fields(kind)}:
        raise ContractViolation("trusted activation fields are incomplete")
    result = object.__new__(kind)
    for name, value in values.items():
        object.__setattr__(result, name, value)
    result._validate_native_fields()
    return result


def _native_identifier(value, name: str) -> None:
    if type(value) is not str or len(value) > 128:
        raise ContractViolation("trusted activation identifiers must be bounded native strings")
    require_identifier(value, name)


def _native_enum_reference(value, kind) -> None:
    if type(value) is not kind:
        raise ContractViolation("trusted activation requires a native enum reference")
    storage = vars(value)
    if (len(storage) != 4 or set(storage) != {"_value_", "_name_", "__objclass__", "_sort_order_"}
            or type(storage["_value_"]) is not str or type(storage["_name_"]) is not str
            or storage["__objclass__"] is not kind or type(storage["_sort_order_"]) is not int
            or kind.__members__.get(storage["_name_"]) is not value):
        raise ContractViolation("trusted enum cannot contain mutable or foreign member storage")


def _native_action_reference(action: ActionSnapshotV1) -> None:
    """Check the fixed native layout without a recursive container walk."""
    if type(action) is not ActionSnapshotV1:
        raise ContractViolation("trusted activation requires a native action reference")
    # Native constructors validate the scalar controls and operation schema.
    # Reject scalar subclasses as well; those may have mutable instance storage.
    action.__post_init__()
    for value in (action.episode_id, action.schema_version):
        if type(value) is not str:
            raise ContractViolation("trusted action identifiers must be ordinary strings")
    action.movement.__post_init__()
    action.look.__post_init__()
    for value in (action.look.yaw_delta_degrees, action.look.pitch_delta_degrees):
        if type(value) not in (int, float):
            raise ContractViolation("trusted action look must use native numbers")
    if action.operation is not None:
        # All seven native operations have a fixed, scalar-only field layout.
        for field in fields(action.operation):
            value = getattr(action.operation, field.name)
            if type(value) not in (str, int, float, bool) or type(value) is float and not math.isfinite(value):
                raise ContractViolation("trusted action operation must use native scalar fields")


def _native_binding_reference(binding) -> None:
    if type(binding) is not StreamActivationBinding or type(binding.requested_window) is not MovementTickWindowV1:
        raise ContractViolation("trusted transaction requires a native binding and window")
    for name in ("proof_token", "generation_id", "owner_id", "ledger_id", "world_session_id"):
        _native_identifier(getattr(binding, name), name)
    binding.requested_window.__post_init__()
    _native_action_reference(binding.compound_action)
    require_nonnegative_int(binding.first_control_sequence, "actual first sequence")
    require_nonnegative_int(binding.submitted_movement_tick, "submitted movement tick")
    if (binding.first_control_sequence != binding.compound_action.request_sequence_id
            or binding.submitted_movement_tick > binding.requested_window.latest_tick
            or binding.compound_action.cancel_request_sequence_id is not None):
        raise ContractViolation("trusted activation binding identity is inconsistent")


class ActivationStatus(StrEnum):
    ACTIVATED = "activated"
    NOT_SELECTED = "not_selected"
    ACTIVATION_MISMATCH = "activation_mismatch"
    STALE_GENERATION = "stale_generation"


class ActivationRejectReason(StrEnum):
    GENERATION = "generation"
    OWNER = "owner"
    WINDOW = "window"
    MOVEMENT = "movement"
    ALREADY_BOUND = "already_bound"
    TRANSACTION = "transaction"
    PROOF = "proof"
    LEDGER = "ledger"
    WORLD_SESSION = "world_session"
    HANDOFF = "handoff"


@dataclass(frozen=True, slots=True)
class ActivationResult:
    status: ActivationStatus
    reason: ActivationRejectReason | None = None

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        self._validate_fields()

    def _validate_fields(self) -> None:
        if type(self.status) is not ActivationStatus or self.reason is not None and type(self.reason) is not ActivationRejectReason:
            raise ContractViolation("activation result requires native typed status and reason")
        if (self.status in (ActivationStatus.ACTIVATION_MISMATCH, ActivationStatus.STALE_GENERATION)) != (self.reason is not None):
            raise ContractViolation("activation rejection requires a typed reason")

    @classmethod
    def _from_native_references(cls, status: ActivationStatus, reason: ActivationRejectReason | None = None):
        return _freeze_native_fields(cls, dict(status=status, reason=reason))

    def _validate_native_fields(self) -> None:
        self._validate_fields()
        _native_enum_reference(self.status, ActivationStatus)
        if self.reason is not None:
            _native_enum_reference(self.reason, ActivationRejectReason)


@dataclass(frozen=True, slots=True)
class StreamActivationBinding:
    proof_token: str
    generation_id: str
    owner_id: str
    ledger_id: str
    world_session_id: str
    first_control_sequence: int
    requested_window: MovementTickWindowV1
    submitted_movement_tick: int
    compound_action: ActionSnapshotV1

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        for name in ("proof_token", "generation_id", "owner_id", "ledger_id", "world_session_id"):
            require_identifier(getattr(self, name), name)
        require_nonnegative_int(self.first_control_sequence, "actual first sequence")
        require_nonnegative_int(self.submitted_movement_tick, "submitted movement tick")
        if self.first_control_sequence != self.compound_action.request_sequence_id:
            raise ContractViolation("activation sequence must be the final compound command sequence")
        if self.submitted_movement_tick > self.requested_window.latest_tick:
            raise ContractViolation("activation submission is already outside its window")
        if self.compound_action.cancel_request_sequence_id is not None:
            raise ContractViolation("a cancellation command cannot activate a movement stream")

    @classmethod
    def _from_native_references(cls, **values):
        return _freeze_native_fields(cls, values)

    def _validate_native_fields(self):
        _native_binding_reference(self)


@dataclass(frozen=True, slots=True)
class StreamActivationProposal:
    proof_token: str
    generation_id: str
    owner_id: str
    ledger_id: str
    world_session_id: str
    movement_intent_id: str
    expected_movement: MovementV1
    expected_yaw_radians: float
    requested_window: MovementTickWindowV1
    handoff_token: str

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        self._validate_fields()

    def _validate_fields(self) -> None:
        if type(self.expected_movement) is not MovementV1 or type(self.requested_window) is not MovementTickWindowV1:
            raise ContractViolation("trusted proposal requires native movement and window references")
        for name in ("proof_token", "generation_id", "owner_id", "ledger_id", "world_session_id",
                     "movement_intent_id", "handoff_token"):
            if type(getattr(self, name)) is not str:
                raise ContractViolation("proposal identifiers must be ordinary strings")
            require_identifier(getattr(self, name), name)
        if type(self.expected_yaw_radians) not in (int, float):
            raise ContractViolation("proposal yaw must be a native number")
        require_finite(self.expected_yaw_radians, "expected movement yaw")

    @classmethod
    def _from_native_references(cls, **values):
        """Reuse the movement checked by receiver publication and native window."""
        return _freeze_native_fields(cls, values)

    def _validate_native_fields(self) -> None:
        self._validate_fields()
        self.expected_movement.__post_init__()
        self.requested_window.__post_init__()
        for name in ("proof_token", "generation_id", "owner_id", "ledger_id", "world_session_id",
                     "movement_intent_id", "handoff_token"):
            _native_identifier(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class FirstStreamApplication:
    generation_id: str
    control_sequence: int
    effect_tick: int
    movement: MovementV1
    movement_yaw_radians: float

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        require_identifier(self.generation_id, "generation")
        require_nonnegative_int(self.control_sequence, "actual sequence")
        require_nonnegative_int(self.effect_tick, "actual effect tick")
        require_finite(self.movement_yaw_radians, "actual movement yaw")

    @classmethod
    def _from_native_references(cls, **values):
        return _freeze_native_fields(cls, values)

    def _validate_native_fields(self):
        _native_identifier(self.generation_id, "generation")
        require_nonnegative_int(self.control_sequence, "actual sequence")
        require_nonnegative_int(self.effect_tick, "actual effect tick")
        if type(self.movement) is not MovementV1 or type(self.movement_yaw_radians) not in (int, float):
            raise ContractViolation("actual application requires native controls and yaw")
        self.movement.__post_init__()
        require_finite(self.movement_yaw_radians, "actual movement yaw")


class StreamStartBranch(StrEnum):
    ON_TIME = "on_time"
    LATE_ONE = "late_one"


class StreamApplicationStatus(StrEnum):
    APPLIED = "applied"
    REJECTED = "rejected"
    DEVIATION = "deviation"


class StreamApplicationReason(StrEnum):
    ACTIVATION_MISSING = "activation_missing"
    GENERATION = "generation"
    SEQUENCE = "sequence"
    INPUTS = "inputs"
    TIMING = "timing"
    DUPLICATE = "duplicate"
    COMMAND_INDEX = "command_index"
    FIRST_APPLICATION_MISSING = "first_application_missing"


@dataclass(frozen=True, slots=True)
class StreamApplicationResult:
    status: StreamApplicationStatus
    start_branch: StreamStartBranch | None = None
    reason: StreamApplicationReason | None = None

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        if (self.status is not StreamApplicationStatus.APPLIED) != (self.reason is not None):
            raise ContractViolation("stream rejection requires a typed reason")
        if self.status is StreamApplicationStatus.APPLIED and self.start_branch is None:
            raise ContractViolation("applied stream must have a unique start branch")

    @classmethod
    def _from_native_references(cls, status, start_branch=None, reason=None):
        return _freeze_native_fields(cls, dict(status=status, start_branch=start_branch, reason=reason))

    def _validate_native_fields(self):
        _native_enum_reference(self.status, StreamApplicationStatus)
        if self.start_branch is not None:
            _native_enum_reference(self.start_branch, StreamStartBranch)
        if self.reason is not None:
            _native_enum_reference(self.reason, StreamApplicationReason)
        if (self.status is not StreamApplicationStatus.APPLIED) != (self.reason is not None):
            raise ContractViolation("stream rejection requires a typed reason")
        if self.status is StreamApplicationStatus.APPLIED and self.start_branch is None:
            raise ContractViolation("applied stream must have a unique start branch")


@dataclass(frozen=True, slots=True)
class InputStreamCommandFact:
    sequence: int
    generation_id: str
    command_index: int

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        require_nonnegative_int(self.sequence, "actual sequence")
        require_identifier(self.generation_id, "generation")
        require_nonnegative_int(self.command_index, "command index")

    @classmethod
    def _from_native_references(cls, sequence, generation_id, command_index):
        return _freeze_native_fields(cls, dict(sequence=sequence, generation_id=generation_id, command_index=command_index))

    def _validate_native_fields(self):
        _native_identifier(self.generation_id, "generation")
        require_nonnegative_int(self.sequence, "actual sequence")
        require_nonnegative_int(self.command_index, "command index")


@dataclass(frozen=True, slots=True)
class PreparedActivationTransaction:
    transaction_token: str
    binding: StreamActivationBinding
    old_action: ActionSnapshotV1 | None
    new_action: ActionSnapshotV1
    permission_token: str
    handoff_token: str

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        self._validate_fields()

    def _validate_fields(self) -> None:
        for name in ("transaction_token", "permission_token", "handoff_token"):
            if type(getattr(self, name)) is not str:
                raise ContractViolation("transaction identifiers must be ordinary strings")
            require_identifier(getattr(self, name), name)
        if self.new_action != self.binding.compound_action:
            raise ContractViolation("transaction must retain the final compound action")

    @classmethod
    def _from_native_references(cls, **values):
        """Reuse an already validated binding and this process's native actions."""
        return _freeze_native_fields(cls, values)

    def _validate_native_fields(self) -> None:
        _native_binding_reference(self.binding)
        _native_action_reference(self.new_action)
        if self.old_action is not None:
            _native_action_reference(self.old_action)
        if self.new_action is not self.binding.compound_action:
            raise ContractViolation("trusted transaction must retain the exact final action reference")
        self._validate_fields()
        for name in ("transaction_token", "permission_token", "handoff_token"):
            _native_identifier(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class PreparedActivationAcceptance:
    transaction_token: str
    acceptance_token: str
    binding: StreamActivationBinding
    permission_token: str

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        for name in ("transaction_token", "acceptance_token", "permission_token"):
            require_identifier(getattr(self, name), name)

    @classmethod
    def _from_native_references(cls, transaction_token, acceptance_token, binding, permission_token):
        return _freeze_native_fields(cls, dict(transaction_token=transaction_token, acceptance_token=acceptance_token,
                                              binding=binding, permission_token=permission_token))

    def _validate_native_fields(self):
        _native_binding_reference(self.binding)
        for name in ("transaction_token", "acceptance_token", "permission_token"):
            _native_identifier(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class PreparedActivationDelivery:
    transaction_token: str
    acceptance_token: str
    binding: StreamActivationBinding
    delivery_token: str
    delivered_action: ActionSnapshotV1

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        for name in ("transaction_token", "acceptance_token", "delivery_token"):
            require_identifier(getattr(self, name), name)
        if self.delivered_action != self.binding.compound_action:
            raise ContractViolation("delivery confirmation must name the final compound action")

    @classmethod
    def _from_native_references(cls, transaction_token, acceptance_token, binding, delivery_token, delivered_action):
        return _freeze_native_fields(cls, dict(transaction_token=transaction_token, acceptance_token=acceptance_token,
            binding=binding, delivery_token=delivery_token, delivered_action=delivered_action))

    def _validate_native_fields(self):
        _native_binding_reference(self.binding)
        _native_action_reference(self.delivered_action)
        if self.delivered_action is not self.binding.compound_action:
            raise ContractViolation("delivery must retain the actual final command reference")
        for name in ("transaction_token", "acceptance_token", "delivery_token"):
            _native_identifier(getattr(self, name), name)


@runtime_checkable
class PreparedStreamActivationPort(Protocol):
    def prepare_activation(self, binding: StreamActivationBinding) -> PreparedActivationTransaction | ActivationResult: ...
    def accept_activation(self, transaction: PreparedActivationTransaction) -> PreparedActivationAcceptance | ActivationResult: ...
    def deliver_activation(self, transaction: PreparedActivationTransaction,
                           accepted: PreparedActivationAcceptance) -> PreparedActivationDelivery | ActivationResult: ...
    def commit_activation(self, transaction: PreparedActivationTransaction,
                          accepted: PreparedActivationAcceptance,
                          delivered: PreparedActivationDelivery) -> ActivationResult: ...
    def record_application(self, application: FirstStreamApplication) -> StreamApplicationResult: ...


@dataclass(frozen=True, slots=True)
class IncumbentFallbackDispatch:
    """Local reference to this call's unsent incumbent input, never a proof."""
    action: ActionSnapshotV1
    window: MovementTickWindowV1
    command_index: int
    owner_id: str
    controller: object

    def __reduce_ex__(self, protocol):
        raise TypeError("incumbent fallback is a local borrowed command")


@runtime_checkable
class IncumbentFallbackPort(Protocol):
    def incumbent_fallback(self, action, observation, ledger) -> IncumbentFallbackDispatch | None: ...
    def register_incumbent_fallback(self, fallback: IncumbentFallbackDispatch, ledger) -> bool: ...


@dataclass(frozen=True, slots=True)
class PreparedStreamDispatch:
    """A local, borrowed port; deliberately never serializable preparation data."""
    proposal: StreamActivationProposal
    port: PreparedStreamActivationPort

    def __post_init__(self) -> None:
        if type(self.proposal) is not StreamActivationProposal or not isinstance(self.port, PreparedStreamActivationPort):
            raise ContractViolation("dispatch requires a typed proposal and local activation port")
        require_immutable_graph(self.proposal)

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("PreparedStreamDispatch is a local borrowed port and cannot be serialized")
