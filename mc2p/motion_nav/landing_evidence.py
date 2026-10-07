"""Direct descent visual evidence, without admission or action dependencies."""
from __future__ import annotations
from typing import TYPE_CHECKING
from mc2p.motion_nav.world_model import BlockPos, CellKnowledge
if TYPE_CHECKING:
    from mc2p.motion_nav.runtime_adapter import NavigationFrame
    from mc2p.motion_nav.landing_edge_probe import LandingEdgeProbe

DIRECT_DROP_SUPPORT_MAX_AGE_TICKS = 1
DIRECT_DROP_EVIDENCE_DISTANCE_BLOCKS = 4.5
DIRECT_DROP_EVIDENCE_MAX_AGE_TICKS = 5


def direct_drop_visual_evidence_sufficient(
    frame: NavigationFrame,
    landing_cell: BlockPos,
    *,
    edge_probe: LandingEdgeProbe | None = None,
) -> bool:
    """Accept nearby lower evidence or evidence owned by the active edge probe."""
    fact = frame.world.cell(landing_cell)
    if fact.knowledge is not CellKnowledge.AIR:
        return False
    if (edge_probe is not None
            and edge_probe.owned
            and edge_probe.landing_cell == landing_cell):
        # Once an edge probe owns this landing check, fresh air that becomes
        # visible during its approach must not bypass the probe's return-to-
        # entry and sneak-release lifecycle merely because it is nearby.
        return edge_probe.allows_evidence(frame, landing_cell)
    evidence = fact.visual_air_evidence
    if evidence is None:
        return False
    age = frame.body.stamp.sequence_id - evidence.stamp.sequence_id
    if age < 0 or age > DIRECT_DROP_EVIDENCE_MAX_AGE_TICKS:
        return False
    lower_volume_seen = (
        evidence.lower_region_visible
        and evidence.observer_distance_blocks
            <= DIRECT_DROP_EVIDENCE_DISTANCE_BLOCKS + 1.0e-9
    )
    return lower_volume_seen
