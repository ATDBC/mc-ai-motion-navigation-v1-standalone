"""Consumers of a hypothetical expanded exit; does not authorize motion."""
from dataclasses import asdict
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.motion_nav.test_fixed_route_walk import FlatFixture, profile
from mc2p.motion_nav.action_route import JumpGapSegment
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
from mc2p.motion_nav.fixed_route import FixedRoute, FixedRouteController, RoutePoint, _RouteGeometry
from mc2p.motion_nav.ground_motion import PlanarBodyState
from mc2p.motion_nav.jump_gap import JumpGapEdge
from mc2p.motion_nav.support_surfaces import HorizontalRegion, SupportSurface, SurfaceNodeId
from mc2p.motion_nav.geometry import query_support

fixture = FlatFixture()
route = FixedRoute('audit-following-walk', tuple(RoutePoint(.5, 1., z + .5) for z in range(4, 9)))
start_id, end_id = SurfaceNodeId(0, 2, 1, 0), SurfaceNodeId(0, 4, 1, 0)
start = SupportSurface(start_id, (.5, 1., 2.5), HorizontalRegion(0., 2., 1., 3.), 1., ('minecraft:grass_block',), ())
end = SupportSurface(end_id, (.5, 1., 4.5), HorizontalRegion(0., 4., 1., 5.), 1., ('minecraft:grass_block',), ())
action = JumpGapSegment(JumpGapEdge(start_id, end_id, 'audit-gap', 1., ()), start, end, ())
results = []
for z in (5.05, 5.4, 6.8):
    frame = fixture.frame(1, PlanarBodyState(.5, z, 0., 1.5, 0.))
    geometry = _RouteGeometry(route)
    projection = geometry.project(.5, z, 0., 0, .45)
    controller = FixedRouteController(profile())
    controller.start(route, frame)
    decision = controller.decide(frame)
    support = query_support(frame.body.body_box, frame.world)
    results.append({'body_z': z, 'support_fraction': support.support_fraction,
        'projection': asdict(projection), 'controller_state': decision.state.value,
        'controller_reason': decision.reason, 'controller_movement': asdict(decision.movement),
        'old_destination_fallback_accepts': ActionRouteExecutor._landed_on_current_action_destination(action, frame)})
Path(__file__).with_name('consumer-probe-results.json').write_text(
    json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(results, ensure_ascii=False))
