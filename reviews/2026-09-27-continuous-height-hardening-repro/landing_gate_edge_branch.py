"""Round-17 review: what does the direct-drop landing gate actually accept?

direct_drop_visual_evidence_sufficient() accepts
    (lower_region_visible and observer_distance <= 4.5)  OR  fresh_edge_probe
where fresh_edge_probe only asks that the body is on ground, sneaking and within
0.35 blocks (horizontally) of the landing cell centre, plus some visual-air
evidence no older than 5 observation sequences.

Part 1 calls the gate with evidence that saw only the UPPER part of the landing
cell (lower_region_visible = False) from 9 blocks away.
Part 2 shows the distance the client would report from the edge for the landing
body cell of a direct drop of h blocks (eye-to-nearest-point of the cell, as in
SurfaceSensor.java), standing and sneaking.

Run from the repository root of a 5fa2f33 checkout:
    PYTHONPATH=. python -B <this script>
"""
import math
from dataclasses import replace

from mc2p.motion_nav.route_admission import (
    DIRECT_DROP_EVIDENCE_DISTANCE_BLOCKS, direct_drop_visual_evidence_sufficient,
)
from mc2p.motion_nav.world_model import (
    BlockGeometry, ObservationStamp, VisualAirEvidence, WorldKnowledge, WorldSessionId,
)
from tests.motion_nav.test_b07_step_transition import frame

session = WorldSessionId("landing-gate")
world = WorldKnowledge(session)
early = ObservationStamp(session, 10, 10, "test-clock", 10)
landing = (1, 59, 0)                         # body cell of a 5-block drop off (0, 63, 0)
world.observe_blocks(early, {(0, 63, 0): BlockGeometry.full_cube("minecraft:stone"),
                             (1, 58, 0): BlockGeometry.full_cube("minecraft:stone")})
upper_only = VisualAirEvidence(early, observer_distance_blocks=9.0, lower_region_visible=False)
world.confirm_air(early, (landing,), visual_evidence={landing: upper_only})

edge = (1.2, 64.0, .5)                       # sneak edge: box still overlaps the platform
for label, position, sneaking in (("standing at centre  ", (.5, 64.0, .5), False),
                                  ("standing at edge    ", edge, False),
                                  ("sneaking at edge    ", edge, True)):
    current = frame(world, 12, position)
    current = replace(current, body=replace(current.body, is_sneaking=sneaking,
                                            pose="crouching" if sneaking else "standing"))
    print(f"part 1: upper-only evidence seen 9 blocks away, {label}:",
          direct_drop_visual_evidence_sufficient(current, landing))

print("\npart 2: nearest-point distance from the eye to the landing body cell, body at the edge "
      f"(gate limit {DIRECT_DROP_EVIDENCE_DISTANCE_BLOCKS})")
for drop in (2, 3, 4, 5, 8):
    for pose, eye_height in (("standing", 1.62), ("sneaking", 1.27)):
        eye = (edge[0], 64.0 + eye_height, edge[2])
        cell = (1, 64 - drop, 0)
        d = [max(cell[i] - eye[i], eye[i] - cell[i] - 1, 0.0) for i in range(3)]
        distance = math.sqrt(sum(v * v for v in d))
        print(f"  drop {drop} ({pose}): {distance:.2f}  "
              f"{'within' if distance <= DIRECT_DROP_EVIDENCE_DISTANCE_BLOCKS else 'beyond'} the limit")
