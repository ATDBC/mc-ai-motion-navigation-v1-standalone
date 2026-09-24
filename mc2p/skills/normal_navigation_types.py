"""Frozen normal-only experiment configuration, not a new wire contract."""
from dataclasses import dataclass, field

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import (
    ContractViolation,
    require_finite,
    require_identifier,
    require_nonnegative_int,
)
from mc2p.contracts.observation import Vec3V0
from mc2p.skills.navigation_evidence import EvidenceStamp
from mc2p.skills.terrain_spatial_index import SPHERICAL_TERRAIN_BOUND


@dataclass(frozen=True, slots=True)
class ControlProposal:
    scope_id: str
    memory_generation: int
    based_on: EvidenceStamp
    movement: MovementV1
    look: LookV1
    origin: Vec3V0
    velocity: Vec3V0
    yaw: float
    pitch: float
    endpoint: Vec3V0

    def __post_init__(self) -> None:
        require_identifier(self.scope_id, "control proposal scope")
        require_nonnegative_int(
            self.memory_generation, "control proposal memory generation"
        )
        if self.memory_generation == 0 or self.memory_generation > 2**63-1:
            raise ContractViolation("control proposal memory generation must be positive int64")
        if type(self.based_on) is not EvidenceStamp:
            raise ContractViolation("control proposal evidence must be EvidenceStamp")
        if type(self.movement) is not MovementV1 or type(self.look) is not LookV1:
            raise ContractViolation("control proposal requires formal V1 controls")
        if type(self.origin) is not Vec3V0 or type(self.velocity) is not Vec3V0:
            raise ContractViolation("control proposal requires typed origin and velocity")
        if type(self.endpoint) is not Vec3V0:
            raise ContractViolation("control proposal endpoint must be Vec3V0")
        require_finite(self.yaw, "control proposal yaw")
        require_finite(self.pitch, "control proposal pitch")


@dataclass(frozen=True, slots=True)
class NormalNavigationConfig:
    group: str = 'A'
    revision: int = field(default=2, init=False)
    control_profile: str = field(default='forward_stop_v1', init=False)
    max_terrain: int = field(default=SPHERICAL_TERRAIN_BOUND, init=False)
    max_entities: int = field(default=64, init=False)
    terrain_retention_ns: int = field(default=60_000_000_000, init=False)
    entity_retention_ns: int = field(default=15_000_000_000, init=False)
    memory_radius_blocks: float = field(default=32., init=False)
    planning_radius_blocks: float = field(default=8., init=False)
    max_expansions: int = field(default=256, init=False)
    max_needs: int = field(default=8, init=False)
    max_candidates: int = field(default=16, init=False)
    planning_budget_ns: int = field(default=10_000_000, init=False)
    freshness_ns: int = field(default=500_000_000, init=False)
    step_lease_ns: int = field(default=250_000_000, init=False)
    interval_ns: int = field(default=50_000_000, init=False)
    owner_lease_ns: int = field(default=3_000_000_000, init=False)
    cleanup_ns: int = field(default=1_000_000_000, init=False)
    attempt_budget_ns: int = field(default=3_000_000_000, init=False)
    attempt_checks: int = field(default=16, init=False)
    max_recoveries: int = field(default=12, init=False)
    retry_wait_ns: int = field(default=250_000_000, init=False)
    empty_budget_ns: int = field(default=30_000_000_000, init=False)
    obstacle_budget_ns: int = field(default=90_000_000_000, init=False)

    def __post_init__(self) -> None:
        if type(self.group) is not str or self.group not in {'A', 'B', 'C', 'D', 'E', 'goal_directed_exploration'}:
            raise ContractViolation('unsupported normal navigation group')
        if self.group in {'D', 'E', 'goal_directed_exploration'}:
            object.__setattr__(self, 'control_profile', 'normal_decoupled_v1')


@dataclass(frozen=True, slots=True)
class JointCandidate:
    candidate_id: str
    endpoint: Vec3V0
    proposal: ControlProposal
    need_id: str | None
    required_by_ns: int | None
    intervals: tuple[tuple[int, int], ...]
    progress_debt_seconds: float
    recovery_seconds: float
    uncertainty_penalty: float
    gaze_debt: float
    valid_until_ns: int
    route_id: str = 'route'
    route_score: float = 0.
    gaze_kind: str = 'hold'
    need_priority: int = 9
    expected_acquired_ns: int | None = None
    missing_measurements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        from mc2p.skills.navigation_joint_cost import union_duration_ns
        require_identifier(self.candidate_id, 'joint candidate id')
        require_identifier(self.route_id, 'joint route id')
        if type(self.endpoint) is not Vec3V0 or type(self.proposal) is not ControlProposal:
            raise ContractViolation('joint candidate requires endpoint and proposal')
        for name in ('progress_debt_seconds', 'recovery_seconds',
                     'uncertainty_penalty', 'gaze_debt', 'route_score'):
            value = getattr(self, name)
            require_finite(value, name)
            if value < 0:
                raise ContractViolation(name+' must be nonnegative')
        require_nonnegative_int(self.valid_until_ns, 'candidate expiry')
        require_nonnegative_int(self.need_priority, 'need priority')
        for name in ('required_by_ns', 'expected_acquired_ns'):
            value = getattr(self, name)
            if value is not None:
                require_nonnegative_int(value, name)
        if self.need_id is not None:
            require_identifier(self.need_id, 'need id')
        union_duration_ns(self.intervals)
