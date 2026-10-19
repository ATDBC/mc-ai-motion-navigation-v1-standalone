"""D102 preparation values; no worker, registry, or execution authority.

Future commands are identified only by stream generation and command index.
The actual sequence lives exclusively in activation/application facts.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum
import math
from typing import TYPE_CHECKING, Protocol, TypeAlias, runtime_checkable

from mc2p.contracts.common import ContractViolation, require_finite, require_identifier, require_nonnegative_int
from mc2p.contracts.motion_activation import (
    FirstStreamApplication, StreamStartBranch, validate_frozen_value,
    ActivationResult, PreparedActivationTransaction, PreparedActivationAcceptance, PreparedActivationDelivery,
)
from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementV1
from mc2p.motion_nav.async_work import AsyncComputationScope, AsyncWorkIdentity, AsyncWorkKind
from mc2p.motion_nav.body_control import HandoffEvidence
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.motion_chain import (
    FallbackCandidate, FallbackTerminalMode, ProvenFallbackSelection, SuccessorPreparationReference,
    preserve_current_neutral_fallback, validate_fallback_candidates,
)
from mc2p.motion_nav.motion_solver import MotionCommandTick
from mc2p.motion_nav.online_motion import (
    CandidateExecutionWindow, InputApplicationRecord, project_movement_command, ProjectionStatus,
)
from mc2p.motion_nav.physics_types import PhysicsRuleset, PhysicsState, TickInput
from mc2p.motion_nav.world_model import (
    Aabb, BlockPos, CellFact, ImmutableWorldSection, SectionPos, SnapshotSourceProvenance, block_section,
)

if TYPE_CHECKING:
    from mc2p.motion_nav.world_snapshot import SealedSnapshot


DependencyFacts: TypeAlias = tuple[tuple[BlockPos, CellFact], ...]
DependencyVersions: TypeAlias = tuple[tuple[BlockPos, int], ...]
SectionReferences: TypeAlias = tuple[ImmutableWorldSection, ...]
MAXIMUM_PREPARED_ID_UTF8_BYTES = 128


def _digest(value: str, name: str) -> None:
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ContractViolation(f"{name} must be a sealed SHA-256 digest")


def _ids(value: object, *names: str) -> None:
    for name in names:
        identifier = getattr(value, name)
        if type(identifier) is not str or not identifier:
            raise ContractViolation(f"{name} must be a non-empty identifier")
        try:
            encoded = identifier.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ContractViolation(f"{name} must have a valid UTF-8 encoding") from error
        if len(encoded) > MAXIMUM_PREPARED_ID_UTF8_BYTES:
            raise ContractViolation(f"{name} exceeds 128 UTF-8 bytes")
        require_identifier(identifier, name)


def _nonnegative(value: object, *names: str) -> None:
    for name in names:
        require_nonnegative_int(getattr(value, name), name)


def _amount(value: float, name: str) -> None:
    require_finite(value, name)
    if value < 0:
        raise ContractViolation(f"{name} must be nonnegative")


def _dependencies(facts: DependencyFacts, versions: DependencyVersions, *, exact: bool = True) -> None:
    positions = tuple(pos for pos, _ in facts)
    version_positions = tuple(pos for pos, _ in versions)
    if positions != tuple(sorted(set(positions))) or version_positions != tuple(sorted(set(version_positions))):
        raise ContractViolation("dependency positions must be ordered and unique")
    if exact and positions != version_positions:
        raise ContractViolation("dependency facts and versions must cover the same cells")
    for _, version in versions:
        require_nonnegative_int(version, "semantic cell version")


def _state_rules(state: PhysicsState, ruleset: PhysicsRuleset, session: str) -> None:
    if (state.ruleset_id != ruleset.ruleset_id or state.state_schema != ruleset.state_schema
            or state.session.value != session):
        raise ContractViolation("physics state must match the request rules and world session")


def _require_dependency_subset(facts: DependencyFacts, versions: DependencyVersions,
                               parent_facts: DependencyFacts, parent_versions: DependencyVersions) -> None:
    fact_index, version_index = dict(parent_facts), dict(parent_versions)
    if any(fact_index.get(pos) != fact for pos, fact in facts):
        raise ContractViolation("proof dependency summary omits or contradicts a branch/tail fact")
    if any(version_index.get(pos) != version for pos, version in versions):
        raise ContractViolation("proof dependency summary omits or contradicts a branch/tail version")


def _dependency_world(facts: DependencyFacts, session: str) -> None:
    if any(fact.stamp is not None and fact.stamp.session.value != session for _, fact in facts):
        raise ContractViolation("proof dependency fact belongs to a different request world")


def _proof_snapshot(proof: PreparedMotionProof, identity: SnapshotIdentity) -> None:
    """Compare declared scope only; do not scan/replay or access a live world."""
    _snapshot_facts(identity.world_session_id, identity.bounds, proof.dependency_facts, proof.dependency_versions)
    for branch in proof.branches:
        _snapshot_facts(identity.world_session_id, identity.bounds, branch.dependency_facts, branch.dependency_versions)
        for state in branch.trajectory:
            _state_rules(state, identity.ruleset, identity.world_session_id)
        for tail in branch.safety_tails:
            _snapshot_facts(identity.world_session_id, identity.bounds, tail.dependency_facts, tail.dependency_versions)
            for state in tail.trajectory:
                _state_rules(state, identity.ruleset, identity.world_session_id)
            if tail.fallback.release_witness_state is not None:
                _state_rules(tail.fallback.release_witness_state,
                             identity.ruleset, identity.world_session_id)


def _tail_damage_budget(proof: PreparedMotionProof, budget: TaskDamageBudget, remaining: float) -> None:
    for branch in proof.branches:
        for tail in branch.safety_tails:
            if tail.expected_damage_points > remaining or tail.expected_damage_points > budget.maximum_expected_damage_points:
                raise ContractViolation("prepared safety tail damage exceeds the request balance or task authorization")


def _canonical_key_nodes(proof: PreparedMotionProof) -> tuple[PhysicsState, ...]:
    return tuple(branch.trajectory[index] for branch in proof.branches
                 for index in sorted({0, branch.last_abandonable_index,
                                      branch.commitment_index, len(branch.inputs)}))


def _canonical_tails(proof: PreparedMotionProof) -> tuple[PreparedSafetyTail, ...]:
    return tuple(tail for branch in proof.branches for tail in branch.safety_tails)


@dataclass(frozen=True, slots=True)
class StreamGeneration:
    generation_id: str
    owner_id: str
    request_id: str
    route_id: str
    action_index: int
    expected_inputs: tuple[TickInput, ...]
    entry_identity: tuple[object, ...]
    requested_window: CandidateExecutionWindow
    suffix_identity: tuple[object, ...]
    late_start_hold_input: TickInput

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        _ids(self, "generation_id", "owner_id", "request_id", "route_id")
        _nonnegative(self, "action_index")
        if not self.expected_inputs or not self.entry_identity or not self.suffix_identity:
            raise ContractViolation("stream must bind inputs, exact entry and safety suffix")
        if len(self.entry_identity) != 1 or type(self.entry_identity[0]) is not PhysicsState:
            raise ContractViolation("stream exact entry must be an immutable PhysicsState reference")
        if self.requested_window.latest_start_tick - self.requested_window.earliest_start_tick > 1:
            raise ContractViolation("stream start window cannot exceed the unique 0/1 start branches")

    @classmethod
    def from_selected_fields(cls, **values) -> StreamGeneration:
        """Freeze bounded, already-typed owner fields without walking a graph.

        The parent request constructor repeats the complete graph check on the
        receiver before submission. This factory grants no registry authority.
        """
        if set(values) != {field.name for field in fields(cls)}:
            raise ContractViolation("stream selection fields are incomplete")
        result = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(result, name, value)
        _ids(result, "generation_id", "owner_id", "request_id", "route_id")
        _nonnegative(result, "action_index")
        if (type(result.expected_inputs) is not tuple or not 1 <= len(result.expected_inputs) <= 40
                or any(type(item) is not TickInput for item in result.expected_inputs)
                or type(result.entry_identity) is not tuple or len(result.entry_identity) != 1
                or type(result.entry_identity[0]) is not PhysicsState
                or type(result.requested_window) is not CandidateExecutionWindow
                or type(result.suffix_identity) is not tuple or not result.suffix_identity
                or type(result.late_start_hold_input) is not TickInput):
            raise ContractViolation("selected stream requires bounded native immutable fields")
        return result


@dataclass(frozen=True, slots=True)
class WorldSnapshotVersion:
    world_session_id: str
    geometry_revision: int
    evidence_revision: int
    local_revision: int
    bounds: Aabb
    ruleset: PhysicsRuleset
    section_references: SectionReferences
    source_provenance: SnapshotSourceProvenance | None = None

    def __post_init__(self) -> None:
        _ids(self, "world_session_id")
        _nonnegative(self, "geometry_revision", "evidence_revision", "local_revision")
        if self.source_provenance is not None and type(self.source_provenance) is not SnapshotSourceProvenance:
            raise ContractViolation("borrowed snapshot requires typed source provenance")
        if type(self.bounds) is not Aabb or type(self.ruleset) is not PhysicsRuleset:
            raise ContractViolation("borrowed snapshot requires immutable bounds and ruleset")
        if (type(self.section_references) is not tuple
                or any(type(section) is not ImmutableWorldSection for section in self.section_references)):
            raise ContractViolation("borrowed snapshot requires immutable section references")
        # Each section was checked when observation committed it. Borrowing must
        # not walk or flatten its fact graph again.
        sections = tuple(section.position for section in self.section_references)
        if sections != tuple(sorted(set(sections))):
            raise ContractViolation("snapshot section references must be ordered and unique")


def _snapshot_facts(session: str, bounds: Aabb, facts: DependencyFacts, versions: DependencyVersions) -> None:
    _dependencies(facts, versions)
    for (x, y, z), fact in facts:
        if fact.stamp is not None and fact.stamp.session.value != session:
            raise ContractViolation("snapshot facts belong to a different world session")
        if not (bounds.min_x <= x < bounds.max_x and bounds.min_y <= y < bounds.max_y
                and bounds.min_z <= z < bounds.max_z):
            raise ContractViolation("snapshot fact is outside its declared bounds")


@dataclass(frozen=True, slots=True)
class SnapshotIdentity:
    world_session_id: str
    geometry_revision: int
    evidence_revision: int
    local_revision: int
    digest: str
    ruleset: PhysicsRuleset
    bounds: Aabb

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        _ids(self, "world_session_id")
        _nonnegative(self, "geometry_revision", "evidence_revision", "local_revision")
        _digest(self.digest, "snapshot digest")


@dataclass(frozen=True, slots=True)
class SnapshotLease:
    lease_token: str
    # A local import checks the exact sealed type without a module cycle.
    snapshot: object

    def __post_init__(self) -> None:
        from mc2p.motion_nav.world_snapshot import SealedSnapshot
        _ids(self, "lease_token")
        if type(self.snapshot) is not SealedSnapshot:
            raise ContractViolation("snapshot lease requires an already sealed snapshot")

    @property
    def identity(self) -> SnapshotIdentity:
        return self.snapshot.identity

    @property
    def facts(self) -> DependencyFacts:
        return self.snapshot.facts

    @property
    def dependency_versions(self) -> DependencyVersions:
        return self.snapshot.dependency_versions

    @property
    def canonical_bytes(self) -> bytes:
        return self.snapshot.canonical_bytes

    def sealed_snapshot(self) -> SealedSnapshot:
        return self.snapshot

    def cell(self, position: BlockPos) -> CellFact:
        return self.snapshot.cell(position)


class SnapshotBorrowStatus(StrEnum):
    READY = "ready"
    BUSY = "busy"
    CAPACITY = "capacity"


@dataclass(frozen=True, slots=True)
class SnapshotBorrowResult:
    status: SnapshotBorrowStatus
    version: WorldSnapshotVersion | None = None

    def __post_init__(self) -> None:
        if (type(self.status) is not SnapshotBorrowStatus
                or self.version is not None and type(self.version) is not WorldSnapshotVersion):
            raise ContractViolation("snapshot borrow requires a typed status and version")
        if (self.status is SnapshotBorrowStatus.READY) != (self.version is not None):
            raise ContractViolation("only a ready snapshot borrow carries a version")


class SnapshotPublishStatus(StrEnum):
    READY = "ready"
    CAPACITY = "capacity"
    INVALID = "invalid"


class SnapshotLeaseStatus(StrEnum):
    READY = "ready"
    NOT_READY = "not_ready"
    CAPACITY = "capacity"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class SnapshotLeaseResult:
    status: SnapshotLeaseStatus
    lease: SnapshotLease | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not SnapshotLeaseStatus or (
                self.lease is not None and type(self.lease) is not SnapshotLease):
            raise ContractViolation("snapshot lease result must use typed status and lease")
        if (self.status is SnapshotLeaseStatus.READY) != (self.lease is not None):
            raise ContractViolation("only a ready snapshot result carries a sealed lease")


@dataclass(frozen=True, slots=True)
class PreparedScanBudget:
    maximum_nodes: int
    maximum_physics_steps: int
    maximum_candidates: int

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        _nonnegative(self, "maximum_nodes", "maximum_physics_steps", "maximum_candidates")
        if min(self.maximum_nodes, self.maximum_physics_steps, self.maximum_candidates) < 1:
            raise ContractViolation("preparation scan budgets must be positive")


@dataclass(frozen=True, slots=True)
class PreparedActionPreparation:
    """Parent-owned raw fields awaiting the same receiver's source lease.

    Keep existing immutable references here. Digest, complete graph checking
    and PreparedMotionRequest construction happen only on the receiver.
    """
    job_token: str
    async_identity: AsyncWorkIdentity
    stream: StreamGeneration
    route_revision: int
    action_revision: int
    goal_revision: int
    commands: tuple[MotionCommandTick, ...]
    exit_state: PhysicsState
    ledger_id: str
    current_state: PhysicsState
    inherited_dependencies: DependencyVersions
    ruleset: PhysicsRuleset
    damage_budget: TaskDamageBudget
    damage_remaining_points: float
    scan_budget: PreparedScanBudget
    boundary_applications: tuple[FirstStreamApplication, ...] = ()
    fallback_candidates: tuple[FallbackCandidate, ...] | None = None

    def __post_init__(self) -> None:
        _ids(self, "job_token", "ledger_id")
        _nonnegative(self, "route_revision", "action_revision", "goal_revision")
        if (type(self.async_identity) is not AsyncWorkIdentity or type(self.stream) is not StreamGeneration
                or type(self.commands) is not tuple or not 1 <= len(self.commands) <= 40
                or any(type(command) is not MotionCommandTick for command in self.commands)
                or type(self.exit_state) is not PhysicsState or type(self.current_state) is not PhysicsState
                or type(self.inherited_dependencies) is not tuple
                or type(self.boundary_applications) is not tuple
                or type(self.ruleset) is not PhysicsRuleset or type(self.damage_budget) is not TaskDamageBudget
                or type(self.scan_budget) is not PreparedScanBudget):
            raise ContractViolation("raw preparation requires existing immutable native references")
        if self.fallback_candidates is None:
            object.__setattr__(self, "fallback_candidates", (
                preserve_current_neutral_fallback(),
            ))
        validate_fallback_candidates(self.fallback_candidates)
        _amount(self.damage_remaining_points, "raw preparation damage balance")

    def build_request(self, lease: SnapshotLease) -> PreparedMotionRequest:
        from mc2p.motion_nav.prepared_motion_proof import canonical_motion_digest
        stream = self.stream
        return PreparedMotionRequest(self.job_token, self.async_identity, stream,
            self.route_revision, self.action_revision, self.goal_revision, self.commands,
            stream.expected_inputs, canonical_motion_digest(stream.expected_inputs), stream.entry_identity[0],
            self.exit_state, stream.requested_window, stream.requested_window.earliest_start_tick,
            self.ledger_id, self.current_state.session.value, self.boundary_applications,
            lease.identity, lease.lease_token, self.inherited_dependencies, self.ruleset,
            self.damage_budget, self.damage_remaining_points, self.scan_budget, stream.suffix_identity,
            self.fallback_candidates)


@dataclass(frozen=True, slots=True)
class PreparedMotionRequest:
    job_token: str
    async_identity: AsyncWorkIdentity
    stream: StreamGeneration
    route_revision: int
    action_revision: int
    goal_revision: int
    commands: tuple[MotionCommandTick, ...]
    expected_inputs: tuple[TickInput, ...]
    expected_input_digest: str
    entry_state: PhysicsState
    exit_state: PhysicsState
    requested_window: CandidateExecutionWindow
    deadline_tick: int
    ledger_id: str
    world_session_id: str
    boundary_applications: tuple[FirstStreamApplication, ...]
    snapshot_identity: SnapshotIdentity
    snapshot_lease_token: str
    inherited_dependencies: DependencyVersions
    ruleset: PhysicsRuleset
    damage_budget: TaskDamageBudget
    damage_remaining_points: float
    scan_budget: PreparedScanBudget
    safety_tail_requirement: tuple[object, ...]
    fallback_candidates: tuple[FallbackCandidate, ...] | None = None

    @property
    def request_id(self) -> str:
        return self.stream.request_id

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        _ids(self, "job_token", "ledger_id", "world_session_id", "snapshot_lease_token")
        _nonnegative(self, "route_revision", "action_revision", "goal_revision", "deadline_tick")
        _digest(self.expected_input_digest, "parent expected-input digest")
        if (self.async_identity.work_kind is not AsyncWorkKind.MOTION_SOLVE
                or self.async_identity.world_session_id != self.world_session_id
                or self.async_identity.subject_id != self.request_id):
            raise ContractViolation("prepared request must retain its complete motion computation identity")
        if self.snapshot_identity.world_session_id != self.world_session_id or self.snapshot_identity.ruleset != self.ruleset:
            raise ContractViolation("request snapshot must match world and complete ruleset")
        _state_rules(self.entry_state, self.ruleset, self.world_session_id)
        _state_rules(self.exit_state, self.ruleset, self.world_session_id)
        if self.stream.entry_identity != (self.entry_state,):
            raise ContractViolation("this exact-entry request must bind its stream to the same entry state")
        if self.stream.suffix_identity != self.safety_tail_requirement:
            raise ContractViolation("request safety requirements differ from the input-stream suffix")
        if self.fallback_candidates is None:
            object.__setattr__(self, "fallback_candidates", (
                preserve_current_neutral_fallback(),
            ))
        validate_fallback_candidates(self.fallback_candidates)
        if self.expected_inputs != self.stream.expected_inputs or not self.expected_inputs:
            raise ContractViolation("parent inputs and stream inputs must be the same exact sequence")
        if self.requested_window != self.stream.requested_window:
            raise ContractViolation("request and stream start windows must agree")
        if self.deadline_tick > self.requested_window.latest_start_tick:
            raise ContractViolation("preparation deadline is after the permitted start window")
        if len(self.commands) != len(self.expected_inputs):
            raise ContractViolation("commands must cover the complete parent input sequence")
        for command, expected in zip(self.commands, self.expected_inputs):
            projection = project_movement_command(self.entry_state, command.movement,
                                                  movement_yaw_radians=command.required_movement_yaw_radians)
            if projection.status is not ProjectionStatus.READY or projection.tick_input != expected:
                raise ContractViolation("formal commands do not project to the parent expected inputs")
        _dependencies((), self.inherited_dependencies, exact=False)
        sequences = tuple(item.control_sequence for item in self.boundary_applications)
        if sequences != tuple(sorted(set(sequences))):
            raise ContractViolation("already applied boundary facts must have unique ordered actual sequences")
        if any(item.effect_tick > self.entry_state.movement_tick_id for item in self.boundary_applications):
            raise ContractViolation("boundary applications must already have occurred at the request entry")
        _amount(self.damage_remaining_points, "damage balance")
        if self.damage_remaining_points > self.damage_budget.maximum_expected_damage_points:
            raise ContractViolation("damage balance exceeds the task's authorized budget")
        if any(type(value) is SuccessorPreparationReference for value in self.safety_tail_requirement):
            from mc2p.motion_nav.prepared_motion_proof import canonical_motion_digest
            if len(self.safety_tail_requirement) != 1:
                raise ContractViolation("fixed successor requires its single compact reference")
            reference = self.safety_tail_requirement[0]
            reference.__post_init__()
            if (self.entry_state != reference.entry_state or self.exit_state != reference.exit_state
                    or self.requested_window != reference.requested_window
                    or self.ruleset != reference.ruleset
                    or self.inherited_dependencies != reference.dependency_versions
                    or self.damage_remaining_points != reference.damage_remaining_points
                    or self.scan_budget != reference.scan_budget
                    or self.expected_input_digest != reference.input_digest
                    or canonical_motion_digest(self.commands) != reference.commands_digest
                    or canonical_motion_digest(self.fallback_candidates) != reference.fallbacks_digest):
                raise ContractViolation("fixed successor request differs from its compact feasibility reference")


@dataclass(frozen=True, slots=True)
class PreparedSafetyTail:
    boundary_index: int
    start_branch: StreamStartBranch
    inputs: tuple[TickInput, ...]
    trajectory: tuple[PhysicsState, ...]
    dependency_facts: DependencyFacts
    dependency_versions: DependencyVersions
    expected_damage_points: float
    permitted_window: CandidateExecutionWindow
    fallback: ProvenFallbackSelection

    @property
    def first_effect_tick(self) -> int:
        """Empty tails keep the same next-tick comparison boundary, with no input."""
        return self.trajectory[0].movement_tick_id + 1

    @property
    def activation_deadline_tick(self) -> int:
        """The selected branch permits no additional delay inside its tail."""
        return self.first_effect_tick

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        _nonnegative(self, "boundary_index")
        if len(self.trajectory) != len(self.inputs) + 1:
            raise ContractViolation("safety tail must include its boundary state and every input result")
        _dependencies(self.dependency_facts, self.dependency_versions)
        _amount(self.expected_damage_points, "tail expected damage")
        if any(b.movement_tick_id != a.movement_tick_id + 1 for a, b in zip(self.trajectory, self.trajectory[1:])):
            raise ContractViolation("tail trajectory must contain consecutive movement ticks")
        if (not self.permitted_window.allows_start(self.first_effect_tick)
                or self.permitted_window.latest_start_tick > self.activation_deadline_tick):
            raise ContractViolation("tail window must cover its exact first effect tick without a new late branch")
        if type(self.fallback) is not ProvenFallbackSelection:
            raise ContractViolation("safety tail must retain its selected fallback")
        # Stream ownership is checked again at the containing branch/proof.
        start = self.fallback.input_start_index
        if start is not None:
            if (start >= len(self.inputs)
                    or self.fallback.first_effect_tick != self.first_effect_tick + start):
                raise ContractViolation("selected fallback is detached from the tail timing")
            for index in range(start, len(self.inputs)):
                if not self.fallback.candidate.matches_materialized(
                        self.inputs[index], self.trajectory[index]):
                    raise ContractViolation("safety tail inputs differ from the selected fallback")
        if self.fallback.candidate.terminal_mode is FallbackTerminalMode.SNEAK_EDGE_STABLE:
            terminal = self.trajectory[-1]
            witness = self.fallback.release_witness_state
            if (not terminal.on_ground or not terminal.sneaking
                    or math.hypot(terminal.velocity_blocks_per_tick[0],
                                  terminal.velocity_blocks_per_tick[2]) > 1.e-9
                    or witness.movement_tick_id != terminal.movement_tick_id + 1
                    or not witness.on_ground or witness.sneaking
                    or any(abs(actual - expected) > 1.e-9
                           for actual, expected in zip(witness.position, terminal.position))
                    or math.hypot(witness.velocity_blocks_per_tick[0],
                                  witness.velocity_blocks_per_tick[2]) > 1.e-9):
                raise ContractViolation("sneak-edge fallback release witness is not stable")


@dataclass(frozen=True, slots=True)
class PreparedBranchProof:
    start_branch: StreamStartBranch
    first_effect_tick: int
    inputs: tuple[TickInput, ...]
    trajectory: tuple[PhysicsState, ...]
    safety_tails: tuple[PreparedSafetyTail, ...]
    commitment_index: int
    last_abandonable_index: int
    dependency_facts: DependencyFacts
    dependency_versions: DependencyVersions
    ruleset: PhysicsRuleset
    nodes_scanned: int
    physics_steps: int
    completed_candidates: int = 0
    stopping_tail_steps: int = 0

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        _nonnegative(self, "first_effect_tick", "commitment_index", "last_abandonable_index", "nodes_scanned", "physics_steps", "completed_candidates", "stopping_tail_steps")
        if self.stopping_tail_steps > self.physics_steps:
            raise ContractViolation("branch stopping work is part of its physics-step budget")
        count = len(self.inputs)
        if count == 0 or len(self.trajectory) != count + 1:
            raise ContractViolation("branch must prove every input and its exact entry state")
        if self.last_abandonable_index > self.commitment_index or self.commitment_index >= count:
            raise ContractViolation("commitment and last-abandonable indices are inconsistent")
        if tuple(state.movement_tick_id for state in self.trajectory) != tuple(range(self.first_effect_tick - 1, self.first_effect_tick + count)):
            raise ContractViolation("branch trajectory is not aligned to its first effect tick")
        if tuple(tail.boundary_index for tail in self.safety_tails) != tuple(range(count + 1)):
            raise ContractViolation("branch must retain a safety tail at every boundary")
        _dependencies(self.dependency_facts, self.dependency_versions)
        for state in self.trajectory:
            _state_rules(state, self.ruleset, self.trajectory[0].session.value)
        for tail in self.safety_tails:
            if tail.start_branch is not self.start_branch or tail.trajectory[0] != self.trajectory[tail.boundary_index]:
                raise ContractViolation("tail must originate at the same branch boundary")
            for state in tail.trajectory:
                _state_rules(state, self.ruleset, self.trajectory[0].session.value)


@dataclass(frozen=True, slots=True)
class PreparedMotionProof:
    stream: StreamGeneration
    branches: tuple[PreparedBranchProof, ...]
    ruleset: PhysicsRuleset
    dependency_facts: DependencyFacts
    dependency_versions: DependencyVersions
    nodes_scanned: int
    physics_steps: int
    completed_candidates: int = 0
    stopping_tail_steps: int = 0

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        _nonnegative(self, "nodes_scanned", "physics_steps", "completed_candidates", "stopping_tail_steps")
        if self.stopping_tail_steps > self.physics_steps:
            raise ContractViolation("proof stopping work is part of its physics-step budget")
        expected_branches = ((StreamStartBranch.ON_TIME, StreamStartBranch.LATE_ONE)
                             if self.stream.requested_window.latest_start_tick > self.stream.requested_window.earliest_start_tick
                             else (StreamStartBranch.ON_TIME,))
        if tuple(branch.start_branch for branch in self.branches) != expected_branches:
            raise ContractViolation("proof must retain exactly the authorized start branches")
        _dependencies(self.dependency_facts, self.dependency_versions)
        session = self.stream.entry_identity[0].session.value
        _dependency_world(self.dependency_facts, session)
        for branch in self.branches:
            offset = int(branch.start_branch is StreamStartBranch.LATE_ONE)
            if (branch.first_effect_tick != self.stream.requested_window.earliest_start_tick + offset
                    or branch.inputs != self.stream.expected_inputs or branch.ruleset != self.ruleset):
                raise ContractViolation("proof branch differs from its stream inputs, rules or timing")
            for state in branch.trajectory:
                _state_rules(state, self.ruleset, session)
            _dependency_world(branch.dependency_facts, session)
            _require_dependency_subset(branch.dependency_facts, branch.dependency_versions,
                                       self.dependency_facts, self.dependency_versions)
            for tail in branch.safety_tails:
                if (tail.fallback.generation_id != self.stream.generation_id
                        or tail.fallback.owner_id != self.stream.owner_id
                        or tail.fallback.route_id != self.stream.route_id
                        or tail.fallback.action_index != self.stream.action_index):
                    raise ContractViolation("fallback proof belongs to another stream owner or action")
                for state in tail.trajectory:
                    _state_rules(state, self.ruleset, session)
                if tail.fallback.release_witness_state is not None:
                    _state_rules(tail.fallback.release_witness_state,
                                 self.ruleset, session)
                _dependency_world(tail.dependency_facts, session)
                _require_dependency_subset(tail.dependency_facts, tail.dependency_versions,
                                           self.dependency_facts, self.dependency_versions)
        if (self.nodes_scanned != sum(branch.nodes_scanned for branch in self.branches)
                or self.physics_steps != sum(branch.physics_steps for branch in self.branches)
                or self.completed_candidates != sum(branch.completed_candidates for branch in self.branches)
                or self.stopping_tail_steps != sum(branch.stopping_tail_steps for branch in self.branches)):
            raise ContractViolation("proof budget counters must retain all branch work")


@dataclass(frozen=True, slots=True)
class PreparedMotionBundle:
    request: PreparedMotionRequest
    inputs: tuple[TickInput, ...]
    input_digest: str
    proof: PreparedMotionProof
    proof_digest: str
    snapshot_identity: SnapshotIdentity
    ruleset: PhysicsRuleset
    sealed_bytes: bytes

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        _digest(self.input_digest, "bundle input digest")
        _digest(self.proof_digest, "proof digest")
        if self.inputs != self.request.expected_inputs or self.input_digest != self.request.expected_input_digest:
            raise ContractViolation("bundle inputs must agree with the independent parent request")
        if self.proof.stream != self.request.stream or any(branch.inputs != self.inputs for branch in self.proof.branches):
            raise ContractViolation("proof and bundle must retain the same input stream")
        if self.ruleset != self.proof.ruleset or self.ruleset != self.request.ruleset or self.ruleset != self.snapshot_identity.ruleset:
            raise ContractViolation("proof, bundle, request and snapshot complete rulesets must agree")
        if self.snapshot_identity != self.request.snapshot_identity:
            raise ContractViolation("bundle substituted the request's source snapshot")
        _proof_snapshot(self.proof, self.snapshot_identity)
        versions = dict(self.proof.dependency_versions)
        if any(versions.get(pos) != version for pos, version in self.request.inherited_dependencies):
            raise ContractViolation("proof omits or changes a parent inherited dependency version")
        _tail_damage_budget(self.proof, self.request.damage_budget, self.request.damage_remaining_points)
        if self.proof.branches[0].trajectory[0] != self.request.entry_state:
            raise ContractViolation("on-time branch must start at the exact parent entry")
        if self.proof.branches[0].trajectory[-1] != self.request.exit_state:
            raise ContractViolation("on-time branch must finish at the exact parent exit")
        declared_fallbacks = set(self.request.fallback_candidates)
        if any(tail.fallback.candidate not in declared_fallbacks
               for branch in self.proof.branches for tail in branch.safety_tails):
            raise ContractViolation("proof selected a fallback the parent did not declare")
        if (self.proof.nodes_scanned > self.request.scan_budget.maximum_nodes
                or self.proof.physics_steps > self.request.scan_budget.maximum_physics_steps
                or self.proof.completed_candidates > self.request.scan_budget.maximum_candidates):
            raise ContractViolation("proof exceeds the parent's fixed scan budget")
        if not self.sealed_bytes:
            raise ContractViolation("prepared bundle requires sealed bytes")


@dataclass(frozen=True, slots=True)
class PreparedMotionHotView:
    stream: StreamGeneration
    entry_state: PhysicsState
    key_nodes: tuple[PhysicsState, ...]
    branches: tuple[PreparedBranchProof, ...]
    inputs: tuple[TickInput, ...]
    safety_tails: tuple[PreparedSafetyTail, ...]
    dependency_facts: DependencyFacts
    dependency_versions: DependencyVersions
    damage_budget: TaskDamageBudget
    damage_remaining_points: float
    requested_window: CandidateExecutionWindow
    deadline_tick: int
    ledger_id: str
    world_session_id: str
    ruleset: PhysicsRuleset
    snapshot_identity: SnapshotIdentity
    proof: PreparedMotionProof
    route_revision: int
    commands: tuple[MotionCommandTick, ...]

    @classmethod
    def from_bundle(cls, bundle: PreparedMotionBundle) -> PreparedMotionHotView:
        """Build reference-only views during preparation/admission, never per tick.

        This helper grants no registration authority. The sole result service
        must still match the exact registered handle object before any use.
        """
        if type(bundle) is not PreparedMotionBundle:
            raise ContractViolation("hot view requires a typed prepared bundle")
        request, proof = bundle.request, bundle.proof
        nodes = _canonical_key_nodes(proof)
        tails = _canonical_tails(proof)
        return cls(request.stream, request.entry_state, nodes, proof.branches, bundle.inputs,
                   tails, proof.dependency_facts, proof.dependency_versions, request.damage_budget,
                   request.damage_remaining_points, request.requested_window, request.deadline_tick,
                   request.ledger_id, request.world_session_id, bundle.ruleset, bundle.snapshot_identity, proof,
                   request.route_revision, request.commands)

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        _ids(self, "ledger_id", "world_session_id")
        _nonnegative(self, "deadline_tick", "route_revision")
        if len(self.commands) != len(self.inputs):
            raise ContractViolation("hot view must retain every parent command")
        for command, expected in zip(self.commands, self.inputs):
            projected = project_movement_command(self.entry_state, command.movement,
                movement_yaw_radians=command.required_movement_yaw_radians)
            if projected.tick_input != expected:
                raise ContractViolation("hot view command differs from the proved input")
        _amount(self.damage_remaining_points, "hot-view damage balance")
        _dependencies(self.dependency_facts, self.dependency_versions)
        if (self.proof.stream != self.stream or self.proof.ruleset != self.ruleset
                or self.branches is not self.proof.branches):
            raise ContractViolation("hot view must retain the same immutable proof and branch references")
        if (self.dependency_facts != self.proof.dependency_facts
                or self.dependency_versions != self.proof.dependency_versions):
            raise ContractViolation("hot view cannot omit or change the complete proof dependency summary")
        _proof_snapshot(self.proof, self.snapshot_identity)
        _tail_damage_budget(self.proof, self.damage_budget, self.damage_remaining_points)
        if (self.inputs != self.stream.expected_inputs or self.requested_window != self.stream.requested_window
                or self.stream.entry_identity != (self.entry_state,)):
            raise ContractViolation("hot view must retain the same stream entry, inputs and timing")
        if self.snapshot_identity.ruleset != self.ruleset or self.snapshot_identity.world_session_id != self.world_session_id:
            raise ContractViolation("hot view substituted world or rules")
        _state_rules(self.entry_state, self.ruleset, self.world_session_id)
        if not self.branches or any(branch.inputs != self.inputs or branch.ruleset != self.ruleset for branch in self.branches):
            raise ContractViolation("hot view must reference prepared branches for the same inputs and rules")
        expected_branches = ((StreamStartBranch.ON_TIME, StreamStartBranch.LATE_ONE)
                             if self.requested_window.latest_start_tick > self.requested_window.earliest_start_tick
                             else (StreamStartBranch.ON_TIME,))
        if tuple(branch.start_branch for branch in self.branches) != expected_branches:
            raise ContractViolation("hot view must retain exactly the stream's authorized start branches")
        for branch in self.branches:
            offset = int(branch.start_branch is StreamStartBranch.LATE_ONE)
            if branch.first_effect_tick != self.requested_window.earliest_start_tick + offset:
                raise ContractViolation("hot view branch timing differs from its stream")
        if self.branches[0].trajectory[0] != self.entry_state:
            raise ContractViolation("hot view entry must match the same prepared state")
        nodes = _canonical_key_nodes(self.proof)
        if len(nodes) != len(self.key_nodes) or any(a is not b for a, b in zip(nodes, self.key_nodes)):
            raise ContractViolation("hot view must retain every canonical key node in order and by reference")
        tails = _canonical_tails(self.proof)
        if len(tails) != len(self.safety_tails) or any(a is not b for a, b in zip(tails, self.safety_tails)):
            raise ContractViolation("hot view tails must reference the same prepared branch tails")
        if self.damage_remaining_points > self.damage_budget.maximum_expected_damage_points:
            raise ContractViolation("hot view damage balance exceeds the task's authorization")


@dataclass(frozen=True, slots=True)
class PreparedMotionHandle:
    proof_token: str
    request_id: str
    generation_id: str
    owner_id: str
    route_id: str
    action_index: int
    registered_digest: str
    dependency_versions: DependencyVersions
    hot_view: PreparedMotionHotView

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        _ids(self, "proof_token", "request_id", "generation_id", "owner_id", "route_id")
        _nonnegative(self, "action_index")
        _digest(self.registered_digest, "registered digest")
        stream = self.hot_view.stream
        if any(getattr(self, name) != getattr(stream, name) for name in
               ("request_id", "generation_id", "owner_id", "route_id", "action_index")):
            raise ContractViolation("handle identities must match its immutable hot view")
        if self.dependency_versions != self.hot_view.dependency_versions:
            raise ContractViolation("handle dependency versions differ from its hot view")


@dataclass(frozen=True, slots=True)
class PreparedCurrentFacts:
    scope: AsyncComputationScope
    stream: StreamGeneration
    body_owner_id: str
    world_session_id: str
    ruleset: PhysicsRuleset
    dependency_versions: DependencyVersions
    ledger_id: str
    snapshot_identity: SnapshotIdentity
    current_state: PhysicsState
    damage_remaining_points: float
    movement_tick: int
    permitted_window: CandidateExecutionWindow

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        _ids(self, "body_owner_id", "world_session_id", "ledger_id")
        _nonnegative(self, "movement_tick")
        _amount(self.damage_remaining_points, "current damage balance")
        _dependencies((), self.dependency_versions, exact=False)
        if self.scope.world_session_id != self.world_session_id or self.snapshot_identity.world_session_id != self.world_session_id:
            raise ContractViolation("current facts must describe one world session")
        if self.snapshot_identity.ruleset != self.ruleset:
            raise ContractViolation("current facts must describe one complete ruleset")
        _state_rules(self.current_state, self.ruleset, self.world_session_id)
        if self.current_state.movement_tick_id != self.movement_tick:
            raise ContractViolation("current body and movement tick disagree")

    def _with_activation_observation(self, state: PhysicsState, versions: DependencyVersions,
                                     damage_remaining: float) -> PreparedCurrentFacts:
        """Borrow checked native live facts without walking the prepared stream graph."""
        if type(state) is not PhysicsState or type(versions) is not tuple or type(damage_remaining) not in (int, float):
            raise ContractViolation("activation observation requires native bounded facts")
        state.__post_init__()
        _state_rules(state, self.ruleset, self.world_session_id)
        _amount(damage_remaining, "current damage balance")
        _dependencies((), versions, exact=False)
        result = object.__new__(PreparedCurrentFacts)
        for field in fields(PreparedCurrentFacts):
            value = (state if field.name == "current_state" else state.movement_tick_id if field.name == "movement_tick"
                     else versions if field.name == "dependency_versions" else damage_remaining
                     if field.name == "damage_remaining_points" else getattr(self, field.name))
            object.__setattr__(result, field.name, value)
        return result


class PreparedRejectReason(StrEnum):
    MESSAGE = "message"
    PROOF = "proof"
    MUTABLE_GRAPH = "mutable_graph"
    REQUEST = "request"
    GENERATION = "generation"
    OWNER = "owner"
    ROUTE_ACTION = "route_action"
    INPUTS = "inputs"
    LEDGER = "ledger"
    RULESET = "ruleset"
    WORLD_SNAPSHOT = "world_snapshot"
    DEPENDENCY = "dependency"


class PreparedSubmitStatus(StrEnum):
    ACCEPTED = "accepted"
    QUEUE_FULL = "queue_full"
    CLOSED = "closed"
    INPUT_UNCONFIRMED = "input_unconfirmed"


@dataclass(frozen=True, slots=True)
class PreparedSubmitResult:
    status: PreparedSubmitStatus
    job_token: str | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not PreparedSubmitStatus or self.job_token is not None and type(self.job_token) is not str:
            raise ContractViolation("submission requires a typed status and token")
        if (self.status is PreparedSubmitStatus.ACCEPTED) != (self.job_token is not None):
            raise ContractViolation("only accepted submission carries a registered job token")
        if self.job_token is not None:
            _ids(self, "job_token")


class PreparedComputeStatus(StrEnum):
    PREPARED = "prepared"
    MISSING_INFORMATION = "missing_information"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"
    WORKER_FAILED = "worker_failed"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class PreparedComputeCounts:
    nodes_scanned: int = 0
    physics_steps: int = 0
    completed_candidates: int = 0
    stopping_tail_steps: int = 0

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        _nonnegative(self, "nodes_scanned", "physics_steps", "completed_candidates", "stopping_tail_steps")
        if self.stopping_tail_steps > self.physics_steps:
            raise ContractViolation("stopping-tail work is part of the one physics-step budget")


@dataclass(frozen=True, slots=True)
class PreparedComputeResult:
    status: PreparedComputeStatus
    job_token: str
    counts: PreparedComputeCounts
    bundle: PreparedMotionBundle | None = None
    missing_cells: tuple[BlockPos, ...] = ()

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        _ids(self, "job_token")
        if self.missing_cells != tuple(sorted(set(self.missing_cells))):
            raise ContractViolation("compute missing cells must be ordered and unique")
        if self.missing_cells and self.status is not PreparedComputeStatus.MISSING_INFORMATION:
            raise ContractViolation("only missing-information computation carries missing cells")
        if (self.status is PreparedComputeStatus.PREPARED) != (self.bundle is not None):
            raise ContractViolation("only prepared computation carries a bundle")
        if self.bundle is not None and self.job_token != self.bundle.request.job_token:
            raise ContractViolation("computation result substituted its job token")
        if self.bundle is not None and (
                self.counts.nodes_scanned != self.bundle.proof.nodes_scanned
                or self.counts.physics_steps != self.bundle.proof.physics_steps
                or self.counts.completed_candidates != self.bundle.proof.completed_candidates
                or self.counts.stopping_tail_steps != self.bundle.proof.stopping_tail_steps):
            raise ContractViolation("compute counters must retain all prepared proof work")


class PreparedAdmissionStatus(StrEnum):
    REGISTERED = "registered"
    STALE = "stale"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class PreparedAdmissionResult:
    status: PreparedAdmissionStatus
    handle: PreparedMotionHandle | None = None
    reason: PreparedRejectReason | None = None

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        if (self.status is PreparedAdmissionStatus.REGISTERED) != (self.handle is not None):
            raise ContractViolation("only registered admission carries a handle")
        if (self.status is PreparedAdmissionStatus.REJECTED) != (self.reason is not None):
            raise ContractViolation("only rejected admission carries a typed rejection reason")


class PreparedQueryStatus(StrEnum):
    READY_MATCH = "ready_match"
    NOT_READY = "not_ready"
    OVERDUE = "overdue"
    ENTRY_MISMATCH = "entry_mismatch"
    DEPENDENCY_CHANGED = "dependency_changed"
    TIMING_MISMATCH = "timing_mismatch"


@dataclass(frozen=True, slots=True)
class PreparedQueryResult:
    status: PreparedQueryStatus
    handle: PreparedMotionHandle | None = None

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        if (self.status is PreparedQueryStatus.READY_MATCH) != (self.handle is not None):
            raise ContractViolation("only matching query carries an eligible registered handle")


class PreparedHandoffStatus(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class PreparedHandoffRejectReason(StrEnum):
    OWNER = "owner"
    BOUNDARY = "boundary"
    PROOF = "proof"
    ENTRY = "entry"
    TIMING = "timing"
    LEDGER = "ledger"
    COMMANDS = "commands"


@dataclass(frozen=True, slots=True)
class PreparedActionHandoff:
    """Read-only action boundary facts; no future control sequence or release."""
    handoff_token: str
    owner_id: str
    route_id: str
    route_revision: int
    old_action_index: int
    new_action_index: int
    old_proof_token: str | None
    old_generation_id: str | None
    proof_token: str | None
    generation_id: str | None
    ledger_id: str
    world_session_id: str
    observation_sequence_id: int
    entry_state: PhysicsState
    permitted_window: CandidateExecutionWindow | None
    entry_result: PreparedQueryStatus
    boundary_reached: bool
    conflicting_commands: tuple[InputApplicationRecord, ...]
    current_command: ActionSnapshotV1 | None
    expected_movement: MovementV1 | None
    expected_yaw_radians: float | None

    def __post_init__(self) -> None:
        # This graph contains only bounded facts, never a complete proof graph.
        _ids(self, "handoff_token", "owner_id", "route_id", "ledger_id", "world_session_id")
        _nonnegative(self, "route_revision", "old_action_index", "new_action_index", "observation_sequence_id")
        for name in ("old_proof_token", "old_generation_id", "proof_token", "generation_id"):
            if getattr(self, name) is not None:
                _ids(self, name)
        if self.new_action_index != self.old_action_index + 1:
            raise ContractViolation("an action handoff must name the immediate successor")
        if (type(self.entry_state) is not PhysicsState or type(self.entry_result) is not PreparedQueryStatus
                or type(self.boundary_reached) is not bool or type(self.conflicting_commands) is not tuple
                or any(type(record) is not InputApplicationRecord for record in self.conflicting_commands)
                or self.permitted_window is not None and type(self.permitted_window) is not CandidateExecutionWindow
                or self.current_command is not None and type(self.current_command) is not ActionSnapshotV1
                or self.expected_movement is not None and type(self.expected_movement) is not MovementV1):
            raise ContractViolation("action handoff requires existing native immutable facts")
        if self.expected_yaw_radians is not None:
            require_finite(self.expected_yaw_radians, "handoff expected yaw")
        if self.entry_state.session.value != self.world_session_id:
            raise ContractViolation("handoff body belongs to another world")
        if self.entry_result is PreparedQueryStatus.READY_MATCH and any(value is None for value in (
                self.proof_token, self.generation_id, self.permitted_window,
                self.expected_movement, self.expected_yaw_radians)):
            raise ContractViolation("matching action handoff requires complete prepared bindings")
        sequences = tuple(record.control_sequence for record in self.conflicting_commands)
        if sequences != tuple(sorted(set(sequences))):
            raise ContractViolation("handoff conflict facts must be unique and ordered")


@dataclass(frozen=True, slots=True)
class PreparedHandoffApproval:
    """Supervisor permission to propose; it is not body-release evidence."""
    status: PreparedHandoffStatus
    proposal: PreparedActionHandoff
    approval_token: str | None = None
    reason: PreparedHandoffRejectReason | None = None

    def __post_init__(self) -> None:
        if (type(self.status) is not PreparedHandoffStatus or type(self.proposal) is not PreparedActionHandoff
                or self.reason is not None and type(self.reason) is not PreparedHandoffRejectReason):
            raise ContractViolation("approval requires a typed handoff and decision")
        approved = self.status is PreparedHandoffStatus.APPROVED
        if approved != (self.approval_token is not None) or approved == (self.reason is not None):
            raise ContractViolation("only approved handoffs carry a permission token")
        if approved:
            _ids(self, "approval_token")
            if not self.proposal.boundary_reached or self.proposal.entry_result is not PreparedQueryStatus.READY_MATCH:
                raise ContractViolation("approval requires a proved real action boundary")


class ProofUse(StrEnum):
    CANDIDATE = "candidate"
    EXECUTING = "executing"
    SAFE_TAIL = "safe_tail"


class ReferenceStatus(StrEnum):
    RETAINED = "retained"
    RELEASED = "released"
    REJECTED = "rejected"


class ReferenceRejectReason(StrEnum):
    HANDLE = "handle"
    USE_NOT_ACTIVE = "use_not_active"
    CLOSED = "closed"
    ACTION = "action"
    TRANSACTION = "transaction"
    CONSUMER = "consumer"
    COMMAND_TERMINALS_MISSING = "command_terminals_missing"
    COMMAND_IDENTITY = "command_identity"
    COMMAND_UNRESOLVED = "command_unresolved"
    COMMAND_REVOCATION_UNCONFIRMED = "command_revocation_unconfirmed"
    ACTION_UNRESOLVED = "action_unresolved"
    SUPERVISOR_UNCONFIRMED = "supervisor_unconfirmed"


@dataclass(frozen=True, slots=True)
class ReferenceResult:
    status: ReferenceStatus
    reason: ReferenceRejectReason | None = None

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        if (self.status is ReferenceStatus.REJECTED) != (self.reason is not None):
            raise ContractViolation("only rejected reference operations carry a typed reason")


@dataclass(frozen=True, slots=True)
class ProofConsumerIdentity:
    """One action's use, independent of its computation or task outcome."""
    owner_id: str
    route_id: str
    action_index: int
    generation_id: str
    ledger_id: str
    world_session_id: str
    use: ProofUse
    proof_token: str

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        _ids(self, "owner_id", "route_id", "generation_id", "ledger_id", "world_session_id", "proof_token")
        _nonnegative(self, "action_index")

    @classmethod
    def from_handle(cls, handle: PreparedMotionHandle, use: ProofUse) -> ProofConsumerIdentity:
        if type(handle) is not PreparedMotionHandle or type(use) is not ProofUse:
            raise ContractViolation("proof consumer needs a native registered handle and use")
        result = object.__new__(cls)
        values = (handle.owner_id, handle.route_id, handle.action_index, handle.generation_id,
                  handle.hot_view.ledger_id, handle.hot_view.world_session_id, use, handle.proof_token)
        for field, value in zip(fields(cls), values):
            object.__setattr__(result, field.name, value)
        _ids(result, "owner_id", "route_id", "generation_id", "ledger_id", "world_session_id", "proof_token")
        _nonnegative(result, "action_index")
        return result


