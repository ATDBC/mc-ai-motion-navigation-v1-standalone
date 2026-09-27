"""Round-14 review: the formal navigation chain never asks to re-check a known block.

The client can now turn a whole-visible, empty, previously solid cell into
visual air, and scripts/visual_air_runtime.py proves it with a hand-built
ObservationRequestV3.  The formal request path goes through
NavigationObservationAdapter.air_request(), which keeps only UNKNOWN cells, and
NavigationSession.observation_request() only feeds it UNKNOWN missing cells.

Run from the repository root of a b740098 checkout:
    PYTHONPATH=. python -B <this script>
"""
from mc2p.contracts.observation_v3 import CollisionShapeV3, ObservedBlockV3
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from tests.observation_v3_fixtures import valid_snapshot_v3

support = (3, 63, 0)
stone = ObservedBlockV3(support, "minecraft:stone", CollisionShapeV3("full_cube"),
                        None, ("surface_depth",))
adapter = NavigationObservationAdapter()
frame = adapter.ingest(valid_snapshot_v3(blocks=(stone,), sequence=1))
print("support cell knowledge:", frame.world.cell(support).knowledge.value)
request, deferred = adapter.air_request((support,))
print("formal air request for that cell:", request.air_positions, "deferred:", deferred)
