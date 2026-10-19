"""Minimal dirt placement on the existing calculator and input-sampling backend.

No actor knowledge is filled from truth. Contacts, inventory and block targeting
are observed through V3; placement is a dispatched player operation.
"""
from dataclasses import replace
import math

from mc2p.contracts.action_v1 import InteractBlockV1
from mc2p.contracts.common import FieldStatusV0
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v2 import ItemStackV2
from mc2p.contracts.observation_v3 import TargetingStateV3
from tests.follow_v3_fixtures import observed_block
from tests.sim.backend import CalculatorBackend, Scene


class PlacementCalculatorBackend(CalculatorBackend):
    def __init__(self, *args, **kwargs):
        self.items = 3
        self.placements = []
        self.inventory_delivery_missing = False
        self.report_wrong_destination = False
        self.partial_world_only = False
        self.confirmation_started = None
        self.dispatch_impulse = None
        self.dispatch_lift = 0.
        self.requested_profile = "navigation_v1"
        super().__init__(*args, **kwargs)

    def _target(self):
        state = self.state
        eye = (state.position[0], state.position[1] + (1.27 if state.pose == "crouching" else 1.62),
               state.position[2])
        direction = (-math.sin(state.yaw_radians) * math.cos(state.pitch_radians),
                     -math.sin(state.pitch_radians),
                     math.cos(state.yaw_radians) * math.cos(state.pitch_radians))
        best = None
        faces = (("west", "east"), ("down", "up"), ("north", "south"))
        for cell, block in self.scene.solids.items():
            if block not in {"minecraft:stone", "minecraft:dirt"}:
                continue
            near, far, face = 0., 4.5, None
            for axis in range(3):
                if abs(direction[axis]) < 1e-10:
                    if not cell[axis] <= eye[axis] <= cell[axis] + 1:
                        near, far = 1., 0.
                        break
                    continue
                a = (cell[axis] - eye[axis]) / direction[axis]
                b = (cell[axis] + 1 - eye[axis]) / direction[axis]
                incoming = faces[axis][0 if direction[axis] > 0 else 1]
                if a > b:
                    a, b = b, a
                if a > near:
                    near, face = a, incoming
                far = min(far, b)
            if face is not None and near <= far and (best is None or near < best[0]):
                best = (near, cell, face, tuple(eye[i] + near * direction[i] for i in range(3)))
        return best

    def observation(self, **kwargs):
        snapshot = super().observation(**kwargs)
        blocks = {block.position: block for block in snapshot.perception.value.blocks}
        box = self.state.body_box
        for x in range(math.floor(box.min_x), math.ceil(box.max_x)):
            for y in range(math.floor(box.min_y) - 1, math.ceil(box.max_y)):
                for z in range(math.floor(box.min_z), math.ceil(box.max_z)):
                    cell = (x, y, z)
                    if cell not in self.scene.solids:
                        if cell in blocks:
                            blocks[cell] = replace(blocks[cell], sources=tuple(sorted(set(blocks[cell].sources) | {"body_contact"})))
                        else:
                            blocks[cell] = observed_block(cell, "minecraft:air", kind="empty", sources=("body_contact",))
        target = self._target()
        interaction = self.requested_profile == "interaction_v1"
        targeting = TargetingStateV3("miss", None, None, None, None, None)
        if target is not None and interaction:
            distance, cell, face, point = target
            targeting = TargetingStateV3("block", cell, None, face, Vec3V0(*point), distance)
            if cell in blocks:
                block = blocks[cell]
                blocks[cell] = replace(block, sources=tuple(sorted(set(block.sources) | {"current_target"})))
        empty = ItemStackV2(True, None, 0, 0, 0, False, None)
        count = 3 if self.partial_world_only else self.items
        held = ItemStackV2(False, "minecraft:dirt", count, 0, 0, False, None) if count else empty
        inventory = replace(snapshot.inventory.value, main=(held,) + (empty,) * 35,
                            main_hand=held, selected_hotbar_slot=0)
        if self.report_wrong_destination:
            for cell in self.placements:
                if cell in blocks:
                    blocks[cell] = replace(blocks[cell], block_id="minecraft:stone")
        snapshot = replace(snapshot, field_profile=self.requested_profile,
            inventory=replace(snapshot.inventory, value=inventory),
            targeting=(replace(snapshot.targeting, status=FieldStatusV0.VALID, reason_code=None, value=targeting)
                       if interaction else snapshot.targeting),
            perception=replace(snapshot.perception, value=replace(snapshot.perception.value,
                blocks=tuple(blocks[cell] for cell in sorted(blocks)),
                air_query_results=tuple(result for result in snapshot.perception.value.air_query_results
                    if result.status == "visible_air" or result.position not in blocks))))
        if self.inventory_delivery_missing and self.placements:
            snapshot = replace(snapshot, inventory=replace(snapshot.inventory,
                status=FieldStatusV0.MISSING, reason_code="test_delivery_gap", value=None))
        return snapshot

    def step(self, action, deadline, **kwargs):
        request = kwargs.get("observation_request")
        self.requested_profile = "navigation_v1" if request is None else request.field_profile
        operation = action.operation
        placed = False
        if type(operation) is InteractBlockV1:
            target = self._target()
            support = (operation.block_x, operation.block_y, operation.block_z)
            if target is not None and target[1:3] == (support, operation.face):
                delta = {"east": (1, 0, 0), "west": (-1, 0, 0), "north": (0, 0, -1),
                         "south": (0, 0, 1), "up": (0, 1, 0), "down": (0, -1, 0)}[operation.face]
                cell = tuple(support[i] + delta[i] for i in range(3))
                if self.items and cell not in self.scene.solids:
                    self.scene = Scene({**self.scene.solids, cell: "minecraft:dirt"}, self.scene.volume)
                    self._build_truth()
                    self.items -= 1
                    self.placements.append(cell)
                    self.confirmation_started = self.movement_tick + 1
                    if self.dispatch_impulse is not None:
                        self.state = replace(self.state,
                            velocity_blocks_per_tick=self.dispatch_impulse,
                            position=(self.state.position[0], self.state.position[1] + self.dispatch_lift,
                                      self.state.position[2]), on_ground=self.dispatch_lift == 0.)
                    placed = True
        result = super().step(action, deadline, **kwargs)
        if placed:
            result = replace(result, receipt=replace(result.receipt,
                status="pending_confirmation", reason="block_use_dispatched"))
        return result
