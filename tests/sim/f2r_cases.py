"""Frozen F2-R fixtures; oracle geometry is confined to simulation tests."""
from dataclasses import replace
import math
import random
import hashlib
import json
from pathlib import Path

from mc2p.contracts.observation import Vec3V0
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
from mc2p.motion_nav.world_model import Aabb
from mc2p.skills.fixed_melee import COMBAT_GOAL_RADIUS_BLOCKS
from mc2p.skills.known_world_follow_driver import KnownWorldFollowDriver
from tests.sim.backend import Scene
from tests.sim.continuous_height_matrix import _rotate_cell, _rotate_point, _YAW_BY_DIRECTION
from tests.sim.runner import _goal, Scenario


TARGETS = ('product', 'melee', 'follow')
DIRECTIONS = ('south', 'east', 'north', 'west')


def frozen_manifest():
    return json.loads((Path(__file__).parent/'manifests/navigation-product-r28-v8.json').read_text('utf-8'))


def input_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def goal_for(position, target):
    if target == 'product':
        return _goal(position)
    if target == 'follow':
        return KnownWorldFollowDriver._goal(Vec3V0(*position))
    x, y, z = position
    r = COMBAT_GOAL_RADIUS_BLOCKS
    return GoalState(Aabb(x-r, y-.1, z-r, x+r, y+.1, z+r), GoalSupport.SOLID,
                     frozenset({MovementMode.WALK}), frozenset({'standing'}), .6)


def layout(family, direction='south', config=None):
    config = frozen_manifest()['geometry'] if config is None else config
    floor = config['floor_ranges']
    solids = {(x, floor['y'], z): config['material'] for x in range(*floor['x']) for z in range(*floor['z'])}
    posts = config['pillar_columns']
    if family == 'outer_corner':
        posts = [(x, z) for x in config['outer_corner_columns']['x'] for z in config['outer_corner_columns']['z']]
    elif family == 'two_pillars':
        posts = config['two_pillars_columns']
    elif family not in ('diagonal_pillar', 'multi_cell', 'diagonal_approach'):
        raise ValueError(family)
    solids.update({(x, y, z): config['material'] for x, z in posts for y in config['pillar_heights']})
    start = config['start'] if family != 'diagonal_approach' else config['diagonal_start']
    target = config['goal']
    corners = [_rotate_cell((x, 60, z), direction) for x in config['volume'][0] for z in config['volume'][2]]
    scene = Scene({_rotate_cell(p, direction): b for p, b in solids.items()},
        ((min(p[0] for p in corners), max(p[0] for p in corners)), tuple(config['volume'][1]),
         (min(p[2] for p in corners), max(p[2] for p in corners))))
    return scene, _rotate_point(start, direction), _rotate_point(target, direction)


def geometry_cases(config=None):
    config = frozen_manifest()['geometry'] if config is None else config
    for family, targets in config['families'].items():
        for target in targets:
            for direction in config['directions']:
                scene, start, position = layout(family, direction, config)
                goal = goal_for(position, target)
                if family == 'multi_cell' and target == 'product':
                    half = config['multi_cell_product_half_extent']
                    goal = replace(goal, region=Aabb(position[0]-half, 63.92, position[2]-half,
                                                    position[0]+half, 64.08, position[2]+half))
                for condition in config['conditions']:
                    yield {'id': f'f2r/{family}/{target}/{direction}/{condition}', 'family': family,
                        'target': target, 'direction': direction, 'condition': condition,
                        'start': list(start), 'goal': list(position), 'goal_box': list(goal.region.as_tuple()),
                        'yaw_degrees': _YAW_BY_DIRECTION[direction], 'max_ticks': config['max_ticks'],
                        'solids': [[*p, b] for p, b in sorted(scene.solids.items())],
                        'volume': scene.volume, 'expected': 'success'}


def clutter_cases(config=None):
    """Exact reviewer scan inputs, serialized before changing production code."""
    config = frozen_manifest()['clutter'] if config is None else config
    for density in config['densities']:
        seed = config['seed_template'].format(density=density)
        rng = random.Random(seed)
        for scene_index in range(config['scene_count']):
            posts = {(x, z) for x in range(*config['post_ranges']['x'])
                     for z in range(*config['post_ranges']['z']) if rng.random() < density}
            posts.difference_update(tuple(p) for p in config['removed_post_columns'])
            free = [(x, z) for x in range(*config['free_sample_ranges']['x'])
                    for z in range(*config['free_sample_ranges']['z']) if (x, z) not in posts]
            for sample in range(config['sample_count']):
                cx, cz = rng.choice(free)
                gx, gz = cx+rng.uniform(*config['sample_offset_range']), cz+rng.uniform(*config['sample_offset_range'])
                for target in config['targets']:
                    goal = goal_for((gx, 64., gz), target)
                    yield {'id': f'f2r/clutter/{density}/{scene_index}/{sample}/{target}',
                        'family': f'clutter_{int(density*100)}', 'target': target, 'seed': seed,
                        'scene_index': scene_index, 'sample': sample, 'column': [cx, cz],
                        'posts': [list(p) for p in sorted(posts)], 'goal': [gx, 64., gz],
                        'goal_box': list(goal.region.as_tuple())}


def scenario_for(case):
    if 'posts' in case:
        config = frozen_manifest()['clutter']
        floor = config['floor_ranges']
        solids = {(x, floor['y'], z): config['material'] for x in range(*floor['x']) for z in range(*floor['z'])}
        solids.update({(x, y, z): config['material'] for x, z in case['posts'] for y in config['pillar_heights']})
        return Scenario(case['id'], Scene(solids, tuple(tuple(v) for v in config['volume'])),
                        tuple(config['start']), tuple(case['goal']), max_ticks=config['max_ticks'])
    scene = Scene({tuple(row[:3]): row[3] for row in case['solids']},
                  tuple(tuple(v) for v in case['volume']))
    return Scenario(case['id'], scene, tuple(case['start']), tuple(case['goal']),
                    case['yaw_degrees'], max_ticks=case['max_ticks'])


def materialized_manifest():
    manifest = frozen_manifest()
    if (manifest.get('generator_id'), manifest.get('generator_version')) != ('mc2p.f2r-piecewise-inputs', 1):
        raise ValueError('unsupported frozen input generator')
    base = Path(__file__).parent/'manifests'/manifest['base_manifest']
    if hashlib.sha256(base.read_bytes()).hexdigest() != manifest['base_manifest_sha256']:
        raise ValueError('frozen v7 inputs changed')
    manifest['tasks'] = list(geometry_cases(manifest['geometry']))
    manifest['clutter_scan'] = list(clutter_cases(manifest['clutter']))
    for key, prefix in (('tasks', 'geometry'), ('clutter_scan', 'clutter')):
        if (len(manifest[key]) != manifest['expected_inputs'][prefix+'_cases']
                or input_digest(manifest[key]) != manifest['expected_inputs'][prefix+'_input_sha256']):
            raise ValueError('frozen F2-R inputs changed: '+prefix)
    return manifest
