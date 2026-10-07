"""Immutable action requirements and acquisition identities, independent of callers."""
from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum
import hashlib
from typing import TYPE_CHECKING
from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.world_model import BlockPos, CellKnowledge
if TYPE_CHECKING:
    from mc2p.motion_nav.runtime_adapter import NavigationFrame
    from mc2p.motion_nav.route_admission import ActiveRoute

class ActionPreconditionStatus(StrEnum):
    READY = "ready"
    NEEDS_ACQUISITION = "needs_acquisition"
    NEEDS_INFORMATION = "needs_information"
    REJECTED = "rejected"


class ActionPreconditionReason(StrEnum):
    READY = "action_precondition_ready"
    LANDING_LOWER_EVIDENCE_REQUIRED = "landing_lower_evidence_required"
    LANDING_SUPPORT_INFORMATION_REQUIRED = (
        "landing_support_information_required"
    )
    LANDING_SUPPORT_MISSING = "landing_support_missing"
    LANDING_SUPPORT_UNSUPPORTED = "landing_support_unsupported"
    ACTION_INDEX_INVALID = "action_index_invalid"


@dataclass(frozen=True, slots=True)
class AcquisitionSpec:
    acquisition_id: str
    task_id: str
    goal_id: str
    goal_revision: int
    route_id: str
    route_revision: int
    action_index: int
    landing_cell: BlockPos
    world_session: str
    geometry_revision: int
    dependencies: tuple[BlockPos, ...]

    def __post_init__(self) -> None:
        for value, label in (
            (self.acquisition_id, "acquisition id"),
            (self.task_id, "acquisition task id"),
            (self.goal_id, "acquisition goal id"),
            (self.route_id, "acquisition route id"),
            (self.world_session, "acquisition world session"),
        ):
            require_identifier(value, label)
        if (type(self.goal_revision) is not int or self.goal_revision < 0
                or type(self.route_revision) is not int
                or self.route_revision < 0
                or type(self.action_index) is not int
                or self.action_index < 0
                or type(self.geometry_revision) is not int
                or self.geometry_revision < 0):
            raise ContractViolation("acquisition identity is invalid")
        if (type(self.dependencies) is not tuple
                or self.dependencies != tuple(sorted(set(self.dependencies)))):
            raise ContractViolation(
                "acquisition dependencies must be sorted and unique"
            )


@dataclass(frozen=True, slots=True)
class AcquisitionGrant:
    acquisition_id: str
    route_id: str
    route_revision: int
    action_index: int
    landing_cell: BlockPos
    evidence_sequence_id: int
    dependencies: tuple[BlockPos, ...]

    def __post_init__(self) -> None:
        require_identifier(self.acquisition_id, "acquisition grant id")
        require_identifier(self.route_id, "acquisition grant route id")
        if (type(self.route_revision) is not int or self.route_revision < 0
                or type(self.action_index) is not int or self.action_index < 0
                or type(self.evidence_sequence_id) is not int
                or self.evidence_sequence_id < 0):
            raise ContractViolation("acquisition grant identity is invalid")
        if (type(self.dependencies) is not tuple
                or self.dependencies != tuple(sorted(set(self.dependencies)))):
            raise ContractViolation(
                "acquisition grant dependencies must be sorted and unique"
            )

    def applies(
        self, route: ActiveRoute, action_index: int, frame: NavigationFrame,
    ) -> bool:
        if (self.route_id != route.route_id
                or self.route_revision != route.route_revision
                or self.action_index != action_index):
            return False
        fact = frame.world.cell(self.landing_cell)
        if fact.knowledge is not CellKnowledge.AIR:
            return False
        # The grant owns the lower-region observation made by the edge probe.
        # A later view from the route entry can still confirm that this cell is
        # air while no longer seeing its lower region.  That ordinary refresh
        # must not erase the completed acquisition or extend its lifetime.
        age = frame.body.sequence_id - self.evidence_sequence_id
        return 0 <= age <= 80


@dataclass(frozen=True, slots=True)
class ActionPreconditionResult:
    status: ActionPreconditionStatus
    reason: ActionPreconditionReason
    acquisition: AcquisitionSpec | None = None
    missing_cells: tuple[BlockPos, ...] = ()

    def __post_init__(self) -> None:
        if (type(self.status) is not ActionPreconditionStatus
                or type(self.reason) is not ActionPreconditionReason):
            raise ContractViolation("action precondition result must be typed")
        if (type(self.missing_cells) is not tuple
                or self.missing_cells
                    != tuple(sorted(set(self.missing_cells)))):
            raise ContractViolation(
                "action precondition missing cells must be sorted and unique"
            )
        if ((self.status is ActionPreconditionStatus.NEEDS_ACQUISITION)
                != (self.acquisition is not None)):
            raise ContractViolation(
                "acquisition result must own exactly one acquisition spec"
            )


def acquisition_id(
    route: ActiveRoute, action_index: int, landing_cell: BlockPos,
) -> str:
    raw = (
        f"{route.route_id}:{route.route_revision}:{action_index}:"
        f"{landing_cell[0]}:{landing_cell[1]}:{landing_cell[2]}"
    )
    return "landing-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
