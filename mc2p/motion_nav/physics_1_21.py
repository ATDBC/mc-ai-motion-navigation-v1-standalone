"""Deterministic one-tick rules for the supported Minecraft Java 1.21 subset."""
from __future__ import annotations

from dataclasses import replace
import math

from mc2p.motion_nav.geometry import required_cells_for_sweep
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import (
    CalculationStatus, MotionSegment, PhysicsRuleset, PhysicsState,
    ResourceStatus, ResourceUpdate, StepResult, TickInput,
)
from mc2p.motion_nav.world_model import Aabb, CellKnowledge


_EPSILON = 1.0e-9
_INPUT_RETENTION = 0.98
_AIR_RETENTION = 0.91
_VERTICAL_RETENTION = 0.9800000190734863
_GROUND_ACCELERATION_NUMERATOR = 0.21600002
_ORDINARY_SLIPPERINESS = 0.6
_SPRINT_SPEED_MULTIPLIER = 1.300000011920929
_POSE_HEIGHTS = {"standing": 1.8, "crouching": 1.5, "swimming": 0.6}
_ORDINARY_MATERIALS = frozenset({
    "minecraft:dirt", "minecraft:glass", "minecraft:grass_block",
    "minecraft:oak_planks", "minecraft:stone", "minecraft:dirt_path",
    "minecraft:oak_stairs", "minecraft:smooth_stone_slab",
    "minecraft:snow", "minecraft:white_carpet",
})


def _same_session(state: PhysicsState, world: PhysicsWorldView,
                  ruleset: PhysicsRuleset) -> tuple[str, ...]:
    reasons = []
    if state.session != world.session:
        reasons.append("world_session_mismatch")
    if state.ruleset_id != ruleset.ruleset_id or world.ruleset != ruleset:
        reasons.append("ruleset_mismatch")
    if state.state_schema != ruleset.state_schema:
        reasons.append("state_schema_mismatch")
    return tuple(reasons)


def _input_velocity(strafe: float, forward: float, speed: float,
                    yaw_radians: float) -> tuple[float, float]:
    x, z = strafe * _INPUT_RETENTION, forward * _INPUT_RETENTION
    length_squared = x * x + z * z
    if length_squared < 1.0e-7:
        return 0.0, 0.0
    if length_squared > 1.0:
        length = math.sqrt(length_squared)
        x, z = x / length, z / length
    x, z = x * speed, z * speed
    sine, cosine = math.sin(yaw_radians), math.cos(yaw_radians)
    return x * cosine - z * sine, z * cosine + x * sine


def _overlap(a_min: float, a_max: float, b_min: float, b_max: float) -> bool:
    return a_max > b_min + _EPSILON and a_min < b_max - _EPSILON


def _clip_axis(box: Aabb, obstacles: tuple[Aabb, ...], delta: float, axis: int) -> float:
    if abs(delta) <= _EPSILON:
        return 0.0
    value = delta
    for obstacle in obstacles:
        if axis == 0:
            if not (_overlap(box.min_y, box.max_y, obstacle.min_y, obstacle.max_y)
                    and _overlap(box.min_z, box.max_z, obstacle.min_z, obstacle.max_z)):
                continue
            if value > 0 and box.max_x <= obstacle.min_x + _EPSILON:
                value = min(value, obstacle.min_x - box.max_x)
            elif value < 0 and box.min_x >= obstacle.max_x - _EPSILON:
                value = max(value, obstacle.max_x - box.min_x)
        elif axis == 1:
            if not (_overlap(box.min_x, box.max_x, obstacle.min_x, obstacle.max_x)
                    and _overlap(box.min_z, box.max_z, obstacle.min_z, obstacle.max_z)):
                continue
            if value > 0 and box.max_y <= obstacle.min_y + _EPSILON:
                value = min(value, obstacle.min_y - box.max_y)
            elif value < 0 and box.min_y >= obstacle.max_y - _EPSILON:
                value = max(value, obstacle.max_y - box.min_y)
        else:
            if not (_overlap(box.min_x, box.max_x, obstacle.min_x, obstacle.max_x)
                    and _overlap(box.min_y, box.max_y, obstacle.min_y, obstacle.max_y)):
                continue
            if value > 0 and box.max_z <= obstacle.min_z + _EPSILON:
                value = min(value, obstacle.min_z - box.max_z)
            elif value < 0 and box.min_z >= obstacle.max_z - _EPSILON:
                value = max(value, obstacle.max_z - box.min_z)
    return 0.0 if abs(value) <= _EPSILON else value


