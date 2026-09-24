"""One bounded sensor scene for both backends; coordinates belong only to the evaluator."""
from collections.abc import Callable
import math
from pathlib import Path
from mc2p.contracts.action_v1 import LookV1, MovementV1, InteractBlockV1, ClickSlotV1, CloseScreenV1, SelectHotbarV1
from mc2p.runtime.trace import trace_projection
from scripts.control_probe_core import write_json_atomic


def await_fixture_front(read_observation: Callable[[], dict], step: Callable[[str], object],
                        *, max_steps: int = 40) -> dict:
    """Wait at the fixed off-target pose; any observed cow leak fails immediately."""
    current = read_observation()
    episode, start = current["episode_id"], current["sequence_id"]
    for neutral_steps in range(max_steps + 1):
        if current["episode_id"] != episode or current["sequence_id"] != start + neutral_steps:
            raise ValueError("fixture readiness observation episode/sequence discontinuity")
        entities = current["perception"]["value"]["visible_entities"]
        for entity in entities:
            if entity["entity_type"] == "minecraft:cow" and (entity["display_name"] is not None or entity["equipment"]):
                raise ValueError(f"fixture off-target cow information leak at sequence {current['sequence_id']}")
        kinds = [e["entity_type"] for e in entities]
        if kinds.count("minecraft:armor_stand") == kinds.count("minecraft:cow") == 1:
            return {"start_sequence": start, "ready_sequence": current["sequence_id"], "neutral_steps": neutral_steps}
        if neutral_steps < max_steps:
            step("await_fixture_front")
            current = read_observation()
    raise TimeoutError(f"fixture front entities not observed after {max_steps} neutral steps")


def run_visibility_scenario(read_observation, step, milestone, run_dir: Path) -> list[dict]:
    checks=[]
    def orient(yaw, pitch, *, label="aim", movement=MovementV1(), field_profile="navigation_v1"):
        step(label, look=LookV1((yaw - read_observation().yaw_degrees.value + 180) % 360 - 180,
                               pitch - read_observation().pitch_degrees.value), movement=movement,
                               field_profile=field_profile)

    def aim(x, y, z):
        p = read_observation().position.value
        vx, vz = x - p.x, z - p.z
        orient(math.degrees(math.atan2(-vx, vz)), math.degrees(math.atan2(p.y + 1.62 - y, math.hypot(vx, vz))),
               field_profile="interaction_v1")

    def move_to(x, z):
        for _ in range(400):
            p = read_observation().position.value
            vx, vz = x - p.x, z - p.z
            distance = math.hypot(vx, vz)
            if distance < .08: break
            orient(math.degrees(math.atan2(-vx, vz)), 0, label="fixture_waypoint_move",
                   movement=MovementV1(forward=1, sneak=distance < .8))
        else:
            raise TimeoutError(f"normal movement did not reach fixture waypoint {x}, {z}")
        for _ in range(8): step("waypoint_release")
        p = read_observation().position.value
        if math.hypot(x - p.x, z - p.z) > .14 or abs(p.y + 60) > .01:
            raise RuntimeError(f"fixture waypoint pose invalid: {p}")

    def await_condition(label, condition):
        for _ in range(40):
            if condition(): return
            step(label)
        raise TimeoutError(f"missing subsequent observation confirmation: {label}")

    for _ in range(12): step("initial_settle")
    p = read_observation().position.value
    if math.hypot(p.x - .5, p.z - .5) > .1 or abs(p.y + 60) > .01:
        raise RuntimeError(f"existing fixture did not load its declared spawn: {p}")
    aim(.5, -59, 6.5)
    readiness = await_fixture_front(lambda: trace_projection(read_observation()), step)
    write_json_atomic(run_dir / "fixture-readiness.json", readiness)
    milestone("front")
    orient(read_observation().yaw_degrees.value + 180, 0)
    milestone("turned_away")
    aim(.5, -59, 6.5)
    milestone("turned_back")
    for x, label in ((2.5, "partial_occlusion"), (4.5, "full_occlusion"), (.5, "occlusion_return")):
        move_to(x, .5)
        aim(.5, -59, 6.5)
        milestone(label)
    move_to(.5, -27.5)
    aim(.5, -59, 6.5)
    milestone("out_of_range")
    move_to(.5, .5)
    aim(.5, -59, 6.5)
    milestone("range_return")
    move_to(-4.5, 3.8)
    aim(-4.5, -59.3, 6.5)
    milestone("cow_targeted")
    orient(read_observation().yaw_degrees.value + 20, read_observation().pitch_degrees.value,
           field_profile="interaction_v1")
    milestone("cow_off_target")
    move_to(.5, 1.7)  # Remain clear of the chest's collision box.
    aim(-1.5, -59.55, .5)
    target = read_observation().targeting.value
    if target is None or target.hit_kind != "block" or target.block_position != (-2, -60, 0):
        raise RuntimeError("independent targeting did not find fixture chest")
    step("open_chest", operation=InteractBlockV1(-2, -60, 0, target.face),
         field_profile="interaction_v1")
    await_condition("await_chest", lambda: read_observation().gui.value.open and read_observation().gui.value.screen_kind == "generic_container")
    g = read_observation().gui.value
    source = next(s for s in g.slots if s.source_kind == "container" and s.item.item_id == "minecraft:stone" and s.item.count == 16)
    step("stone_to_hotbar", operation=ClickSlotV1(g.gui_session_id, g.sync_id, g.revision, source.slot_id, 0, "swap"))
    await_condition("await_stone_sync", lambda: read_observation().inventory.value.main[0].item_id == "minecraft:stone"
                    and read_observation().inventory.value.main[0].count == 16)
    step("close_chest", operation=CloseScreenV1())
    step("select_stone", operation=SelectHotbarV1(0))
    for _ in range(6): step("await_use_cooldown")
    aim(-1.5, -60, 3.5)
    target = read_observation().targeting.value
    target_block = None if target is None or target.block_position is None else next(
        (block for block in read_observation().perception.value.blocks if block.position == target.block_position), None)
    if (target is None or target.hit_kind != "block" or target.block_position != (-2, -61, 3)
            or target.face != "up" or target_block is None or target_block.block_id != "minecraft:diamond_ore"):
        raise RuntimeError("independent targeting did not find exposed ore top")
    milestone("ore_exposed")
    step("cover_ore", operation=InteractBlockV1(-2, -61, 3, "up"), field_profile="interaction_v1")
    for _ in range(12): step("await_placement_sync")
    step("observe_placed_cover", field_profile="interaction_v1")
    target = read_observation().targeting.value
    cover = None if target is None or target.block_position is None else next(
        (block for block in read_observation().perception.value.blocks if block.position == target.block_position), None)
    checks.append({"name": "normal_placement_observed_at_cover_cell_and_inventory_decremented", "passed":
        target is not None and target.hit_kind == "block" and target.block_position == (-2, -60, 3)
        and cover is not None and cover.block_id == "minecraft:stone"
        and read_observation().inventory.value.main_hand.item_id == "minecraft:stone" and read_observation().inventory.value.main_hand.count == 15})
    milestone("ore_covered")
    step("final_neutral")
    milestone("final_neutral")
    return checks
