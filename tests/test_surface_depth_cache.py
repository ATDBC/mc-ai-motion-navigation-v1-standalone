"""Native diagnostic cache invariants; run after the explicit offline build."""
from pathlib import Path
import importlib.util
import numpy as np
import random
import unittest

ROOT=Path(__file__).resolve().parents[1]

def load():
    path=ROOT/'scripts/surface_depth_probe/native.py'
    assert path.exists(), 'surface cache wrapper has not been implemented'
    spec=importlib.util.spec_from_file_location('surface_cache_test_api',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

def world(blocks):
    data=dict(boxes=[],owners=[],centers=[],opaque=[])
    for i,(pos,parts,opaque) in enumerate(blocks):
        data['centers'].append([v+.5 for v in pos]);data['opaque'].append(int(opaque))
        for b in parts:
            data['boxes'].append([b[k]+pos[k%3] for k in range(6)]);data['owners'].append(i)
    return data

FULL=[[0,0,0,1,1,1]]
HALF=[[0,0,0,1,.5,1]]
STAIR=[[0,0,0,1,.5,1],[0,.5,.5,1,1,1]]

def test_stable_identity_reorder_and_local_change(tile):
    api=load();blocks=[((x,0,3),FULL,True) for x in range(-9,10)]
    with api.Cache(tile) as c:
        c.update(world(blocks));ids=c.identities.copy()
        c.update(world(blocks[::-1]))
        assert np.array_equal(c.identities,ids[::-1])
        assert c.stats[3]==0, 'reordering unchanged blocks must not rebuild tiles'
        c.update(world(blocks+[ ((1,2,4),FULL,True) ]))
        assert np.array_equal(c.identities[:-1],ids)
        assert 0<c.stats[3]<c.stats[4], 'one addition must leave distant tiles intact'

def test_border_remove_transparency_and_shape_match_full_rebuild(tile):
    api=load();front=(tile-1,1,3);back=(tile-1,1,5)
    states=[[(front,FULL,True),(back,FULL,True)],[(front,FULL,False),(back,FULL,True)],
            [(back,FULL,True)],[(front,HALF,True),(back,FULL,True)],
            [(front,FULL,True),((tile,1,3),FULL,True),(back,FULL,True)]]
    with api.Cache(tile) as c:
        for blocks in states:
            data=world(blocks);c.update(data)
            for cam in ([tile-.5,1.5,0,0],[tile+2,2,-1,-25]):
                fast=c.frame(cam,True);full=c.frame(cam,False)
                assert fast[0].tolist()==full[0].tolist()==api.reference(data,cam)[0].tolist()
                assert np.allclose(full[1],api.reference(data,cam)[1],rtol=1e-8,atol=1e-10)
            assert fast[3][1]==0, 'surface-only path must not merge block silhouettes'

def test_recycled_slot_generation_range_and_fast_exit():
    api=load()
    with api.Cache(4) as c:
        d=world([((0,0,3),FULL,True)]);c.update(d);old=c.identities.copy()
        c.update(world([]));c.update(world([((0,0,20),FULL,True)]))
        assert c.identities[0,0]==old[0,0] and c.identities[0,1]>old[0,1]
        assert not c.frame([0,2,0,0],True)[0].any()
        c.update(d);fast=c.frame([-3,3,-3,30],True);full=c.frame([-3,3,-3,30],False)
        assert fast[0].tolist()==full[0].tolist()==[1]
        assert fast[3][6]<full[3][6], 'boolean query should stop after its first visible face'

def test_ctypes_rejects_incomplete_arrays_before_native_call():
    api=load();data=world([((0,0,3),FULL,True)])
    for key,value in [('owners',[]),('opaque',[]),('owners',[1])]:
        try:api.arrays(dict(data,**{key:value}))
        except ValueError:pass
        else:raise AssertionError(f'invalid {key} must be rejected before native pointer access')
    with api.Cache(4) as c:
        c.update(data)
        for cam,query in [([0,1,0],None),([0,1,0,0],[]),([0,1,float('nan'),0],None),([0,1,0,0],[2])]:
            try:c.frame(cam,True,query)
            except ValueError:pass
            else:raise AssertionError('invalid camera/query must be rejected before native pointer access')

def test_enclosed_full_cube_culling_preserves_visible_positions():
    api=load();rng=random.Random(21001)
    faces=((1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1))
    poses=[(-6,2.2,-6,45,0),(6,3.1,-6,-45,-15),(-6,4.0,6,135,20),(6,2.0,6,-135,0)]
    for trial in range(220):
        blocks=[]
        for x in range(-3,4):
            for y in range(-2,3):
                for z in range(-3,4):
                    if rng.random()<.72:
                        parts=FULL if rng.random()<.85 else HALF
                        blocks.append(((x,y,z),parts,rng.random()<.88))
        table={position:(parts,opaque) for position,parts,opaque in blocks}
        def full_opaque(position):
            item=table.get(position)
            return item is not None and item[0]==FULL and item[1]
        culled=[]
        for block in blocks:
            position=block[0]
            enclosed=full_opaque(position) and all(full_opaque((position[0]+dx,position[1]+dy,position[2]+dz))
                                                    for dx,dy,dz in faces)
            if not enclosed:culled.append(block)
        with api.Cache(4) as complete,api.Cache(4) as shell:
            complete.update(world(blocks));shell.update(world(culled))
            for eye_x,eye_y,eye_z,yaw,pitch in poses:
                original=complete.frame_pose([eye_x,eye_y,eye_z,yaw,pitch],True)[0]
                reduced=shell.frame_pose([eye_x,eye_y,eye_z,yaw,pitch],True)[0]
                original_positions={blocks[i][0] for i,value in enumerate(original) if value}
                reduced_positions={culled[i][0] for i,value in enumerate(reduced) if value}
                assert original_positions==reduced_positions

def test_entity_boxes_reuse_surface_occlusion():
    api=load()
    wall=world([((0,0,3),FULL,True),((0,1,3),FULL,True)])
    with api.Cache(4) as cache:
        cache.update(wall)
        boxes=[
            [0.2,0.1,4.0,0.8,1.9,4.6],
            [0.8,0.1,4.0,1.4,1.9,4.6],
            [20.0,0.1,4.0,20.6,1.9,4.6],
        ]
        visible=cache.visible_boxes([.5,1.62,0,0,0],boxes,16.)
        assert visible.tolist()==[0,1,0]
    glass=world([((0,0,3),FULL,False),((0,1,3),FULL,False)])
    with api.Cache(4) as cache:
        cache.update(glass)
        visible=cache.visible_boxes([.5,1.62,0,0,0],[boxes[0]],16.)
        assert visible.tolist()==[1]

def test_visual_air_accepts_any_visible_region():
    api=load()
    candidates=[(0,1,3),(0,1,-3),(0,1,17)]
    with api.Cache(4) as empty:
        empty.update(world([]))
        _,air,_,_=empty.frame_pose_air([.5,1.5,0,0,0],candidates)
        assert air.tolist()==[1,2,2]
    opaque_wall=world([((0,1,2),FULL,True)])
    with api.Cache(4) as wall:
        wall.update(opaque_wall)
        _,air,_,_=wall.frame_pose_air([.5,1.5,0,0,0],[(0,1,3)])
        assert air.tolist()==[0]
        # An off-axis blocker has to lie on the eye-to-cell ray. Equal world x
        # values at different depths are visibly beside each other in perspective.
        wall.update(world([((-2,1,3),FULL,True)]))
        _,air,_,_=wall.frame_pose_air([.5,1.5,0,0,0],[(-4,1,6)])
        assert air.tolist()==[0]
        wall.update(world([((-4,1,3),FULL,True)]))
        _,air,_,_=wall.frame_pose_air([.5,1.5,0,0,0],[(-4,1,6)])
        assert air.tolist()==[1]
    transparent_wall=world([((0,1,2),FULL,False)])
    with api.Cache(4) as glass:
        glass.update(transparent_wall)
        _,air,_,_=glass.frame_pose_air([.5,1.5,0,0,0],[(0,1,3)])
        assert air.tolist()==[1]
    thin_occluder=world([((0,1,2),((.45,0,0,.55,1,1),),True)])
    with api.Cache(4) as fence:
        fence.update(thin_occluder)
        _,air,_,_=fence.frame_pose_air([.5,1.5,0,0,0],[(0,1,3)])
        assert air.tolist()==[1], 'uncovered part of the hypothetical cell proves visual air'
    with api.Cache(4) as stairs:
        stairs.update(world([((0,1,2),STAIR,True)]))
        _,air,_,_=stairs.frame_pose_air([.5,1.5,0,0,0],[(0,1,3)])
        assert air.tolist()==[0], 'this stair silhouette fully covers the target from the fixed pose'
    with api.Cache(4) as camera_face:
        camera_face.update(world([]))
        _,air,_,_=camera_face.frame_pose_air([.5,1.5,-.001,0,0],[(0,1,0)])
        assert air.tolist()==[2], 'a cell crossing the camera near plane is not a stable view candidate'

def test_visual_air_matches_any_visible_projected_area_in_random_worlds():
    api=load();rng=random.Random(260927);camera=[.5,1.5,0,0,0]
    shapes=(FULL,HALF,STAIR,((.4,0,0,.6,1,1),))
    with api.Cache(4) as cache:
        for _ in range(500):
            candidate=(rng.randint(-2,2),1,rng.randint(4,8));used={candidate};blocks=[]
            for _ in range(rng.randint(4,18)):
                position=(rng.randint(-3,3),rng.randint(0,3),rng.randint(1,10))
                if position in used:continue
                used.add(position);blocks.append((position,rng.choice(shapes),rng.random()<.8))
            query=(candidate,FULL,False)
            visible=api.reference(world([*blocks,query]),camera[:4],2)[1][-1]
            expected=visible>1e-12
            cache.update(world(blocks))
            actual=bool(cache.frame_pose_air(camera,[candidate])[1][0])
            assert actual==expected,(candidate,visible,blocks)

def test_visual_air_distinguishes_outside_view_from_occlusion():
    api=load()
    with api.Cache(4) as cache:
        cache.update(world([((0,1,2),FULL,True)]))
        _,status,_,_=cache.frame_pose_air(
            [.5,1.5,0,0,0],[(0,1,3),(0,1,-3),(0,1,17)])
        assert status.tolist()==[0,2,2]

def test_visual_air_confirms_a_partly_exposed_downward_cell_without_preobservation():
    api=load();blocks=[]
    for x in range(-4,5):
        for z in range(-4,5):
            blocks.append(((x,-1,z),FULL,True))
            if x<0:blocks.append(((x,0,z),FULL,True))
    with api.Cache(4) as cache:
        cache.update(world(blocks))
        visible=False
        for pitch in range(-90,91,5):
            _,air,_,_=cache.frame_pose_air([-.31,2.62,.5,90,pitch],[(0,0,0)])
            visible=visible or bool(air[0])
        assert visible, 'the drop body cell has a visible region even though its near face is blocked'

def load_tests(loader, tests, pattern):
    suite=unittest.TestSuite()
    for tile in (4,8):
        for fn in (test_stable_identity_reorder_and_local_change,test_border_remove_transparency_and_shape_match_full_rebuild):
            suite.addTest(unittest.FunctionTestCase(lambda f=fn,t=tile:f(t),description=f'{fn.__name__}[{tile}]'))
    suite.addTest(unittest.FunctionTestCase(test_recycled_slot_generation_range_and_fast_exit))
    suite.addTest(unittest.FunctionTestCase(test_ctypes_rejects_incomplete_arrays_before_native_call))
    suite.addTest(unittest.FunctionTestCase(test_enclosed_full_cube_culling_preserves_visible_positions))
    suite.addTest(unittest.FunctionTestCase(test_entity_boxes_reuse_surface_occlusion))
    suite.addTest(unittest.FunctionTestCase(test_visual_air_accepts_any_visible_region))
    suite.addTest(unittest.FunctionTestCase(test_visual_air_matches_any_visible_projected_area_in_random_worlds))
    suite.addTest(unittest.FunctionTestCase(test_visual_air_distinguishes_outside_view_from_occlusion))
    suite.addTest(unittest.FunctionTestCase(test_visual_air_confirms_a_partly_exposed_downward_cell_without_preobservation))
    return suite

if __name__=='__main__':unittest.main()
