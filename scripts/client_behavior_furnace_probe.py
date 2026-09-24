"""Functional furnace synchronization evidence using only normal client operations.

Called inside the bounded, supervised container probe. The dense superflat preset
is a world-creation fixture, not a representative timing/performance workload.
"""
from __future__ import annotations

import time

from mc2p.contracts.action_v1 import ClickSlotV1, CloseScreenV1, InteractBlockV1
from mc2p.contracts.common import ContractViolation, FieldStatusV0
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.runtime.trace import trace_projection
from mc2p.skills.targeting import confirmed_block_target
from scripts.client_behavior_container_probe import INTERACTION


def furnace_operation_from_target(observation: ObservationSnapshotV3, *, now_ns: int) -> InteractBlockV1:
    if type(observation) is not ObservationSnapshotV3:
        raise ContractViolation("furnace targeting requires exact ObservationSnapshotV3")
    target = observation.targeting.value
    if (observation.targeting.status is not FieldStatusV0.VALID or target is None
            or target.hit_kind != "block"
            or not confirmed_block_target(observation, block_position=target.block_position, face=target.face,
                now_ns=now_ns, controller_clock_id=observation.controller_clock_id)):
        raise RuntimeError("fresh targeting query does not identify a furnace")
    block = next((value for value in observation.perception.value.blocks
                  if value.position == target.block_position), None)
    if block is None or block.block_id != "minecraft:furnace":
        raise RuntimeError("targeted block is not an observed furnace")
    return InteractBlockV1(*target.block_position, target.face)


def evaluate_furnace(frames: list[dict], opened: dict, reopened: dict,
                     output: dict, confirmed_output: dict) -> list[dict]:
    ticks = [r["world_tick"] for r in frames]
    values = [dict(r["properties"]) for r in frames]
    valid = bool(frames) and all(r["properties_status"] == "valid" and
        [p[0] for p in r["properties"]] == [0, 1, 2, 3] for r in frames)
    # Pinned 1.21 AbstractFurnaceBlockEntity.createFuelTimeMap: Items.STICK = 100 ticks.
    progress = valid and any(0 < b[2] < b[3] and a[2] < b[2] and 0 < b[0] < a[0] and b[1] == 100
                             for a, b in zip(values, values[1:]))
    checks = {
        "four_whitelisted_furnace_properties": valid,
        "furnace_samples_have_advancing_world_time": len(ticks) >= 3 and all(b > a for a, b in zip(ticks, ticks[1:])),
        "server_synchronized_burn_and_cook_progress": progress,
        "charcoal_produced_and_reopened": not output["empty"] and output["item_id"] == "minecraft:charcoal"
            and output["count"] == 1 and output == confirmed_output,
        "furnace_reopened_in_new_server_handler": opened["gui_session_id"] != reopened["gui_session_id"]
            and opened["sync_id"] != reopened["sync_id"],
    }
    return [{"name": name, "passed": passed} for name, passed in checks.items()]