def _resolve(body: Aabb, requested: tuple[float, float, float],
             obstacles: tuple[Aabb, ...]) -> tuple[float, float, float]:
    dx, dy, dz = requested
    dy = _clip_axis(body, obstacles, dy, 1)
    moved = body.moved(0, dy, 0)
    z_first = abs(dx) < abs(dz)
    if z_first:
        dz = _clip_axis(moved, obstacles, dz, 2)
        moved = moved.moved(0, 0, dz)
    dx = _clip_axis(moved, obstacles, dx, 0)
    moved = moved.moved(dx, 0, 0)
    if not z_first:
        dz = _clip_axis(moved, obstacles, dz, 2)
    return dx, dy, dz


def _resolve_with_step(body: Aabb, requested: tuple[float, float, float],
                       obstacles: tuple[Aabb, ...], step_height: float,
                       was_on_ground: bool, *,
                       base: tuple[float, float, float] | None = None
                       ) -> tuple[tuple[float, float, float], bool]:
    base = _resolve(body, requested, obstacles) if base is None else base
    horizontal_clipped = (not math.isclose(base[0], requested[0], abs_tol=_EPSILON)
                          or not math.isclose(base[2], requested[2], abs_tol=_EPSILON))
    downward_contact = (requested[1] < 0
                        and not math.isclose(base[1], requested[1], abs_tol=_EPSILON))
    if step_height <= 0 or not horizontal_clipped or not (was_on_ground or downward_contact):
        return base, False
    vertical_offset = base[1] if downward_contact else 0.0
    vertical_base = body.moved(0, vertical_offset, 0)
    heights = sorted({
        height
        for obstacle in obstacles
        for height in (
            obstacle.min_y - vertical_base.min_y,
            obstacle.max_y - vertical_base.min_y,
        )
        if _EPSILON < height <= step_height + _EPSILON
    })
    base_horizontal = base[0] * base[0] + base[2] * base[2]
    for height in heights:
        relative = _resolve(
            vertical_base, (requested[0], height, requested[2]), obstacles)
        candidate = (relative[0], vertical_offset + relative[1], relative[2])
        candidate_horizontal = relative[0] * relative[0] + relative[2] * relative[2]
        if candidate_horizontal > base_horizontal + _EPSILON:
            return candidate, True
    return base, False


def _toward_zero(value: float, amount: float) -> float:
    if abs(value) <= amount:
        return 0.0
    return value - math.copysign(amount, value)


def _adjust_for_sneaking(
        body: Aabb, requested: tuple[float, float, float],
        step_height: float, world: PhysicsWorldView,
        ) -> tuple[tuple[float, float, float] | None, tuple[tuple[int, int, int], ...],
                   tuple[tuple[int, int, int], ...], tuple[tuple[int, int, int], ...]]:
    """Apply vanilla's 0.05-block ledge clipping before ordinary collision."""
    dx, dy, dz = requested
    dependencies = set()
    missing = set()
    unsupported = set()

    def unsupported_offset(offset_x: float, offset_z: float) -> bool | None:
        probe = Aabb(
            body.min_x + offset_x,
            body.min_y - step_height - 1.0e-5,
            body.min_z + offset_z,
            body.max_x + offset_x,
            body.min_y,
            body.max_z + offset_z,
        )
        cells = required_cells_for_sweep(probe, (0.0, 0.0, 0.0))
        query = world.shapes(cells)
        dependencies.update(query.dependencies)
        missing.update(query.missing_cells)
        unsupported.update(query.unsupported_cells)
        if query.missing_cells or query.unsupported_cells:
            return None
        return not _intersects_any(probe, query.boxes)

    while abs(dx) > _EPSILON:
        empty = unsupported_offset(dx, 0.0)
        if empty is None:
            return None, tuple(sorted(dependencies)), tuple(sorted(missing)), tuple(sorted(unsupported))
        if not empty:
            break
        dx = _toward_zero(dx, 0.05)
    while abs(dz) > _EPSILON:
        empty = unsupported_offset(0.0, dz)
        if empty is None:
            return None, tuple(sorted(dependencies)), tuple(sorted(missing)), tuple(sorted(unsupported))
        if not empty:
            break
        dz = _toward_zero(dz, 0.05)
    while abs(dx) > _EPSILON and abs(dz) > _EPSILON:
        empty = unsupported_offset(dx, dz)
        if empty is None:
            return None, tuple(sorted(dependencies)), tuple(sorted(missing)), tuple(sorted(unsupported))
        if not empty:
            break
        dx = _toward_zero(dx, 0.05)
        dz = _toward_zero(dz, 0.05)
    return (dx, dy, dz), tuple(sorted(dependencies)), (), ()