@dataclass(frozen=True, slots=True)
class ProofActionExitEvidence:
    """The action owner names the exact proof use whose body responsibility ended."""
    consumer: ProofConsumerIdentity
    exit_id: str
    handoff: HandoffEvidence

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        _ids(self, "exit_id")


@dataclass(frozen=True, slots=True)
class ProofSupervisorReleaseEvidence:
    """The supervisor independently confirms one named action exit."""
    consumer: ProofConsumerIdentity
    confirmed_exit_id: str
    handoff: HandoffEvidence

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        _ids(self, "confirmed_exit_id")


@dataclass(frozen=True, slots=True)
class ProofReleaseEvidence:
    """Borrowed facts from the input ledger, action owner and supervisor.

    Missing facts are expressible so the registry can reject a release with a
    typed reason. A task terminal is deliberately absent from this contract.
    """
    consumer: ProofConsumerIdentity | None
    command_terminals: tuple[InputApplicationRecord, ...] | None
    action_handoff: ProofActionExitEvidence | None
    supervisor_confirmation: ProofSupervisorReleaseEvidence | None

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        if self.command_terminals is not None:
            sequences = tuple(item.control_sequence for item in self.command_terminals)
            if sequences != tuple(sorted(set(sequences))):
                raise ContractViolation("release facts must retain unique ordered actual commands")