def run_furnace_extension(*, step, look_at, get_observation, chest_operation, milestone) -> list[dict]:
    def gui():
        return get_observation().gui.value

    def wait_handler(kind):
        for _ in range(40):
            if gui().open and gui().screen_kind == kind:
                return
            step("await_" + kind)
        raise TimeoutError("normal server handler did not open: " + kind)

    def click(slot_id, button=0, mode="pickup"):
        g = gui()
        receipt = step("furnace_material_slot", operation=ClickSlotV1(
            g.gui_session_id, g.sync_id, g.revision, slot_id, button, mode))
        if (receipt["status"], receipt["reason"]) != ("pending_confirmation", "slot_click_sent"):
            raise RuntimeError("normal slot dispatch failed: " + str(receipt))
        for _ in range(3):
            step("await_material_sync")

    def is_log(item):
        return not item.empty and item.item_id in {"minecraft:oak_log", "minecraft:birch_log",
            "minecraft:spruce_log", "minecraft:jungle_log", "minecraft:acacia_log", "minecraft:dark_oak_log"}

    for _ in range(6): step("furnace_chest_cooldown")
    step("confirm_furnace_chest_target", observation_request=INTERACTION)
    if not confirmed_block_target(get_observation(),
            block_position=(chest_operation.block_x, chest_operation.block_y, chest_operation.block_z),
            face=chest_operation.face, now_ns=time.perf_counter_ns(),
            controller_clock_id=get_observation().controller_clock_id):
        raise RuntimeError("fresh targeting query did not confirm material chest")
    step("reopen_chest_for_furnace", operation=chest_operation, observation_request=INTERACTION)
    wait_handler("generic_container")
    # All choices come from this open handler. Never assume the seed guarantees loot.
    for _ in range(27):
        player = [s for s in gui().slots if s.source_kind != "container"]
        have_log = any(is_log(s.item) for s in player)
        sticks = sum(s.item.count for s in player if s.item.item_id == "minecraft:stick")
        if have_log and sticks >= 4:
            break
        source = next((s for s in gui().slots if s.source_kind == "container" and (
            (not have_log and is_log(s.item)) or (sticks < 4 and s.item.item_id == "minecraft:stick"))), None)
        if source is None:
            raise RuntimeError("fixture lacks an observed log and four sticks; no privileged replenishment")
        click(source.slot_id, mode="quick_move")
    else:
        raise TimeoutError("material acquisition budget exhausted")
    milestone("furnace_materials_acquired")
    step("close_material_chest", operation=CloseScreenV1())
    for _ in range(6): step("material_close_cooldown")

    operation = None
    for yaw in range(0, 360, 45):
        look_at(yaw, 65, "aim_visible_furnace", interaction=True)
        try:
            operation = furnace_operation_from_target(get_observation(),
                                                       now_ns=time.perf_counter_ns())
            break
        except RuntimeError:
            pass
    if operation is None:
        raise RuntimeError("no fresh targeting query reaches a fixture furnace")
    receipt = step("open_observed_furnace", operation=operation, observation_request=INTERACTION)
    if (receipt["status"], receipt["reason"]) != ("pending_confirmation", "block_use_dispatched"):
        raise RuntimeError("normal furnace dispatch failed: " + str(receipt))
    wait_handler("furnace")
    for _ in range(3): step("await_empty_furnace_sync")
    milestone("furnace_opened_empty")
    opened = trace_projection(gui())
    if any(not s.item.empty for s in gui().slots if s.slot_id in (0, 1, 2)):
        raise RuntimeError("freshly generated furnace was not empty")

    samples = []
    def sample():
        o = get_observation()
        samples.append({"world_tick": o.world_time_ticks.value,
                        "properties_status": o.gui.value.properties_status,
                        "properties": trace_projection(o.gui.value.properties)})

    sample()
    source = next(s for s in gui().slots if s.source_kind in ("player_main", "player_hotbar") and is_log(s.item))
    source_id = source.slot_id
    click(source_id)
    click(0, button=1)  # Exactly one input log; retain normal cursor-stack behavior.
    if not gui().cursor_stack.empty:
        click(source_id)
    fuel = next((s for s in gui().slots if s.source_kind in ("player_main", "player_hotbar")
                 and s.item.item_id == "minecraft:stick" and s.item.count >= 4), None)
    if fuel is None:
        raise RuntimeError("normal inventory merging did not provide four sticks")
    click(fuel.slot_id)
    click(1)
    milestone("furnace_loaded")
    for _ in range(300):
        sample()
        output = next(s.item for s in gui().slots if s.slot_id == 2)
        if output.item_id == "minecraft:charcoal" and output.count == 1:
            break
        step("await_normal_smelting")
    else:
        raise TimeoutError("no charcoal output within 300 reference actions")
    milestone("furnace_output")
    output = trace_projection(output)
    step("close_after_smelting", operation=CloseScreenV1())
    closed = trace_projection(gui())
    for _ in range(6): step("await_furnace_close")
    step("confirm_smelted_furnace_target", observation_request=INTERACTION)
    if furnace_operation_from_target(get_observation(), now_ns=time.perf_counter_ns()) != operation:
        raise RuntimeError("fresh targeting query changed the furnace before reopen")
    step("reopen_smelted_furnace", operation=operation, observation_request=INTERACTION)
    wait_handler("furnace")
    for _ in range(6): step("await_reopened_furnace_sync")
    milestone("furnace_reopened")
    reopened = trace_projection(gui())
    confirmed = trace_projection(next(s.item for s in gui().slots if s.slot_id == 2))
    checks = evaluate_furnace(samples, opened, reopened, output, confirmed)
    checks.append({"name": "closed_furnace_clears_public_properties_and_slots", "passed":
        not closed["open"] and closed["properties"] == [] and closed["slots"] == []
        and closed["properties_status"] == "valid" and closed["properties_reason_code"] is None})
    step("final_furnace_close", operation=CloseScreenV1())
    checks.append({"name": "furnace_final_close_and_empty_cursor", "passed": not gui().open and gui().cursor_stack.empty})
    return checks