def _has_near_ground_support(
        body: Aabb, step_height: float, world: PhysicsWorldView,
        ) -> tuple[bool | None, tuple[tuple[int, int, int], ...],
                   tuple[tuple[int, int, int], ...], tuple[tuple[int, int, int], ...]]:
    probe = Aabb(
        body.min_x, body.min_y - step_height - 1.0e-5, body.min_z,
        body.max_x, body.min_y, body.max_z,
    )
    cells = required_cells_for_sweep(probe, (0.0, 0.0, 0.0))
    query = world.shapes(cells)
    if query.missing_cells or query.unsupported_cells:
        return None, query.dependencies, query.missing_cells, query.unsupported_cells
    return _intersects_any(probe, query.boxes), query.dependencies, (), ()


def _boxes_touch_or_overlap(first: Aabb, second: Aabb) -> bool:
    overlaps = (
        first.max_x >= second.min_x - _EPSILON and first.min_x <= second.max_x + _EPSILON,
        first.max_y >= second.min_y - _EPSILON and first.min_y <= second.max_y + _EPSILON,
        first.max_z >= second.min_z - _EPSILON and first.min_z <= second.max_z + _EPSILON,
    )
    strict = (
        _overlap(first.min_x, first.max_x, second.min_x, second.max_x),
        _overlap(first.min_y, first.max_y, second.min_y, second.max_y),
        _overlap(first.min_z, first.max_z, second.min_z, second.max_z),
    )
    return all(overlaps) and sum(strict) >= 2


def _contacts_unmodeled_material(
        body: Aabb, applied: tuple[float, float, float],
        cells: tuple[tuple[int, int, int], ...], world: PhysicsWorldView) -> bool:
    final_body = body.moved(*applied)
    for position in cells:
        fact = world.cell(position)
        if fact.knowledge is not CellKnowledge.BLOCK or fact.block is None:
            continue
        block = fact.block
        if block.material_key in _ORDINARY_MATERIALS:
            continue
        boxes = block.world_boxes(position)
        if not boxes:
            x, y, z = position
            boxes = (Aabb(x, y, z, x + 1, y + 1, z + 1),)
        if any(_boxes_touch_or_overlap(final_body, obstacle) for obstacle in boxes):
            return True
    return False


def _intersects_any(body: Aabb, obstacles: tuple[Aabb, ...]) -> bool:
    return any(
        _overlap(body.min_x, body.max_x, obstacle.min_x, obstacle.max_x)
        and _overlap(body.min_y, body.max_y, obstacle.min_y, obstacle.max_y)
        and _overlap(body.min_z, body.max_z, obstacle.min_z, obstacle.max_z)
        for obstacle in obstacles
    )


def _pose_candidate_box(state: PhysicsState, height: float) -> Aabb:
    half = state.body_width / 2
    x, y, z = state.position
    return Aabb(x - half, y, z - half, x + half, y + height, z + half)


def _try_taller_pose(state: PhysicsState, world: PhysicsWorldView,
                     candidates: tuple[str, ...]):
    """Return the first collision-free taller pose, or the current pose."""
    dependencies = set()
    for pose in candidates:
        box = _pose_candidate_box(state, _POSE_HEIGHTS[pose])
        query = world.shapes(required_cells_for_sweep(box, (0.0, 0.0, 0.0)))
        dependencies.update(query.dependencies)
        if query.missing_cells:
            return None, tuple(sorted(dependencies)), query.missing_cells, ()
        if query.unsupported_cells:
            return None, tuple(sorted(dependencies)), (), query.unsupported_cells
        if not _intersects_any(box, query.boxes):
            return replace(state, pose=pose, body_height=_POSE_HEIGHTS[pose]), \
                tuple(sorted(dependencies)), (), ()
    return state, tuple(sorted(dependencies)), (), ()


