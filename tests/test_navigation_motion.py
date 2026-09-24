import unittest
from dataclasses import replace

from mc2p.contracts.observation import Vec3V0
from mc2p.skills.follow_tracking import project_playground_view
from mc2p.skills.local_navigation import plan_local_route
from mc2p.skills.navigation_memory import NavigationMemory
from mc2p.skills.navigation_motion import NavigationBlockMap, check_navigation_motion
from tests.follow_v3_fixtures import follow_snapshot, observed_block

NOW = 100_000_000


def floor_snapshot(sequence=0, now=NOW, *, yaw=0, pitch=30, floors=True, **kwargs):
    position = kwargs.pop('position', (.5, 64, .5))
    blocks = [observed_block((x,63,z)) for x in range(-2,3) for z in range(-3,9)] if floors else []
    return follow_snapshot(sequence=sequence, received=now, position=position, yaw=yaw, pitch=pitch,
                           blocks=blocks, **kwargs)


def history(memory, obs, now=None):
    return memory.observe(obs, now_ns=obs.received_at_monotonic_ns if now is None else now,
                          controller_clock_id='controller-test', scope_id='scope')


class NavigationMotionTests(unittest.TestCase):
    def test_old_history_can_propose_route_but_cannot_authorize_motion(self):
        memory = NavigationMemory('scope')
        history(memory, floor_snapshot())
        obs = floor_snapshot(1, NOW+3_000_000_000, floors=False)
        snapshot = history(memory, obs)
        view = project_playground_view(obs,obs.received_at_monotonic_ns,'controller-test')
        route = plan_local_route(NavigationBlockMap(snapshot),view.base,Vec3V0(.5,64,2.5),
                                  obs.received_at_monotonic_ns,support_freshness_ns=60_000_000_000)
        self.assertEqual(route.cells, ((0,0),(0,1),(0,2)))
        self.assertEqual(check_navigation_motion(snapshot,view,obs.received_at_monotonic_ns,63,0),
                         'unknown_footprint_support')

    def test_fresh_map_reuses_walk_and_jump_guard_and_does_not_invent_miss_support(self):
        for floors, expected in ((True,None),(False,'unknown_footprint_support')):
            memory = NavigationMemory('scope')
            obs = floor_snapshot(floors=floors,entities=[])
            snapshot = history(memory,obs)
            view = project_playground_view(obs,NOW,'controller-test')
            self.assertEqual(check_navigation_motion(snapshot,view,NOW,63,0), expected)
            if floors:
                self.assertIsNone(check_navigation_motion(snapshot,view,NOW,63,0,jump=True))

    def test_old_or_foreign_snapshot_cannot_pair_with_new_view(self):
        memory=NavigationMemory('scope'); obs=floor_snapshot()
        snapshot=history(memory,obs)
        newer=floor_snapshot(1,NOW+50_000_000)
        view=project_playground_view(newer,newer.received_at_monotonic_ns,'controller-test')
        self.assertEqual(check_navigation_motion(snapshot,view,newer.received_at_monotonic_ns,63,0),
                         'navigation_observation_mismatch')

    def test_unobserved_neighbor_keeps_original_time_through_map_projection(self):
        memory=NavigationMemory('scope'); history(memory,floor_snapshot())
        obs=follow_snapshot(sequence=1,received=NOW+50_000_000,position=(.5,64,.5),
                            blocks=[observed_block((0,62,0))])
        snapshot=history(memory,obs)
        self.assertEqual(NavigationBlockMap(snapshot).records[(0,63,0)].last_seen_ns,NOW-1_000_000)

    def test_missing_or_differently_aimed_view_cannot_authorize_motion(self):
        memory=NavigationMemory('scope'); obs=floor_snapshot(entities=[])
        snapshot=history(memory,obs)
        view=project_playground_view(obs,NOW,'controller-test')
        missing=replace(view,base=replace(view.base,available=False,own=None))
        self.assertEqual(check_navigation_motion(snapshot,missing,NOW,63,0),
                         'navigation_observation_unavailable')
        turned=replace(view,base=replace(view.base,own=replace(view.base.own,yaw=90)))
        self.assertEqual(check_navigation_motion(snapshot,turned,NOW,63,90),
                         'navigation_observation_mismatch')
