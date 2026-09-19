from __future__ import annotations

import math
import random
import unittest
from unittest.mock import patch

from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshotBuilder, PlanningRequest, PlanningStatus,
    SnapshotBuildStatus, WalkEdge, WalkGraph, WalkNode, astar_plan,
    build_walk_graph, dijkstra_reference, plan_known_snapshot,
)
from mc2p.motion_nav.ground_motion import PlanarBodyState
from mc2p.motion_nav.fixed_route import FixedRouteController, FixedRouteState, RoutePoint
from mc2p.motion_nav.route_admission import (
    ActiveRouteTracker, AdmissionStatus, CorridorStatus, RouteAdmitter,
)
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
    WorldView,
)
from tests.motion_nav.test_fixed_route_walk import FlatFixture, apply, profile


def grid_graph(seed: int) -> tuple[WalkGraph, tuple[int, int, int], tuple[int, int, int]]:
    randomizer=random.Random(seed)
    width=height=8
    present={(x,1,z) for x in range(width) for z in range(height)
             if randomizer.random()>.22}
    start,goal=(0,1,0),(width-1,1,height-1)
    present.update((start,goal))
    nodes=tuple(WalkNode(node,(node[0]+.5,1.0,node[2]+.5),())
                for node in sorted(present))
    edges=[]
    for node in sorted(present):
        for dx,dz in ((1,0),(-1,0),(0,1),(0,-1)):
            other=(node[0]+dx,1,node[2]+dz)
            if other in present:
                cost=1.0+randomizer.random()*2.0
                edges.append(WalkEdge(node,other,cost,()))
    return WalkGraph('random',seed,KnownMapBounds(0,width-1,1,1,0,height-1,True),
                     nodes,tuple(sorted(edges,key=lambda edge:(edge.start,edge.end))),False),start,goal


