"""E14：连跳时，“落地后多等几 tick 再起跳”要付出多少速度和跳远距离？

平地（y=0 全实心，足够长），疾跑 12 tick 后疾跑起跳，空中保持疾跑前进，着地。
着地后分别等待 0／1／2／3 个地面 tick（地面上继续按疾跑前进）再疾跑起跳，测：
- 第二跳起跳那一 tick 的水平速度；
- 第二跳从起跳到着地的水平距离（同高度）。
0 tick = 着地那一 tick 的输入就是起跳（输入必须在看到着地观察之前已经排进输入包）。
只用项目的 physics_1_21.step，不改项目文件。
运行：PYTHONPATH=<项目根> python e14_chain_delay.py
"""
import math
from experiments.motion_navigation.trajectory_proto.frozen_templates import SESSION, GEOMETRY_REVISION, _rest_state
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, TickInput, CalculationStatus
from mc2p.motion_nav.world_model import BlockGeometry, CellFact, CellKnowledge, ObservationStamp, WorldView

stamp = ObservationStamp(SESSION, 0, 0, "d097a-trial-v1", 0)
facts = {}
for x in range(-3, 4):
    for y in range(-2, 5):
        for z in range(-4, 90):
            solid = y == 0
            facts[(x, y, z)] = CellFact(CellKnowledge.BLOCK if solid else CellKnowledge.AIR, stamp,
                                        BlockGeometry.full_cube("minecraft:stone") if solid else None)
world = PhysicsWorldView(WorldView.detached(SESSION, GEOMETRY_REVISION, 0, facts), JAVA_1_21_RULESET)
SPRINT = TickInput(1., 0., False, False, True, 0.)
SPRINT_JUMP = TickInput(1., 0., True, False, True, 0.)


def advance(state, command):
    result = step(state, command, world, JAVA_1_21_RULESET)
    assert result.status is CalculationStatus.OK, result.status
    return result.next_state


def hspeed(state):
    return 20. * math.hypot(state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2])


def jump_once(state):
    """Sprint-jump now, hold sprint in the air; return (state on first grounded tick, takeoff z, landing z, air ticks)."""
    takeoff_z = state.position[2]
    state = advance(state, SPRINT_JUMP)
    ticks = 1
    while not state.on_ground:
        state = advance(state, SPRINT)
        ticks += 1
    return state, takeoff_z, state.position[2], ticks


state = _rest_state((.5, 1., 0.), 100)
for _ in range(12):
    state = advance(state, SPRINT)
print(f"run-up: speed after 12 sprint ticks = {hspeed(state):.3f} b/s")
landed, z0, z1, air = jump_once(state)
print(f"first jump: distance {z1 - z0:.3f} blocks, air ticks {air}, speed at touchdown {hspeed(landed):.3f} b/s")
base = None
for wait in range(4):
    s = landed
    for _ in range(wait):
        s = advance(s, SPRINT)
    speed_at_takeoff = hspeed(s)
    after, t0, t1, air2 = jump_once(s)
    distance = t1 - t0
    base = base or distance
    print(f"wait {wait} ground tick(s): speed at 2nd takeoff {speed_at_takeoff:.3f} b/s, "
          f"2nd jump distance {distance:.3f} blocks ({100 * (distance - base) / base:+.1f}% vs wait 0), air ticks {air2}")
# A third jump with zero waits on both landings, to show momentum build-up in a chain.
s = landed
dists = []
for _ in range(4):
    s, t0, t1, _ = jump_once(s)
    dists.append(round(t1 - t0, 3))
print("chain of 4 jumps, 0 ground ticks between:", dists, f"speed {hspeed(s):.3f} b/s")
s = landed
dists = []
for _ in range(4):
    s = advance(s, SPRINT)
    s, t0, t1, _ = jump_once(s)
    dists.append(round(t1 - t0, 3))
print("chain of 4 jumps, 1 ground tick between:", dists, f"speed {hspeed(s):.3f} b/s")