@runtime_checkable
class PreparedProofUsePort(Protocol):
    """The executor borrows only use enumeration and release from the registry."""
    @property
    def registered_handles(self) -> tuple[PreparedMotionHandle, ...]: ...

    def release_usage(self, handle: PreparedMotionHandle, *, use: ProofUse,
                      evidence: ProofReleaseEvidence) -> ReferenceResult: ...


@runtime_checkable
class PreparedHandoffQueryPort(PreparedProofUsePort, Protocol):
    """Borrow existing registry queries without creating another handle map."""
    def has_registered_handle(self, handle: PreparedMotionHandle) -> bool: ...
    def usage_active(self, handle: PreparedMotionHandle, use: ProofUse) -> bool: ...


@runtime_checkable
class PreparedActivationRegistryPort(PreparedHandoffQueryPort, Protocol):
    """The same registry's short activation and actual-command interfaces."""
    def accept_activation(self, handle: PreparedMotionHandle, transaction: PreparedActivationTransaction,
                          handoff: HandoffEvidence) -> PreparedActivationAcceptance | ActivationResult: ...
    def commit_activation(self, accepted: PreparedActivationAcceptance, delivered: PreparedActivationDelivery,
                          *, action_commit=None) -> ActivationResult: ...
    def abort_activation(self, accepted: PreparedActivationAcceptance) -> ReferenceResult: ...
    def retain_safe_tail(self, handle: PreparedMotionHandle, *, action_index: int) -> ReferenceResult: ...
    def record_command(self, handle: PreparedMotionHandle, *, command_index: int,
                       action: ActionSnapshotV1) -> ReferenceResult: ...
    def refresh_activation_observation(self, handle: PreparedMotionHandle, *, scope: AsyncComputationScope,
                                       state: PhysicsState, dependency_versions: DependencyVersions,
                                       ledger_id: str, damage_remaining: float) -> bool: ...


