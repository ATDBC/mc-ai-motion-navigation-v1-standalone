"""Bounded immutable values for lawful task-driven perception.

These values describe needs, proposals and conditional outcomes.  They never
refresh the evidence they reference and are not actor/client wire contracts.
"""
from __future__ import annotations

from dataclasses import dataclass

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import (
    ContractViolation,
    require_finite,
    require_identifier,
    require_nonnegative_int,
)
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v2 import ClientSampleTimingV2
from mc2p.skills.motion_guard import GuardReport, MAX_SUMMARY
from mc2p.skills.navigation_evidence import EvidenceStamp


MAX_IDENTIFIER_LENGTH = 128
MAX_ACTIVE_NEEDS = 8
MAX_CANDIDATES = 16
MAX_STAGES = 3
MAX_OUTCOMES = 2
MAX_UNKNOWN_REASONS = 8
MAX_SIGNED_INT64 = 2**63 - 1
MAX_WORLD_COORDINATE = 30_000_000


@dataclass(frozen=True, slots=True)
class PerceptionConfig:
    revision: int = 5
    planning_budget_ns: int = 10_000_000
    max_needs: int = 8
    max_candidates: int = 16
    max_summary: int = 64
    target_yaw_margin_degrees: float = 10.
    target_pitch_margin_degrees: float = 8.
    progress_weight: float = 4.
    time_weight: float = 1.
    unseen_weight: float = 2.
    turn_weight: float = .25
    reversal_weight: float = .5
    stop_start_weight: float = .5
    failure_weight: float = 1.
    unknown_seconds: float = 1.
    task_gaze_weight: float = 4.

    def __post_init__(self):
        for name, ceiling in (('revision', MAX_SIGNED_INT64),
                              ('planning_budget_ns',10_000_000),
                              ('max_needs',8),('max_candidates',16),('max_summary',64)):
            value=getattr(self,name)
            require_nonnegative_int(value,name)
            if not 0 < value <= ceiling:
                raise ContractViolation(name+' exceeds hard configuration bounds')
        if self.revision != 5:
            raise ContractViolation('unsupported perception algorithm revision')
        for name in ('progress_weight','time_weight','unseen_weight','turn_weight',
                     'reversal_weight','stop_start_weight','failure_weight','unknown_seconds',
                     'target_yaw_margin_degrees','target_pitch_margin_degrees','task_gaze_weight'):
            value=getattr(self,name)
            require_finite(value,name)
            if value < 0:
                raise ContractViolation(name+' must be nonnegative')
        if self.unknown_seconds<=0:
            raise ContractViolation('unknown cost must be positive')
        if self.target_yaw_margin_degrees>=45 or self.target_pitch_margin_degrees>=30:
            raise ContractViolation('target margin must leave a nonempty field of view')

OWNERS = frozenset({
    "follow_nav", "follow_track", "search_nav", "search_attention", "work_face",
})
PURPOSES = frozenset({"route_check", "track", "search", "reacquire", "aim", "verify"})
CONDITIONS = frozenset({
    "motion_guard", "visible_track", "filtered_check", "center_block_face", "observed_block",
})
OPERATION_KINDS = frozenset({"mine_block", "interact_block"})
BLOCK_FACES = frozenset({"down", "up", "north", "south", "west", "east"})


def _identifier(value: str, name: str) -> None:
    require_identifier(value, name)
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise ContractViolation(f"{name} exceeds {MAX_IDENTIFIER_LENGTH} characters")


def _optional_identifier(value: str | None, name: str) -> None:
    if value is not None:
        _identifier(value, name)


def _int64(value: int, name: str) -> None:
    require_nonnegative_int(value, name)
    if value > MAX_SIGNED_INT64:
        raise ContractViolation(f"{name} exceeds signed 64-bit range")


def _block(value: tuple[int, int, int] | None, name: str) -> None:
    if value is None:
        return
    if (type(value) is not tuple or len(value) != 3
            or any(type(coordinate) is not int for coordinate in value)):
        raise ContractViolation(f"{name} must be an integer triple")
    if any(abs(coordinate) > MAX_WORLD_COORDINATE for coordinate in value):
        raise ContractViolation(f"{name} exceeds bounded world-grid coordinates")


