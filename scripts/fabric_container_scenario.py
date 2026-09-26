"""Runtime-only nonempty container probe; fixture contents are never an actor input."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import math
from pathlib import Path
import time

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, MovementV1, LookV1, InteractBlockV1, ClickSlotV1, CloseScreenV1, SelectHotbarV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation, FieldStatusV0, require_identifier
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.contracts.task import TaskIntentV0, SuccessCriterionV0, ComparisonOperatorV0
from mc2p.skills.targeting import confirmed_block_target
from scripts.control_probe_core import append_jsonl

LABELS = ("start", "seen_chest", "aimed", "wrong_target", "open_dispatched", "opened",
    "picked_up", "placed_one", "returned_remainder", "closed", "reopen_dispatched", "reopened",
    "stale_rejected", "recovered", "hotbar", "cancelled")
NAVIGATION = ObservationRequestV3()
INTERACTION = ObservationRequestV3("interaction_v1")


def nearest_observed_block(observation: ObservationSnapshotV3, block_id: str):
    if type(observation) is not ObservationSnapshotV3:
        raise ContractViolation(
            "container discovery requires exact ObservationSnapshotV3",
        )
    require_identifier(block_id, "container block id")
    if (observation.privileged_fields_present
            or observation.perception.status is not FieldStatusV0.VALID
            or observation.perception.value is None
            or observation.position.value is None):
        raise ContractViolation(
            "container discovery requires available non-privileged V3 state",
        )
    position = observation.position.value
    candidates = [
        block for block in observation.perception.value.blocks
        if block.block_id == block_id
    ]

    def distance_squared(block):
        dx = block.position[0] + .5 - position.x
        dy = block.position[1] + .5 - position.y
        dz = block.position[2] + .5 - position.z
        return dx * dx + dy * dy + dz * dz

    return min(candidates, default=None, key=distance_squared)


def _counts(gui: dict, *, container: bool) -> Counter:
    return sum((
        Counter({slot["item"]["item_id"]: slot["item"]["count"]})
        for slot in gui["slots"]
        if (slot["source_kind"] == "container") == container
        and not slot["item"]["empty"]
    ), Counter())


def evaluate_transfer(
    before: dict, after: dict, reopened: dict, moved: dict,
) -> list[dict]:
    delta = Counter({moved["item_id"]: moved["count"]}) \
        if not moved["empty"] else Counter()
    values = {
        "nonempty_stack_selected": bool(delta) and moved["count"] > 0,
        "ordinary_container_handlers": all(
            gui["open"] and gui["screen_kind"] == "generic_container"
            and gui["sync_id"] > 0
            for gui in (before, after, reopened)
        ),
        "stack_removed_from_container": _counts(before, container=True)
            == _counts(after, container=True) + delta,
        "stack_added_to_player": _counts(after, container=False)
            == _counts(before, container=False) + delta,
        "no_cursor_item": all(
            gui["cursor_stack"]["empty"]
            for gui in (before, after, reopened)
        ),
        "new_server_handler_after_reopen":
            before["gui_session_id"] != reopened["gui_session_id"]
            and before["sync_id"] != reopened["sync_id"],
        "reopened_server_contents_match_transfer":
            after["slots"] == reopened["slots"],
    }
    return [
        {"name": name, "passed": passed}
        for name, passed in values.items()
    ]


def evaluate_container_reconnect(first_stages: dict, first: list[dict], second_stages: dict, second: list[dict]) -> list[dict]:
    try:
        def sample(records, sequence):
            observations = [r["payload"]["result"]["observation"] if r["record_type"] == "reset"
                else r["payload"]["backend_result"]["observation"] for r in records if r["record_type"] in {"reset", "step"}]
            matches = [o for o in observations if o["sequence_id"] == sequence]
            if len(matches) != 1: raise ValueError("missing or duplicate reconnect sample")
            value = matches[0]
            if (value.get("schema_version") != "mc2p.observation.v3"
                    or value["perception"]["value"].get("knowledge_model") != "block_state_v1"
                    or "block_rays" in value["perception"]["value"]):
                raise ValueError("container reconnect requires native Observation V3 blocks")
            return value
        def chest(obs):
            return [(s["source_index"], s["item"]) for s in obs["gui"]["value"]["slots"] if s["source_kind"] == "container"]
        before = sample(first, first_stages["cancelled"])["inventory"]["value"]
        after = sample(second, second_stages["start"])["inventory"]["value"]
        prior_chest = chest(sample(first, first_stages["reopened"]))
        next_chest = chest(sample(second, second_stages["opened"]))
        old_gui = sample(first, first_stages["opened"])["gui"]["value"]
        current_gui = sample(second, second_stages["opened"])["gui"]["value"]
        stale_steps = [r["payload"] for r in second if r["record_type"] == "step"
            and r["payload"]["backend_result"]["observation"]["sequence_id"] == second_stages["stale_rejected"]]
        if len(stale_steps) != 1: raise ValueError("missing or duplicate cross-JVM request")
        stale = stale_steps[0]["decision"]["action"]["operation"]
        return [dict(name="reconnect_preserves_full_nonempty_inventory", passed=before == after
                    and any(not item["empty"] for item in before["main"])),
                dict(name="reconnect_preserves_server_chest_contents", passed=bool(prior_chest)
                    and any(not item["empty"] for _, item in prior_chest) and prior_chest == next_chest),
                dict(name="rejected_reference_is_from_previous_jvm", passed=stale["kind"] == "click_slot"
                    and old_gui["gui_session_id"] != current_gui["gui_session_id"]
                    and stale["gui_session_id"] == old_gui["gui_session_id"]
                    and stale["sync_id"] == old_gui["sync_id"] and stale["expected_revision"] == old_gui["revision"])]
    except (KeyError, TypeError, ValueError, IndexError) as error:
        return [dict(name="well_formed_container_reconnect", passed=False, detail=str(error))]


def evaluate_container(stages: dict, records: list[dict]) -> list[dict]:
    """Stage values are only sequence references; all claimed state comes from the Runtime trace."""
    try:
        if set(stages) != set(LABELS) or any(type(i) is not int or i < 0 for i in stages.values()):
            raise ValueError("missing or invalid container stage reference")
        samples, steps = {}, {}
        for record in records:
            if record["record_type"] == "reset":
                obs = record["payload"]["result"]["observation"]
            elif record["record_type"] == "step":
                obs = record["payload"]["backend_result"]["observation"]
                steps[obs["sequence_id"]] = record["payload"]
            else:
                continue
            if obs["sequence_id"] in samples: raise ValueError("duplicate trace sample")
            if (obs.get("schema_version") != "mc2p.observation.v3"
                    or obs["perception"]["value"].get("knowledge_model") != "block_state_v1"
                    or "block_rays" in obs["perception"]["value"]):
                raise ValueError("container evidence requires native Observation V3 blocks")
            samples[obs["sequence_id"]] = obs
        o = {name: samples[stages[name]] for name in LABELS}
        g = {name: obs["gui"]["value"] for name, obs in o.items()}
        def step(name): return steps[stages[name]]
        def op(name): return step(name)["decision"]["action"]["operation"]
        def receipt(name, status, reason):
            r = step(name)["backend_result"]["receipt"]
            return r["status"] == status and r["reason"] == reason
        def items(obs):
            return sum((Counter({x["item_id"]: x["count"]}) for x in obs["inventory"]["value"]["main"] if not x["empty"]), Counter())
        source = next(s for s in g["opened"]["slots"] if s["source_kind"] == "container" and not s["item"]["empty"])
        moved = {**source["item"], "count": 1}
        aimed = o["aimed"]
        target_group = aimed["targeting"]
        target_state = target_group["value"]
        if (aimed["field_profile"] != "interaction_v1" or target_group["status"] != "valid"
                or target_state["hit_kind"] != "block"):
            raise ValueError("aimed stage lacks current block targeting")
        target = target_state["block_position"]
        matching = [block for block in aimed["perception"]["value"]["blocks"]
                    if block["position"] == target]
        if len(matching) != 1 or "current_target" not in matching[0]["sources"]:
            raise ValueError("aimed target lacks current block knowledge")
        cancelled_action = step("cancelled")["decision"]["action"]
        cancelled_report = step("cancelled")["report"]
        cancelled_self = o["cancelled"]["self_state"]["value"]
        checks = dict(
            ordered_trace_stages=stages["start"] == 0 and stages["cancelled"] == max(samples)
                and all(stages[a] <= stages[b] for a, b in zip(LABELS, LABELS[1:]))
                and stages["opened"] < stages["closed"] < stages["reopened"] < stages["recovered"],
            observed_current_target_only=matching[0]["block_id"] == "minecraft:chest" and all(
                op(name)["kind"] == "interact_block" and [op(name)["block_" + axis] for axis in ("x", "y", "z")] == target
                and op(name)["face"] == target_state["face"] for name in ("open_dispatched", "reopen_dispatched")),
            wrong_target_has_no_gui_effect=receipt("wrong_target", "rejected", "target_mismatch")
                and g["wrong_target"] == g["aimed"] and step("wrong_target")["report"]["status"] == "failed",
            dispatch_is_not_server_confirmation=all(receipt(name, "pending_confirmation", reason)
                and step(name)["report"]["status"] == "running" for name, reason in (
                    ("open_dispatched", "block_use_dispatched"), ("reopen_dispatched", "block_use_dispatched"),
                    ("picked_up", "slot_click_sent"), ("placed_one", "slot_click_sent"), ("returned_remainder", "slot_click_sent"))),
            normal_pickup_split_return=[(op(n)["kind"], op(n)["click_type"], op(n)["button"]) for n in
                ("picked_up", "placed_one", "returned_remainder")] == [("click_slot", "pickup", 0), ("click_slot", "pickup", 1), ("click_slot", "pickup", 0)]
                and g["picked_up"]["cursor_stack"] == source["item"]
                and g["placed_one"]["cursor_stack"] == {**source["item"], "count": source["item"]["count"] - 1},
            self_inventory_matches_single_transfer=items(o["returned_remainder"]) == items(o["opened"]) + Counter({moved["item_id"]: 1})
                and items(o["cancelled"]) == items(o["returned_remainder"]),
            stale_reference_rejected_without_transfer=receipt("stale_rejected", "rejected", "stale_gui_session")
                and g["stale_rejected"] == g["reopened"] and step("stale_rejected")["report"]["status"] == "failed",
            closed_state_clears_container=all(not g[n]["open"] and not g[n]["slots"] and not g[n]["properties"]
                and g[n]["gui_session_id"] is None and g[n]["cursor_stack"]["empty"] for n in ("start", "closed", "recovered", "cancelled"))
                and all(op(n) == {"kind": "close_screen"} and receipt(n, "confirmed_local", "closed_normally")
                    for n in ("closed", "recovered")),
            hotbar_and_cancel=op("hotbar") == {"kind": "select_hotbar", "slot": 2}
                and o["cancelled"]["inventory"]["value"]["selected_hotbar_slot"] == 2
                and op("cancelled") is None and cancelled_report["status"] == "cancelled"
                and cancelled_report["phase"] == "cancel" and cancelled_report["failure"]["code"] == "cancelled"
                and cancelled_report["failure"]["source"] == "runtime" and receipt("cancelled", "executed", "neutral")
                and set(cancelled_action["movement"]) == {"forward", "strafe", "jump", "sneak", "sprint"}
                and set(cancelled_action["look"]) == {"yaw_delta_degrees", "pitch_delta_degrees"}
                and MovementV1(**cancelled_action["movement"]) == MovementV1()
                and LookV1(**cancelled_action["look"]) == LookV1()
                and math.hypot(cancelled_self["velocity"]["x"], cancelled_self["velocity"]["z"]) < .01
                and cancelled_self["is_on_ground"] is True and cancelled_self["is_using_item"] is False
                and cancelled_self["active_hand"] is None and cancelled_self["item_use_ticks_remaining"] == 0,
        )
        return [{"name": name, "passed": bool(value)} for name, value in checks.items()] + evaluate_transfer(
            g["opened"], g["returned_remainder"], g["reopened"], moved)
    except (KeyError, TypeError, ValueError, IndexError, StopIteration) as error:
        return [dict(name="well_formed_container_evidence", passed=False, detail=str(error))]


def run_container_scenario(runtime, backend, episode: str, directory: Path, deadline: int, previous_gui=None):
    task = TaskIntentV0("container-probe", "normal-container-transfer", "{}",
        (SuccessCriterionV0("inventory_delta", ComparisonOperatorV0.GREATER_THAN, 0, "items"),),
        600, deadline, True, 0.0)
    profile, stages, rows = BehaviorProfileV0(), {}, []
    def obs(): return runtime.observation
    def gui(): return obs().gui.value
    def diagnostic():
        row = dict(episode_id=episode, observation_sequence_id=obs().sequence_id, diagnostics=backend.last_diagnostics)
        rows.append(row); append_jsonl(directory / "diagnostics.jsonl", row)
    def mark(label):
        stages[label] = obs().sequence_id
        append_jsonl(directory / "container-stages.jsonl", dict(label=label, sequence=obs().sequence_id))
        print(f"FABRIC_CONTAINER_STAGE={episode}:{label}", flush=True)
    def step(*, operation=None, look=None, expected="running", observation_request=NAVIGATION):
        if obs().sequence_id >= 600: raise TimeoutError("container action budget exhausted")
        if operation is not None or look is not None:
            runtime.submit_intent(ActionIntentV1(f"container-{obs().sequence_id}", "container", episode, obs().sequence_id,
                ActionPriorityV0.TASK, time.perf_counter_ns(), deadline, operation=operation, look=look))
        result = runtime.step(task, profile, min(deadline, time.perf_counter_ns() + 5_000_000_000),
                              observation_request=observation_request)
        if result.observation is None or result.report.status.value != expected:
            raise RuntimeError(f"unexpected container Runtime result: {result.report}")
        diagnostic()
        return result
    def settle():
        for _ in range(5): step()
    def await_nonempty():
        for _ in range(40):
            if gui().open and gui().screen_kind == "generic_container" and any(
                    s.source_kind == "container" and not s.item.empty for s in gui().slots): return
            step()
        raise TimeoutError("server did not synchronize a nonempty normal chest")
    def click(slot, button):
        g = gui()
        return ClickSlotV1(g.gui_session_id, g.sync_id, g.revision, slot, button, "pickup")
    diagnostic(); mark("start")
    found = None
    for _ in range(48):
        found = nearest_observed_block(obs(), "minecraft:chest")
        if found is not None: break
        step(look=LookV1(7.5, 25 - obs().pitch_degrees.value), observation_request=NAVIGATION)
    if found is None: raise TimeoutError("no chest found by bounded legal scan")
    mark("seen_chest")
    p = obs().position.value
    target = found.position
    dx, dz = target[0] + .5 - p.x, target[2] + .5 - p.z
    if math.hypot(dx, dz) > 3.5 or obs().self_state.value.pose != "standing":
        raise RuntimeError("observed fixture chest is not within the standing interaction slice")
    yaw = math.degrees(math.atan2(-dx, dz))
    pitch = math.degrees(math.atan2(p.y + 1.62 - (target[1] + .45), math.hypot(dx, dz)))
    step(look=LookV1((yaw - obs().yaw_degrees.value + 180) % 360 - 180,
                     pitch - obs().pitch_degrees.value), observation_request=INTERACTION)
    mark("aimed")
    targeted = obs().targeting.value
    current_chest = nearest_observed_block(obs(), "minecraft:chest")
    if (targeted is None or current_chest is None or current_chest.position != target
            or not confirmed_block_target(obs(), block_position=target, face=targeted.face,
                now_ns=time.perf_counter_ns(), controller_clock_id=obs().controller_clock_id)):
        raise RuntimeError("fresh targeting query missed observed chest")
    operation = InteractBlockV1(*target, targeted.face)
    step(operation=replace(operation, block_x=operation.block_x + 1), expected="failed",
         observation_request=INTERACTION); mark("wrong_target")
    step(operation=operation, observation_request=INTERACTION); mark("open_dispatched")
    await_nonempty(); mark("opened")
    old_gui = gui()
    source = next(s for s in old_gui.slots if s.source_kind == "container" and not s.item.empty and s.item.count > 1)
    destination = next(s for s in old_gui.slots if s.source_kind == "player_hotbar" and s.source_index == 2
        and (s.item.empty or (s.item.item_id == source.item.item_id and s.item.count < 64)))
    step(operation=click(source.slot_id, 0)); mark("picked_up")
    settle()
    if gui().cursor_stack != source.item: raise RuntimeError("server corrected the picked-up stack")
    step(operation=click(destination.slot_id, 1)); mark("placed_one")
    settle()
    if gui().cursor_stack != replace(source.item, count=source.item.count - 1):
        raise RuntimeError("server corrected the one-item split")
    step(operation=click(source.slot_id, 0)); mark("returned_remainder")
    settle()
    if not gui().cursor_stack.empty: raise RuntimeError("cursor not empty before normal close")
    step(operation=CloseScreenV1()); mark("closed")
    settle()
    step(observation_request=INTERACTION)
    if not confirmed_block_target(obs(), block_position=target, face=operation.face,
            now_ns=time.perf_counter_ns(), controller_clock_id=obs().controller_clock_id):
        raise RuntimeError("fresh targeting query did not confirm chest before reopen")
    step(operation=operation, observation_request=INTERACTION); mark("reopen_dispatched")
    await_nonempty(); mark("reopened")
    stale = old_gui if previous_gui is None else previous_gui
    step(operation=ClickSlotV1(stale.gui_session_id, stale.sync_id, stale.revision, source.slot_id, 0, "pickup"), expected="failed")
    mark("stale_rejected")
    step(operation=CloseScreenV1()); mark("recovered")
    step(operation=SelectHotbarV1(2)); mark("hotbar")
    runtime.cancel("container_probe_complete")
    step(expected="cancelled"); mark("cancelled")
    return stages, rows, old_gui