@dataclass(frozen=True, slots=True)
class MotionCloseReport:
    """Actual resource state after one bounded close attempt."""
    worker_exit_code: int | None = None
    live_workers: int = 0
    live_receivers: int = 0
    live_lease_returners: int = 0
    live_queue_feeders: int = 0
    expected_close_feeder_errors: int = 0
    unexpected_feeder_errors: int = 0
    unfinished_computations: int = 0
    candidate_references: int = 0
    executing_references: int = 0
    safe_tail_references: int = 0
    activation_references: int = 0
    lease_count: int = 0
    queued_returns: int = 0
    retained_bytes: int = 0
    timed_out: bool = False
    retained_responsibilities: tuple[ProofConsumerIdentity, ...] = ()

    def __post_init__(self) -> None:
        validate_frozen_value(self)
        _nonnegative(self, "live_workers", "live_receivers", "live_lease_returners", "live_queue_feeders", "expected_close_feeder_errors",
                     "unexpected_feeder_errors", "unfinished_computations",
                     "candidate_references", "executing_references", "safe_tail_references",
                     "activation_references", "lease_count", "queued_returns", "retained_bytes")
        if len(self.retained_responsibilities) != self.executing_references + self.safe_tail_references:
            raise ContractViolation("close report must name every remaining body responsibility")

    @property
    def fully_released(self) -> bool:
        return not any((self.live_workers, self.live_receivers, self.live_lease_returners, self.live_queue_feeders,
                        self.unfinished_computations, self.candidate_references, self.executing_references,
                        self.safe_tail_references, self.activation_references, self.lease_count,
                        self.queued_returns, self.retained_bytes))