def _stamp(value: EvidenceStamp, name: str) -> None:
    if type(value) is not EvidenceStamp:
        raise ContractViolation(f"{name} must be EvidenceStamp")
    for field_name in ("episode_id", "controller_clock_id", "source_backend"):
        _identifier(getattr(value, field_name), f"{name} {field_name}")
    _int64(value.sequence_id, f"{name} sequence")
    if value.request_sequence_id is not None:
        _int64(value.request_sequence_id, f"{name} request sequence")
    _int64(value.request_start_ns, f"{name} request start")
    _int64(value.received_at_ns, f"{name} received time")
    if value.received_at_ns < value.request_start_ns:
        raise ContractViolation(f"{name} received time precedes request start")
    if type(value.client_sample) is not ClientSampleTimingV2:
        raise ContractViolation(f"{name} client sample must be ClientSampleTimingV2")
    _identifier(value.client_sample.clock_id, f"{name} client clock")
    _int64(value.client_sample.started_at_monotonic_ns, f"{name} client sample start")
    _int64(value.client_sample.completed_at_monotonic_ns, f"{name} client sample completion")
    if value.client_sample.completed_at_monotonic_ns < value.client_sample.started_at_monotonic_ns:
        raise ContractViolation(f"{name} client sample completion precedes start")
    if value.controller_clock_id == value.client_sample.clock_id:
        raise ContractViolation(f"{name} controller and client clocks must differ")


def _identifier_tuple(value: tuple[str, ...], name: str, capacity: int) -> None:
    if type(value) is not tuple or len(value) > capacity:
        raise ContractViolation(f"{name} must be an immutable tuple of at most {capacity} identifiers")
    for item in value:
        _identifier(item, name)
    if len(set(value)) != len(value):
        raise ContractViolation(f"{name} must not contain duplicates")


def _optional_seconds(value: float | None, name: str) -> None:
    if value is None:
        return
    require_finite(value, name)
    if value < 0:
        raise ContractViolation(f"{name} must be nonnegative")


@dataclass(frozen=True, slots=True)
class MotionProposal:
    scope_id: str
    based_on: EvidenceStamp
    goal: Vec3V0
    floor: int | None
    yaw_degrees: float
    movement: MovementV1
    horizon_blocks: float
    estimated_duration_ns: int
    allowed_player_contact: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.scope_id, "motion proposal scope")
        _stamp(self.based_on, "motion proposal evidence")
        if type(self.goal) is not Vec3V0:
            raise ContractViolation("motion proposal goal must be Vec3V0")
        if self.floor is not None and type(self.floor) is not int:
            raise ContractViolation("motion proposal floor must be an integer or null")
        require_finite(self.yaw_degrees, "motion proposal yaw")
        if type(self.movement) is not MovementV1:
            raise ContractViolation("motion proposal movement must be MovementV1")
        require_finite(self.horizon_blocks, "motion proposal horizon")
        if self.horizon_blocks <= 0:
            raise ContractViolation("motion proposal horizon must be positive")
        _int64(self.estimated_duration_ns, "motion proposal estimated duration")
        if self.estimated_duration_ns == 0:
            raise ContractViolation("motion proposal estimated duration must be positive")
        _optional_identifier(self.allowed_player_contact, "motion proposal allowed player contact")


@dataclass(frozen=True, slots=True)
class PerceptionNeed:
    need_id: str
    owner: str
    scope_id: str
    based_on: EvidenceStamp
    purpose: str
    priority: int
    deadline_ns: int
    point: Vec3V0
    condition: str
    block: tuple[int, int, int] | None = None
    face: str | None = None
    track_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.need_id, "perception need id")
        _identifier(self.scope_id, "perception need scope")
        if type(self.owner) is not str or self.owner not in OWNERS:
            raise ContractViolation("unknown perception need owner")
        if type(self.purpose) is not str or self.purpose not in PURPOSES:
            raise ContractViolation("unknown perception need purpose")
        if type(self.condition) is not str or self.condition not in CONDITIONS:
            raise ContractViolation("unknown perception need condition")
        _stamp(self.based_on, "perception need evidence")
        _int64(self.priority, "perception need priority")
        _int64(self.deadline_ns, "perception need deadline")
        if self.deadline_ns <= self.based_on.received_at_ns:
            raise ContractViolation("perception need deadline must follow its evidence")
        if type(self.point) is not Vec3V0:
            raise ContractViolation("perception need point must be Vec3V0")
        _block(self.block, "perception need block")
        if self.condition == "observed_block":
            if self.block is None or self.face is not None or self.purpose != "route_check":
                raise ContractViolation("route discovery requires a block without a face")
        elif (self.block is None) != (self.face is None):
            raise ContractViolation("perception need block and face must be supplied together")
        if self.face is not None and (type(self.face) is not str or self.face not in BLOCK_FACES):
            raise ContractViolation("invalid perception need block face")
        _optional_identifier(self.track_id, "perception need track id")
        work_face = (self.owner == "work_face" or self.purpose == "aim"
                     or self.condition == "center_block_face")
        if work_face and (self.block is None or self.face is None):
            raise ContractViolation("work-face need requires a block and face")
        tracking = (self.owner == "follow_track"
                    or self.purpose in {"track", "reacquire"}
                    or self.condition == "visible_track")
        if tracking and self.track_id is None:
            raise ContractViolation("tracking need requires a track id")


