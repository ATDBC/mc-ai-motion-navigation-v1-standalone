"""Read-only diagnosis with existing fixtures and production controllers."""
from __future__ import annotations
from dataclasses import replace
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.fixed_route import FixedRoute, FixedRouteController, RoutePoint
from mc2p.motion_nav.action_route import ActionRoute, WalkSegment
from mc2p.motion_nav.ground_motion import PlanarBodyState
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.online_motion import project_movement_command
from mc2p.motion_nav.geometry import WorldQueryCache
from mc2p.motion_nav.world_model import BlockGeometry, ObservationStamp
from tests.motion_nav.test_fixed_route_walk import FlatFixture, profile
from tests.motion_nav.test_b08_ground_modes import B08GroundModeTests
from tests.motion_nav.test_b10_gap_solver import fixture as gap_fixture


def modes():
    B08GroundModeTests.setUpClass()
    helper = B08GroundModeTests()
    output = []
    for left, right in ((MovementMode.WALK, MovementMode.SPRINT),
                        (MovementMode.SPRINT, MovementMode.WALK),
                        (MovementMode.WALK, MovementMode.WALK),
                        (MovementMode.WALK, MovementMode.CROUCH),
                        (MovementMode.CROUCH, MovementMode.WALK)):
        fixture = FlatFixture()
        initial = fixture.frame(0, PlanarBodyState(.5,.5,0,0,0))
        def segment(name,z0,z1,mode):
            return WalkSegment(FixedRoute(name, (RoutePoint(.5,1,z0), RoutePoint(.5,1,z1))),
                               ((0,1,int(z0)), (0,1,int(z1))), (), helper.transition(mode))
        route = ActionRoute(f"audit-{left.value}-{right.value}", (
            segment("first",.5,1.5,left), segment("second",1.5,3.5,right)))
        executor = helper.executor()
        executor.start(route,initial)
        at_boundary = fixture.frame(1,PlanarBodyState(.5,1.5,0,1.0,0))
        at_boundary = replace(at_boundary,body=replace(at_boundary.body,
            is_sprinting=left is MovementMode.SPRINT,
            is_sneaking=left is MovementMode.CROUCH,
            pose="crouching" if left is MovementMode.CROUCH else "standing"))
        decision = executor.decide(at_boundary)
        output.append(dict(source=left.value,destination=right.value,speed=1.0,
            action_index=decision.action_index,reason=decision.reason_code,
            movement=dict(forward=decision.movement.forward,strafe=decision.movement.strafe,
                          sprint=decision.movement.sprint,sneak=decision.movement.sneak)))
    return output


def corner():
    fixture, motion = FlatFixture(), profile()
    route = FixedRoute("open-corner", (RoutePoint(.5,1,.5), RoutePoint(.5,1,4.5), RoutePoint(5.5,1,4.5)))
    body = PlanarBodyState(.5,3.0,0,2.35,0)
    frame = fixture.frame(0,body)
    controller = FixedRouteController(motion)
    controller.start(route,frame)
    decision = controller.decide(frame)
    target = controller._geometry.point_at(controller._progress + 1.073)
    forward = controller._evaluate_candidate(frame,body,MovementV1(forward=1),target,
        braking=False,query_cache=WorldQueryCache(frame.world))
    return dict(reason=decision.reason,movement=dict(forward=decision.movement.forward,strafe=decision.movement.strafe),
                speed=2.35,corner_distance=controller._geometry.next_sharp_corner_distance(controller._progress,controller._segment_index),
                forward_candidate=dict(blocked=forward.blocked,unsupported=forward.unsupported,
                    missing_count=len(forward.missing),progress_gain=forward.progress_gain,score=forward.score))


def wall_tail():
    fixture,motion = FlatFixture(),profile()
    fixture.world.observe_blocks(ObservationStamp(fixture.session,1,1,"test-clock",1),{
        (0,y,2):BlockGeometry.full_cube("minecraft:stone") for y in (1,2)})
    body=PlanarBodyState(.5,1.15,0,2.35,0)
    frame=fixture.frame(0,body)
    controller=FixedRouteController(motion)
    controller.start(FixedRoute("wall-tail",(RoutePoint(.5,1,1.15),RoutePoint(.5,1,1.7))),frame)
    movement=MovementV1(forward=1)
    rollout=controller._prepare_candidate_rollout(body,movement,(.5,1.7),braking=False)
    candidate=controller._evaluate_candidate(frame,body,movement,(.5,1.7),braking=False,
        query_cache=WorldQueryCache(frame.world),rollout=rollout)
    template=gap_fixture()[0].physics_state
    state=replace(template,session=fixture.session,movement_tick_id=0,position=(.5,1,1.15),
        velocity_blocks_per_tick=(0,-.0784000015258789,2.35/20),yaw_radians=0)
    world=PhysicsWorldView(frame.world,JAVA_1_21_RULESET)
    rows=[]
    for index in range(14):
        command=movement if index<2 else MovementV1()
        projected=project_movement_command(state,command)
        result=step(state,projected.tick_input,world,JAVA_1_21_RULESET)
        if result.next_state is None:
            raise RuntimeError((index,result.status,result.missing_cells))
        state=result.next_state
        rows.append(dict(tick=index+1,z=state.position[2],vz=state.velocity_blocks_per_tick[2]*20,
            horizontal_collision=state.horizontal_collision,on_ground=state.on_ground,
            events=list(result.events)))
    return dict(controller_candidate=dict(blocked=candidate.blocked,unsupported=candidate.unsupported,
        missing_count=len(candidate.missing)),lease_end_z=rollout.tracking_end.z,
        lease_end_body_front=rollout.tracking_end.z+.3,wall_face_z=2,
        uncollided_infinite_tail_z=rollout.states[-1].z,physics_rows=rows)


if __name__=="__main__":
    output=dict(mode_boundaries=modes(),open_corner=corner(),ordinary_wall_tail=wall_tail())
    target=Path(__file__).with_name("diagnosis.json")
    target.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(output,ensure_ascii=False,indent=2))