def _has_effect(state: PhysicsState, *effect_ids: str) -> bool:
    wanted = set(effect_ids)
    return any(effect.effect_id in wanted for effect in state.status_effects)


def _sprint_mode(state: PhysicsState, tick_input: TickInput) -> tuple[bool, tuple[str, ...]]:
    events = []
    can_sprint = state.food_points > 6 or state.allow_flying
    has_forward = tick_input.forward > 1.0e-5
    can_start = (
        not state.sprinting and tick_input.forward >= 0.8 and can_sprint
        and not state.is_using_item
        and not _has_effect(state, "minecraft:blindness", "blindness")
        and not state.fall_flying
    )
    sprinting = state.sprinting
    if tick_input.sprint and can_start:
        sprinting = True
        events.append("sprint_started")
    elif tick_input.sprint and not sprinting:
        events.append("sprint_intent_refused")
    if sprinting and (
            not tick_input.sprint or not has_forward or not can_sprint
            or state.horizontal_collision):
        sprinting = False
        events.append("sprint_stopped")
    return sprinting, tuple(events)


def step(state: PhysicsState, tick_input: TickInput, world: PhysicsWorldView,
         ruleset: PhysicsRuleset) -> StepResult:
    if type(state) is not PhysicsState or type(tick_input) is not TickInput:
        return StepResult(CalculationStatus.INVALID_INPUT,
                          invalid_reasons=("typed_state_and_input_required",))
    identity_errors = _same_session(state, world, ruleset)
    if identity_errors:
        return StepResult(CalculationStatus.INVALID_INPUT, invalid_reasons=identity_errors)
    unsupported = []
    if state.pose not in _POSE_HEIGHTS: unsupported.append("pose_not_supported")
    if (state.swimming or state.submerged_in_water or state.climbing or state.fall_flying
            or state.flying):
        unsupported.append("nonordinary_movement_mode")
    supported_eligibility_effects = {"minecraft:blindness", "blindness"}
    if any(effect.effect_id not in supported_eligibility_effects for effect in state.status_effects):
        unsupported.append("status_effect_motion_rule_not_supported")
    if state.is_using_item:
        unsupported.append("using_item_input_mutation_not_modeled")
    if unsupported:
        return StepResult(CalculationStatus.UNSUPPORTED,
                          unsupported_reasons=tuple(sorted(set(unsupported))))

    dependencies = set()
    working = state
    pose_events = []
    taller_candidates = ()
    pose_event = None
    if state.pose == "standing" and state.sneaking:
        working = replace(state, pose="crouching", body_height=_POSE_HEIGHTS["crouching"])
        pose_events.append("crouch_entered")
    elif state.pose == "crouching" and not state.sneaking:
        taller_candidates, pose_event = ("standing",), "crouch_exited"
    elif (state.pose == "swimming" and not state.swimming
          and not state.submerged_in_water and not state.sneaking):
        taller_candidates, pose_event = ("standing", "crouching"), "crawl_exited"
    if taller_candidates:
        working, pose_dependencies, missing, unsupported_cells = _try_taller_pose(
            state, world, taller_candidates)
        dependencies.update(pose_dependencies)
        if missing:
            return StepResult(CalculationStatus.NEEDS_WORLD,
                              dependencies=tuple(sorted(dependencies)),
                              missing_cells=missing)
        if unsupported_cells:
            return StepResult(CalculationStatus.UNSUPPORTED,
                              dependencies=tuple(sorted(dependencies)),
                              unsupported_reasons=("pose_clearance_shape_not_supported",))
        if working.pose != state.pose:
            pose_events.append(pose_event)

    sprinting, sprint_events = _sprint_mode(working, tick_input)
    working = replace(working, sprinting=sprinting, sneaking=tick_input.sneak)
    cooldown = max(0, working.jumping_cooldown_ticks - 1)
    vx, vy, vz = working.velocity_blocks_per_tick
    # LivingEntity.tickMovement removes per-axis residuals below 0.003 before
    # travel. This is observable when a released key coasts to rest.
    vx = 0.0 if abs(vx) < 0.003 else vx
    vy = 0.0 if abs(vy) < 0.003 else vy
    vz = 0.0 if abs(vz) < 0.003 else vz
    events = list(pose_events + list(sprint_events))
    took_off = tick_input.jump and working.on_ground and cooldown == 0
    if took_off:
        vy = working.jump_strength_attribute
        cooldown = 10
        events.append("takeoff")
        if sprinting:
            vx -= math.sin(tick_input.movement_yaw_radians) * 0.2
            vz += math.cos(tick_input.movement_yaw_radians) * 0.2
    elif not tick_input.jump:
        cooldown = 0
    elif state.on_ground and cooldown > 0:
        events.append("jump_blocked_cooldown")

    slipperiness = _ORDINARY_SLIPPERINESS
    if working.on_ground:
        x, y, z = working.position
        surface_position = (math.floor(x), math.floor(y - 0.500001), math.floor(z))
        dependencies.add(surface_position)
        surface = world.cell(surface_position)
        if surface.knowledge is CellKnowledge.UNKNOWN:
            return StepResult(CalculationStatus.NEEDS_WORLD,
                              dependencies=tuple(sorted(dependencies)),
                              missing_cells=(surface_position,))
        # The center cell can already be air while the 0.6-wide body still
        # overlaps support at a ledge. Vanilla still uses the known center-cell
        # slipperiness lookup; on_ground is an observed contact fact, not proof
        # that the center cell itself is solid.
        if surface.knowledge is CellKnowledge.BLOCK:
            assert surface.block is not None
            if (surface.block.material_key not in _ORDINARY_MATERIALS
                    or surface.block.fluid or surface.block.collision_kind == "unsupported"):
                return StepResult(CalculationStatus.UNSUPPORTED,
                                  dependencies=tuple(sorted(dependencies)),
                                  unsupported_reasons=("surface_motion_rule_not_supported",))
        effective_speed = working.movement_speed_attribute * (
            _SPRINT_SPEED_MULTIPLIER if sprinting else 1.0)
        acceleration = (effective_speed * _GROUND_ACCELERATION_NUMERATOR
                        / (slipperiness * slipperiness * slipperiness))
    else:
        # LivingEntity uses its flying-speed field while airborne. For an
        # ordinary player this is 0.02, with the active sprint multiplier.
        acceleration = 0.02 * (_SPRINT_SPEED_MULTIPLIER if sprinting else 1.0)
    add_x, add_z = _input_velocity(
        tick_input.strafe, tick_input.forward, acceleration,
        tick_input.movement_yaw_radians,
    )
    vx += add_x
    vz += add_z
    requested = (vx, vy, vz)
    can_sneak_clip = False
    if working.sneaking and requested[1] <= 0.0:
        can_sneak_clip = working.on_ground
        if (not can_sneak_clip
                and working.fall_distance_blocks < working.step_height_blocks):
            has_support, support_dependencies, missing, unsupported_cells = \
                _has_near_ground_support(
                    working.body_box,
                    working.step_height_blocks - working.fall_distance_blocks,
                    world,
                )
            dependencies.update(support_dependencies)
            if missing:
                return StepResult(CalculationStatus.NEEDS_WORLD,
                                  dependencies=tuple(sorted(dependencies)),
                                  missing_cells=missing)
            if unsupported_cells:
                return StepResult(
                    CalculationStatus.UNSUPPORTED,
                    dependencies=tuple(sorted(dependencies)),
                    unsupported_reasons=("sneak_support_shape_not_supported",),
                )
            can_sneak_clip = bool(has_support)
    if can_sneak_clip:
        adjusted, edge_dependencies, missing, unsupported_cells = _adjust_for_sneaking(
            working.body_box, requested, working.step_height_blocks, world)
        dependencies.update(edge_dependencies)
        if missing:
            return StepResult(CalculationStatus.NEEDS_WORLD,
                              dependencies=tuple(sorted(dependencies)),
                              missing_cells=missing)
        if unsupported_cells:
            return StepResult(CalculationStatus.UNSUPPORTED,
                              dependencies=tuple(sorted(dependencies)),
                              unsupported_reasons=("sneak_support_shape_not_supported",))
        assert adjusted is not None
        if adjusted != requested:
            events.append("sneak_edge_clipped")
            requested = adjusted

    collision_cells = required_cells_for_sweep(working.body_box, requested)
    query = world.shapes(collision_cells)
    dependencies.update(query.dependencies)
    if query.missing_cells:
        return StepResult(CalculationStatus.NEEDS_WORLD,
                          dependencies=tuple(sorted(dependencies)),
                          missing_cells=query.missing_cells)
    if query.unsupported_cells:
        return StepResult(CalculationStatus.UNSUPPORTED,
                          dependencies=tuple(sorted(dependencies)),
                          unsupported_reasons=("collision_shape_not_supported",))
    base = _resolve(working.body_box, requested, query.boxes)
    horizontal_clipped = (
        not math.isclose(base[0], requested[0], abs_tol=_EPSILON)
        or not math.isclose(base[2], requested[2], abs_tol=_EPSILON)
    )
    downward_contact = (
        requested[1] < 0
        and not math.isclose(base[1], requested[1], abs_tol=_EPSILON)
    )
    step_query = query
    if (working.step_height_blocks > 0 and horizontal_clipped
            and (working.on_ground or downward_contact)):
        vertical_base = working.body_box.moved(
            0, base[1] if downward_contact else 0.0, 0)
        step_cells = required_cells_for_sweep(
            vertical_base,
            (requested[0], working.step_height_blocks, requested[2]),
        )
        combined_cells = tuple(sorted(set(collision_cells) | set(step_cells)))
        step_query = world.shapes(combined_cells)
        dependencies.update(step_query.dependencies)
        if step_query.missing_cells:
            return StepResult(CalculationStatus.NEEDS_WORLD,
                              dependencies=tuple(sorted(dependencies)),
                              missing_cells=step_query.missing_cells)
        if step_query.unsupported_cells:
            return StepResult(CalculationStatus.UNSUPPORTED,
                              dependencies=tuple(sorted(dependencies)),
                              unsupported_reasons=("collision_shape_not_supported",))
    applied, stepped = _resolve_with_step(
        working.body_box, requested, step_query.boxes,
        working.step_height_blocks, working.on_ground, base=base)
    if _contacts_unmodeled_material(
            working.body_box, applied, step_query.dependencies, world):
        return StepResult(
            CalculationStatus.UNSUPPORTED,
            dependencies=tuple(sorted(dependencies)),
            unsupported_reasons=("contact_material_motion_rule_not_supported",),
        )
    horizontal_collision = (not math.isclose(requested[0], applied[0], abs_tol=_EPSILON)
                            or not math.isclose(requested[2], applied[2], abs_tol=_EPSILON))
    vertical_collision = not math.isclose(requested[1], applied[1], abs_tol=_EPSILON)
    on_ground = vertical_collision and requested[1] < 0
    if horizontal_collision: events.append("horizontal_collision")
    if vertical_collision: events.append("vertical_collision")
    if stepped: events.append("step_up")
    if working.on_ground and not on_ground: events.append("left_ground")
    if not working.on_ground and on_ground: events.append("landed")
    moved_vx = 0.0 if not math.isclose(requested[0], applied[0], abs_tol=_EPSILON) else vx
    moved_vz = 0.0 if not math.isclose(requested[2], applied[2], abs_tol=_EPSILON) else vz
    moved_vy = 0.0 if vertical_collision else vy
    retention = slipperiness * _AIR_RETENTION if working.on_ground else _AIR_RETENTION
    next_velocity = (
        moved_vx * retention,
        (moved_vy - working.gravity_attribute) * _VERTICAL_RETENTION,
        moved_vz * retention,
    )
    next_position = tuple(a + b for a, b in zip(working.position, applied))
    fall_distance = 0.0 if on_ground else (
        working.fall_distance_blocks + max(0.0, -applied[1])
    )
    next_state = replace(
        working, movement_tick_id=working.movement_tick_id + 1,
        position=next_position, velocity_blocks_per_tick=next_velocity,
        yaw_radians=tick_input.movement_yaw_radians,
        on_ground=on_ground, horizontal_collision=horizontal_collision,
        vertical_collision=vertical_collision,
        sprinting=sprinting, sneaking=tick_input.sneak,
        jumping_cooldown_ticks=cooldown, fall_distance_blocks=fall_distance,
    )
    segment = MotionSegment("step_collision_resolution" if stepped else "axis_collision_resolution",
                            requested, applied)
    exhaustion = 0.2 if took_off and sprinting else 0.05 if took_off else 0.0
    resources = ResourceUpdate(
        ResourceStatus.CONDITIONAL, exhaustion,
        ("server_hunger_clock_not_in_physics_state",),
    )
    return StepResult(
        status=CalculationStatus.OK, next_state=next_state,
        events=tuple(events), segments=(segment,),
        dependencies=tuple(sorted(dependencies)), resource_update=resources,
    )