@dataclass(frozen=True, slots=True)
class MotionEvidenceReport:
    scope_id: str
    based_on: EvidenceStamp | None
    guard: GuardReport
    checked_yaw_degrees: float
    floor: int | None
    jump: bool
    allowed_player_contact: str | None
    evidence: tuple[tuple[tuple[int, int, int], EvidenceStamp], ...]
    earliest_expiry_ns: int | None
    summary_truncated: bool

    def __post_init__(self) -> None:
        _identifier(self.scope_id, "motion evidence scope")
        if self.based_on is not None:
            _stamp(self.based_on, "motion evidence basis")
        if type(self.guard) is not GuardReport:
            raise ContractViolation("motion evidence guard must be GuardReport")
        require_finite(self.checked_yaw_degrees, "motion evidence checked yaw")
        if self.floor is not None and type(self.floor) is not int:
            raise ContractViolation("motion evidence floor must be an integer or null")
        if type(self.jump) is not bool:
            raise ContractViolation("motion evidence jump must be bool")
        _optional_identifier(self.allowed_player_contact, "motion evidence allowed player contact")
        if type(self.evidence) is not tuple:
            raise ContractViolation("motion evidence must be an immutable tuple")
        if len(self.guard.gaps) + len(self.evidence) > MAX_SUMMARY:
            raise ContractViolation("motion evidence and gaps exceed the shared summary capacity")
        blocks = []
        for item in self.evidence:
            if type(item) is not tuple or len(item) != 2:
                raise ContractViolation("motion evidence entry must pair block and stamp")
            block, item_stamp = item
            _block(block, "motion evidence block")
            if block is None:
                raise ContractViolation("motion evidence block cannot be null")
            _stamp(item_stamp, "motion evidence stamp")
            if self.based_on is not None and (
                    item_stamp.scope != self.based_on.scope
                    or item_stamp.source_backend != self.based_on.source_backend):
                raise ContractViolation("motion evidence stamp scope mismatch")
            if self.based_on is not None and (
                    item_stamp.sequence_id > self.based_on.sequence_id
                    or item_stamp.request_start_ns > self.based_on.request_start_ns
                    or item_stamp.received_at_ns > self.based_on.received_at_ns
                    or item_stamp.client_sample.completed_at_monotonic_ns
                    > self.based_on.client_sample.completed_at_monotonic_ns):
                raise ContractViolation("motion evidence stamp is later than its report basis")
            blocks.append(block)
        if len(set(blocks)) != len(blocks):
            raise ContractViolation("motion evidence blocks must be unique")
        if self.based_on is None and self.evidence:
            raise ContractViolation("unbound motion report cannot carry evidence")
        if self.earliest_expiry_ns is not None:
            _int64(self.earliest_expiry_ns, "motion evidence earliest expiry")
        if type(self.summary_truncated) is not bool:
            raise ContractViolation("motion evidence truncation must be bool")
        if self.guard.summary_truncated and not self.summary_truncated:
            raise ContractViolation("motion evidence cannot downplay guard summary truncation")

    @property
    def reason(self) -> str | None:
        return self.guard.reason


@dataclass(frozen=True, slots=True)
class AimConstraint:
    need: PerceptionNeed
    operation_kind: str
    active: bool

    def __post_init__(self) -> None:
        if type(self.need) is not PerceptionNeed:
            raise ContractViolation("aim constraint need must be PerceptionNeed")
        if (self.need.owner != "work_face" or self.need.block is None or self.need.face is None
                or self.need.condition != "center_block_face"):
            raise ContractViolation("aim constraint requires a center work-face need")
        if type(self.operation_kind) is not str or self.operation_kind not in OPERATION_KINDS:
            raise ContractViolation("unknown aim operation kind")
        if type(self.active) is not bool:
            raise ContractViolation("aim constraint active must be bool")