class KnownMapPlanningTests(unittest.TestCase):
    def known_fixture(self):
        session=WorldSessionId('b04-known')
        world=WorldKnowledge(session)
        stamp=ObservationStamp(session,1,1,'clock',50_000_000)
        floors={(x,0,z):BlockGeometry.full_cube('minecraft:grass_block')
                for x in range(5) for z in range(5)}
        del floors[(2,0,2)]
        world.observe_blocks(stamp,floors)
        air={(x,y,z) for x in range(5) for y in (1,2) for z in range(5)}|{(2,0,2)}
        world.confirm_air(stamp,tuple(sorted(air)))
        world.observe_blocks(stamp,{
            (1,1,2):BlockGeometry.full_cube('minecraft:stone'),
            (1,2,2):BlockGeometry.full_cube('minecraft:stone'),
            (3,2,2):BlockGeometry.full_cube('minecraft:stone'),
        })
        return world

    def test_walk_graph_uses_support_clearance_and_sweep_from_shared_geometry(self):
        world=self.known_fixture()
        bounds=KnownMapBounds(0,4,1,1,0,4,True)
        graph=build_walk_graph(world.view(),bounds,profile())
        ids={node.node_id for node in graph.nodes}
        self.assertNotIn((1,1,2),ids,'两格高墙不能成为站位')
        self.assertNotIn((2,1,2),ids,'坑不能成为站位')
        self.assertNotIn((3,1,2),ids,'第二层悬浮墙必须阻止身体净空')
        self.assertIn((0,1,0),ids)
        self.assertTrue(graph.complete_scope)
        edge_pairs={(edge.start,edge.end) for edge in graph.edges}
        self.assertNotIn(((0,1,2),(1,1,2)),edge_pairs)
        self.assertTrue(all(edge.cost_seconds>0 for edge in graph.edges))

    def test_snapshot_copy_is_bounded_detached_and_restarts_after_geometry_change(self):
        fixture=FlatFixture();bounds=KnownMapBounds(-3,3,1,1,-3,3,True)
        builder=KnownMapSnapshotBuilder(fixture.world.view(),bounds)
        previous=0
        while True:
            progress=builder.advance(fixture.world.view(),7)
            self.assertLessEqual(progress.scanned_cells-previous,7)
            previous=progress.scanned_cells
            if progress.status is SnapshotBuildStatus.COMPLETE:break
            self.assertIs(progress.status,SnapshotBuildStatus.BUILDING)
        self.assertIsNotNone(progress.snapshot)
        old=progress.snapshot.world.cell((0,1,0))
        stamp=ObservationStamp(fixture.session,3,3,'test-clock',150_000_000)
        fixture.world.observe_blocks(stamp,{(0,1,0):BlockGeometry.full_cube('minecraft:stone')})
        self.assertEqual(progress.snapshot.world.cell((0,1,0)),old,
                         '后台快照不能随控制侧世界继续变化')

        stale=KnownMapSnapshotBuilder(fixture.world.view(),bounds)
        self.assertIs(stale.advance(fixture.world.view(),1).status,
                      SnapshotBuildStatus.BUILDING)
        fixture.world.observe_blocks(ObservationStamp(
            fixture.session,4,4,'test-clock',200_000_000),
            {(1,1,0):BlockGeometry.full_cube('minecraft:stone')})
        self.assertIs(stale.advance(fixture.world.view(),7).status,
                      SnapshotBuildStatus.STALE)

    def test_snapshot_completion_does_not_recopy_all_validated_facts(self):
        fixture=FlatFixture();bounds=KnownMapBounds(-3,3,1,1,-3,3,True)
        builder=KnownMapSnapshotBuilder(fixture.world.view(),bounds)
        with patch.object(WorldView,"detached",
                          side_effect=AssertionError("completion recopied the full snapshot")):
            progress=builder.advance(fixture.world.view(),100_000)
        self.assertIs(progress.status,SnapshotBuildStatus.COMPLETE)
        self.assertIsNotNone(progress.snapshot)
        self.assertIs(progress.snapshot.world.cell((0,1,0)).knowledge,
                      fixture.world.view().cell((0,1,0)).knowledge)

    def test_lazy_snapshot_astar_matches_materialized_graph(self):
        fixture=FlatFixture();motion=profile();bounds=KnownMapBounds(-4,4,1,1,-2,10,True)
        stamp=ObservationStamp(fixture.session,2,2,'test-clock',100_000_000)
        fixture.world.observe_blocks(stamp,{(0,y,4):BlockGeometry.full_cube('minecraft:stone')
                                            for y in (1,2)})
        builder=KnownMapSnapshotBuilder(fixture.world.view(),bounds)
        progress=builder.advance(fixture.world.view(),100_000)
        self.assertIs(progress.status,SnapshotBuildStatus.COMPLETE)
        request=PlanningRequest(1,'snapshot','goal',1,fixture.session.value,
                                (0,1,0),(0,1,8))
        lazy=plan_known_snapshot(progress.snapshot,motion,request)
        materialized=astar_plan(build_walk_graph(fixture.world.view(),bounds,motion),request)
        self.assertIs(lazy.status,PlanningStatus.COMPLETE)
        self.assertEqual(lazy.total_cost_seconds,materialized.total_cost_seconds)
        self.assertEqual(lazy.path[0].node_id,request.start)
        self.assertEqual(lazy.path[-1].node_id,request.goal)

    def test_standable_endpoints_do_not_create_an_edge_through_a_thin_barrier(self):
        session=WorldSessionId('b04-thin-edge');world=WorldKnowledge(session)
        stamp=ObservationStamp(session,1,1,'clock',50_000_000)
        world.observe_blocks(stamp,{(x,0,0):BlockGeometry.full_cube('minecraft:grass_block')
                                    for x in (0,1)})
        world.confirm_air(stamp,tuple((x,y,0) for x in (0,1) for y in (1,2)))
        world.observe_blocks(stamp,{(0,1,0):BlockGeometry(
            'minecraft:iron_bars','boxes',(Aabb(.95,0,0,1,1,1),))})
        graph=build_walk_graph(world.view(),KnownMapBounds(0,1,1,1,0,0,True),profile())
        self.assertEqual({node.node_id for node in graph.nodes},{(0,1,0),(1,1,0)})
        self.assertNotIn(((0,1,0),(1,1,0)),
                         {(edge.start,edge.end) for edge in graph.edges})

    def test_missing_facts_do_not_become_air_or_a_complete_no_route_claim(self):
        world=self.known_fixture()
        stamp=ObservationStamp(world.session,2,2,'clock',100_000_000)
        world.invalidate(stamp,((0,1,1),))
        graph=build_walk_graph(world.view(),KnownMapBounds(0,0,1,1,0,2,True),profile())
        self.assertFalse(graph.complete_scope)
        result=astar_plan(graph,PlanningRequest(1,'request','goal',1,world.session.value,
                                                (0,1,0),(0,1,2)))
        self.assertIs(result.status,PlanningStatus.NO_KNOWN_ROUTE)

    def test_unsupported_geometry_or_floor_material_cannot_be_reported_as_proven_no_route(self):
        for label,position,geometry in (
            ("collision",(0,1,1),BlockGeometry.unsupported("minecraft:custom_shape")),
            ("support",(0,0,1),BlockGeometry.full_cube("minecraft:ice")),
        ):
            with self.subTest(label=label):
                fixture=FlatFixture()
                stamp=ObservationStamp(fixture.session,2,2,"test-clock",100_000_000)
                fixture.world.observe_blocks(stamp,{position:geometry})
                bounds=KnownMapBounds(0,0,1,1,0,2,True)
                request=PlanningRequest(1,f"unsupported-{label}","goal",1,
                                        fixture.session.value,(0,1,0),(0,1,2))
                graph=build_walk_graph(fixture.world.view(),bounds,profile())
                self.assertTrue(graph.has_unsupported)
                self.assertIs(astar_plan(graph,request).status,PlanningStatus.UNSUPPORTED)
                builder=KnownMapSnapshotBuilder(fixture.world.view(),bounds)
                progress=builder.advance(fixture.world.view(),100_000)
                self.assertIs(progress.status,SnapshotBuildStatus.COMPLETE)
                self.assertIs(plan_known_snapshot(progress.snapshot,profile(),request).status,
                              PlanningStatus.UNSUPPORTED)

    def test_one_thousand_random_graphs_match_independent_dijkstra(self):
        for seed in range(1000):
            graph,start,goal=grid_graph(seed)
            request=PlanningRequest(seed,'request','goal',1,'random',start,goal)
            candidate=astar_plan(graph,request)
            reference=dijkstra_reference(graph,start,goal)
            with self.subTest(seed=seed):
                self.assertEqual(candidate.status is PlanningStatus.COMPLETE,reference is not None)
                if reference is not None:
                    reference_cost,_=reference
                    self.assertLessEqual(abs(candidate.total_cost_seconds-reference_cost),
                                         1e-6*max(1.0,reference_cost))

    def test_complete_scope_distinguishes_a_proven_no_route(self):
        graph,start,goal=grid_graph(12)
        isolated=WalkGraph(graph.world_session,graph.geometry_revision,graph.bounds,
                           graph.nodes,tuple(edge for edge in graph.edges
                                             if edge.start!=start and edge.end!=start),
                           graph.has_unsupported)
        result=astar_plan(isolated,PlanningRequest(1,'request','goal',1,'random',start,goal))
        self.assertIs(result.status,PlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE)

    def test_expansion_budget_reports_timeout_instead_of_no_route(self):
        graph,start,goal=grid_graph(5)
        result=astar_plan(graph,PlanningRequest(1,'bounded','goal',1,'random',start,goal,1))
        self.assertIs(result.status,PlanningStatus.TIMEOUT)

    def test_route_admission_rejects_stale_identity_and_keeps_route_id_stable(self):
        fixture=FlatFixture();motion=profile()
        graph=build_walk_graph(fixture.world.view(),KnownMapBounds(-2,2,1,1,-2,5,True),motion)
        request=PlanningRequest(7,'request-7','goal-a',3,fixture.session.value,(0,1,0),(0,1,4))
        candidate=astar_plan(graph,request)
        frame=fixture.frame(0,PlanarBodyState(.5,.5,0,0,0))
        admitter=RouteAdmitter(maximum_corridor_blocks=2.5)
        accepted=admitter.admit(candidate,frame,expected_request_id='request-7',
                                goal_id='goal-a',goal_revision=3,
                                changed_cells=())
        self.assertIs(accepted.status,AdmissionStatus.ACCEPTED)
        active=accepted.route
        self.assertIsNotNone(active)
        self.assertLess(active.corridor.length_blocks,active.fixed_route_length_blocks)
        self.assertEqual(len(active.fixed_route.points),2,
                         '连续同方向图边应合并为执行直线，不能让网格点制造减速')
        same=admitter.admit(candidate,frame,expected_request_id='request-7',
                            goal_id='goal-a',goal_revision=3,
                            changed_cells=())
        self.assertEqual(active.route_id,same.route.route_id)
        stamp=ObservationStamp(fixture.session,2,2,'test-clock',100_000_000)
        fixture.world.observe_blocks(stamp,{(15,1,15):BlockGeometry.full_cube('minecraft:stone')})
        changed_frame=fixture.frame(1,PlanarBodyState(.55,.5,0,0,0))
        remote=admitter.admit(candidate,changed_frame,expected_request_id='request-7',
                              goal_id='goal-a',goal_revision=3,
                              changed_cells=((15,1,15),))
        self.assertIs(remote.status,AdmissionStatus.ACCEPTED)
        self.assertEqual(active.route_id,remote.route.route_id)
        self.assertFalse(active.corridor.affected_by(((15,1,15),)))
        self.assertTrue(active.corridor.affected_by(active.corridor.dependencies[:1]))
        tracker=ActiveRouteTracker(active,candidate,maximum_corridor_blocks=2.5)
        first_stop=tracker.update(0).route.corridor.stop_node
        tracker.apply_changes(((15,1,15),))
        advanced=tracker.update(2.1)
        self.assertIs(advanced.status,CorridorStatus.READY)
        self.assertEqual(advanced.route.route_id,active.route_id)
        self.assertNotEqual(advanced.route.corridor.stop_node,first_stop)
        future_dependency=advanced.route.corridor.dependencies[-1:]
        tracker.apply_changes(future_dependency)
        self.assertIs(tracker.update(2.1).status,CorridorStatus.BLOCKED_BY_CHANGE)
        self.assertIs(admitter.admit(candidate,frame,expected_request_id='request-7',
                                     goal_id='goal-a',goal_revision=4,
                                     changed_cells=()).status,AdmissionStatus.REJECTED)
        self.assertIs(admitter.admit(candidate,frame,expected_request_id='newer-request',
                                     goal_id='goal-a',goal_revision=3,
                                     changed_cells=()).status,AdmissionStatus.REJECTED)
        changed=active.corridor.dependencies[:1]
        self.assertTrue(changed)
        self.assertIs(admitter.admit(candidate,frame,expected_request_id='request-7',
                                     goal_id='goal-a',goal_revision=3,
                                     changed_cells=changed).status,AdmissionStatus.REJECTED)

    def test_admitted_offset_start_includes_the_verified_connection_segment(self):
        fixture=FlatFixture();motion=profile()
        graph=build_walk_graph(fixture.world.view(),KnownMapBounds(-2,2,1,1,-2,5,True),motion)
        request=PlanningRequest(8,'offset-request','goal-a',1,fixture.session.value,
                                (0,1,0),(0,1,4))
        candidate=astar_plan(graph,request)
        body=PlanarBodyState(1.29,.5,0,0,0)
        frame=fixture.frame(0,body)
        admitted=RouteAdmitter(maximum_corridor_blocks=8).admit(
            candidate,frame,expected_request_id=request.request_id,
            goal_id=request.goal_id,goal_revision=request.goal_revision,
            changed_cells=(),
        )
        self.assertIs(admitted.status,AdmissionStatus.ACCEPTED)
        active=admitted.route
        self.assertIsNotNone(active)
        self.assertAlmostEqual(active.connection_length_blocks,.79,places=6)
        self.assertAlmostEqual(active.corridor.length_blocks,
                               active.fixed_route_length_blocks,places=6)
        self.assertEqual(active.fixed_route.points[0],RoutePoint(1.29,1.0,.5))
        self.assertEqual(active.fixed_route.points[1],RoutePoint(.5,1.0,.5))
        controller=FixedRouteController(motion)
        controller.start(active.fixed_route,frame)
        decision=controller.decide(frame)
        self.assertIs(decision.state,FixedRouteState.RUNNING)
        tracker=ActiveRouteTracker(active,candidate,maximum_corridor_blocks=8)
        partway=tracker.update(.5)
        self.assertEqual(partway.route.corridor.stop_node,
                         tracker.update(0).route.corridor.stop_node)
        self.assertAlmostEqual(partway.route.corridor.length_blocks,
                               active.fixed_route_length_blocks-.5,places=6)
        connection_only=tuple(sorted(
            set(active.connection_dependencies)-set(candidate.dependencies)
        ))
        self.assertTrue(connection_only)
        tracker.apply_changes(connection_only[:1])
        self.assertIs(tracker.update(.5).status,CorridorStatus.BLOCKED_BY_CHANGE)

        narrow=RouteAdmitter(maximum_corridor_blocks=2.5).admit(
            candidate,frame,expected_request_id=request.request_id,
            goal_id=request.goal_id,goal_revision=request.goal_revision,
            changed_cells=(),
        ).route
        self.assertIsNotNone(narrow)
        initial_stop=narrow.corridor.stop_node
        initial_length=narrow.corridor.length_blocks
        self.assertLessEqual(initial_length,2.5)
        unchanged=ActiveRouteTracker(
            narrow,candidate,maximum_corridor_blocks=2.5,
        ).update(0).route.corridor
        self.assertEqual(unchanged.stop_node,initial_stop)
        self.assertAlmostEqual(unchanged.length_blocks,initial_length,places=6)

    def test_planned_open_wall_pit_and_floating_routes_execute_with_b03_controller(self):
        for scenario in ('open','wall','pit','floating'):
            with self.subTest(scenario=scenario):
                fixture=FlatFixture();motion=profile()
                stamp=ObservationStamp(fixture.session,2,2,'test-clock',100_000_000)
                if scenario=='wall':
                    fixture.world.observe_blocks(stamp,{(0,y,4):BlockGeometry.full_cube('minecraft:stone')
                                                        for y in (1,2)})
                elif scenario=='pit':
                    fixture.world.confirm_air(stamp,((0,0,4),))
                elif scenario=='floating':
                    fixture.world.observe_blocks(stamp,{(0,2,4):BlockGeometry.full_cube('minecraft:stone')})
                graph=build_walk_graph(fixture.world.view(),KnownMapBounds(-3,3,1,1,0,8,True),motion)
                request=PlanningRequest(1,f'plan-{scenario}','goal',1,fixture.session.value,
                                        (0,1,0),(0,1,8))
                candidate=astar_plan(graph,request)
                self.assertIs(candidate.status,PlanningStatus.COMPLETE)
                if scenario!='open':self.assertNotIn((0,1,4),[node.node_id for node in candidate.path])
                body=PlanarBodyState(.5,.5,0,0,0);frame=fixture.frame(0,body)
                admitted=RouteAdmitter(maximum_corridor_blocks=10).admit(
                    candidate,frame,expected_request_id=request.request_id,
                    goal_id='goal',goal_revision=1,changed_cells=())
                self.assertIs(admitted.status,AdmissionStatus.ACCEPTED)
                controller=FixedRouteController(motion);controller.start(admitted.route.fixed_route,frame)
                for sequence in range(1,401):
                    decision=controller.decide(fixture.frame(sequence,body))
                    if decision.state is FixedRouteState.SUCCEEDED:break
                    self.assertNotIn(decision.state,{FixedRouteState.BLOCKED,FixedRouteState.FAILED,
                                                    FixedRouteState.UNSUPPORTED})
                    body=apply(body,decision.movement,motion)
                self.assertIs(decision.state,FixedRouteState.SUCCEEDED)


if __name__=='__main__':
    unittest.main()
