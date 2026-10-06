"""Load only the geometry needed to replay action facts, without execution state."""
from mc2p.motion_nav.action_route import ControlledDropSegment
from mc2p.motion_nav.controlled_drop import ControlledDropEdge
from mc2p.motion_nav.support_surfaces import SupportSurface, SurfaceNodeId, HorizontalRegion


def geometry_action(data):
    def surface(item):
        return SupportSurface(SurfaceNodeId(**item['node_id']), tuple(item['position']),
            HorizontalRegion(**item['region']), item['support_fraction'], tuple(item['materials']),
            tuple(map(tuple, item['dependencies'])))
    start, end = surface(data['start_surface']), surface(data['end_surface'])
    edge = data['edge']
    return ControlledDropSegment(ControlledDropEdge(start.node_id, end.node_id,
        edge['profile_id'], edge['cost_seconds'], tuple(map(tuple, edge['dependencies']))),
        start, end, tuple(map(tuple, data['dependencies'])))