@dataclass(frozen=True, slots=True)
class CandidateOutcome:
    condition_met: bool
    progress_debt: float
    elapsed_seconds: float | None
    unseen_seconds: float | None
    failure_seconds: float | None
    unknown_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.condition_met) is not bool:
            raise ContractViolation("candidate outcome condition_met must be bool")
        require_finite(self.progress_debt, "candidate outcome progress debt")
        if not 0 <= self.progress_debt <= 1:
            raise ContractViolation("candidate outcome progress debt must be within [0,1]")
        for name in ("elapsed_seconds", "unseen_seconds", "failure_seconds"):
            _optional_seconds(getattr(self, name), f"candidate outcome {name}")
        _identifier_tuple(self.unknown_reasons, "candidate outcome unknown reasons",
                          MAX_UNKNOWN_REASONS)


@dataclass(frozen=True, slots=True)
class PerceptionCandidate:
    candidate_id: str
    kind: str
    yaw_degrees: float
    pitch_degrees: float
    motion: MotionProposal | None
    need_ids: tuple[str, ...]
    stages: tuple[str, ...]
    outcomes: tuple[CandidateOutcome, CandidateOutcome]
    turn_units: float
    reversals: int
    stop_starts: int
    task_gaze_debt: float = 0.

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "perception candidate id")
        _identifier(self.kind, "perception candidate kind")
        require_finite(self.yaw_degrees, "perception candidate yaw")
        require_finite(self.pitch_degrees, "perception candidate pitch")
        if self.motion is not None and type(self.motion) is not MotionProposal:
            raise ContractViolation("perception candidate motion must be MotionProposal or null")
        _identifier_tuple(self.need_ids, "perception candidate need ids", MAX_ACTIVE_NEEDS)
        _identifier_tuple(self.stages, "perception candidate stages", MAX_STAGES)
        if not self.stages:
            raise ContractViolation("perception candidate requires at least one stage")
        if (type(self.outcomes) is not tuple or len(self.outcomes) != MAX_OUTCOMES
                or any(type(item) is not CandidateOutcome for item in self.outcomes)):
            raise ContractViolation("perception candidate requires exactly two immutable outcomes")
        require_finite(self.turn_units, "perception candidate turn units")
        if self.turn_units < 0:
            raise ContractViolation("perception candidate turn units must be nonnegative")
        require_finite(self.task_gaze_debt, 'perception candidate task gaze debt')
        if self.task_gaze_debt not in (0., 1.):
            raise ContractViolation('perception candidate task gaze debt must be zero or one')
        for name in ("reversals", "stop_starts"):
            value = getattr(self, name)
            require_nonnegative_int(value, f"perception candidate {name}")
            if value > MAX_STAGES:
                raise ContractViolation(f"perception candidate {name} exceeds stage capacity")


@dataclass(frozen=True, slots=True)
class PerceptionDecision:
    movement: MovementV1
    look: LookV1
    reason: str
    candidate_id: str | None
    completed_need_ids: tuple[str, ...]
    pending_need_ids: tuple[str, ...]
    score: float | None

    def __post_init__(self) -> None:
        if type(self.movement) is not MovementV1 or type(self.look) is not LookV1:
            raise ContractViolation("perception decision requires formal V1 controls")
        _identifier(self.reason, "perception decision reason")
        _optional_identifier(self.candidate_id, "perception decision candidate id")
        _identifier_tuple(self.completed_need_ids, "completed perception need ids", MAX_ACTIVE_NEEDS)
        _identifier_tuple(self.pending_need_ids, "pending perception need ids", MAX_ACTIVE_NEEDS)
        if len(self.completed_need_ids) + len(self.pending_need_ids) > MAX_ACTIVE_NEEDS:
            raise ContractViolation("perception decision exceeds active need capacity")
        if set(self.completed_need_ids).intersection(self.pending_need_ids):
            raise ContractViolation("completed and pending perception needs must be disjoint")
        if self.score is not None:
            require_finite(self.score, "perception decision score")
            if self.score < 0:
                raise ContractViolation("perception decision score must be nonnegative")
