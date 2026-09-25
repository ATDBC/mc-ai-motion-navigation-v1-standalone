"""Streaming T9 proof for lawful hidden block replacement and re-observation."""
from __future__ import annotations

import json
import math
from pathlib import Path

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.runtime.segmented_trace import iter_segmented_jsonl, _safe_path
from mc2p.runtime.trace import trace_projection
from mc2p.skills.navigation_evidence import project_navigation_evidence
from mc2p.skills.navigation_memory import TerrainHistory
from mc2p.skills.perception_needs import PerceptionConfig
from scripts.active_perception_world_change import world_change_plan
from scripts.block_observation_v3_evidence import require_navigation_snapshot_v3
from scripts.navigation_motion_evidence import restore_snapshot


MAX_TRACE_OBSERVATIONS = 1000
MAX_JSON_BYTES = 1_048_576
SENSOR = dict(sensor_profile_revision=3, horizontal_fov_degrees=120.,
              vertical_fov_degrees=120., ray_columns=159, ray_rows=9,
              max_block_distance=16., body_expansion_blocks=.05,
              block_epsilon_blocks=.001, entity_max_distance=32.,
              entity_occlusion_epsilon_blocks=.05)
MARKER_KINDS = ("hidden_before", "hidden_after", "reobserved")
MARKER_WINDOWS = ((13_000_000_000, 15_000_000_000),
                  (25_000_000_000, 30_000_000_000),
                  (30_000_000_000, 38_000_000_000))


def _json(path: Path) -> dict:
    path = _safe_path(path)
    if not path.is_file():
        raise ValueError("world-change evidence file missing: " + path.name)
    with path.open("rb") as stream:
        data = stream.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("world-change evidence file exceeds bounded size")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid world-change evidence JSON") from error
    if type(value) is not dict:
        raise ValueError("world-change evidence must be an object")
    return value


def _triple(value, name):
    if (type(value) is not list or len(value) != 3
            or any(type(item) is not int or abs(item) > 30_000_000 for item in value)):
        raise ValueError(name + " must be a bounded integer triple")
    return tuple(value)


def _sensor(raw):
    perception = raw.get("perception")
    value = None if type(perception) is not dict else perception.get("value")
    if (type(value) is not dict or value.get("knowledge_model") != "block_state_v1"
            or "block_rays" in value
            or any(value.get(key) != expected for key, expected in SENSOR.items())):
        raise ValueError("world-change trace requires V3 profile3 120x120/1431 block evidence")


def _wrap(value):
    return (value + 180.) % 360. - 180.


def _neutral_body(raw, initial_position):
    own = raw["self_state"]["value"]
    velocity = own["velocity"]
    return (own["position"] == initial_position
            and velocity["x"] == 0. and velocity["z"] == 0.
            and type(velocity["y"]) in (int, float) and math.isfinite(velocity["y"])
            and own["pose"] == "standing" and own["is_on_ground"] is True
            and own["is_dead"] is False and own["horizontal_collision"] is False
            and own["is_swimming"] is False and own["is_submerged_in_water"] is False
            and own["is_climbing"] is False and own["is_fall_flying"] is False
            and own["is_burning"] is False and own["is_flying"] is False
            and own["fall_distance_blocks"] == 0.)


def _neutral_action(action):
    return (type(action) is dict and action.get("movement") == trace_projection(MovementV1())
            and action.get("operation") is None)


def _block_at(snapshot, position):
    values = tuple(block for block in snapshot.perception.value.blocks if block.position == position)
    if len(values) > 1:
        raise ValueError("duplicate world-change block observation")
    return None if not values else values[0]


def _history_projection(snapshot, block):
    stamp = project_navigation_evidence(snapshot, now_ns=snapshot.received_at_monotonic_ns,
                                        controller_clock_id=snapshot.controller_clock_id).stamp
    return trace_projection(TerrainHistory(block, stamp))


def _actor_evidence(directory, markers, changed, start_ns, config):
    marker_by_sequence = {marker["observation_sequence_id"]: marker for marker in markers}
    if len(marker_by_sequence) != 3:
        raise ValueError("world-change markers require distinct observation sequences")
    pending_task = None
    frames = {}
    histories = {}
    changed_rows = []
    count = 0
    initial_position = initial_yaw = episode = controller = None
    for row in iter_segmented_jsonl(directory / "runtime/MC2PFollower/trace"):
        kind, payload = row["record_type"], row["payload"]
        if kind == "playground_task":
            if pending_task is not None:
                raise ValueError("overlapping world-change task records")
            pending_task = payload
            continue
        if kind not in {"reset", "step", "close_release"}:
            continue
        raw = payload["result"]["observation"] if kind == "reset" else payload["backend_result"]["observation"]
        snapshot = require_navigation_snapshot_v3(raw)
        _sensor(raw)
        count += 1
        if count > MAX_TRACE_OBSERVATIONS:
            raise ValueError("world-change actor trace exceeds bounded observations")
        if initial_position is None:
            initial_position = raw["self_state"]["value"]["position"]
            initial_yaw = raw["self_state"]["value"]["yaw_degrees"]
            episode, controller = raw["episode_id"], raw["controller_clock_id"]
        if raw["episode_id"] != episode or raw["controller_clock_id"] != controller:
            raise ValueError("world-change actor scope changed")
        if not _neutral_body(raw, initial_position):
            raise ValueError("world-change actor body moved or became unsafe")
        context = None
        if kind != "reset":
            action = payload["decision"]["action"] if kind == "step" else payload["action"]
            if not _neutral_action(action):
                raise ValueError("world-change actor used movement or operation")
            if kind == "step" and payload["task"].get("task_type") == "playground_follow":
                context = pending_task
                if (type(context) is not dict
                        or context.get("observation_sequence_id") + 1 != raw["sequence_id"]
                        or payload["task"].get("task_id") != context.get("task_id")):
                    raise ValueError("world-change marker task is not bound to actual step")
            pending_task = None
        block = _block_at(snapshot, changed)
        if block is not None:
            projection = _history_projection(snapshot, block)
            histories[raw["sequence_id"]] = projection
            changed_rows.append((raw["received_at_monotonic_ns"], raw["sequence_id"], block))
            if block.block_id == "minecraft:cobblestone" and raw["received_at_monotonic_ns"] < start_ns + 30_000_000_000:
                raise ValueError("actor observed replacement before declared return")
        marker = marker_by_sequence.get(raw["sequence_id"])
        if marker is not None:
            if context is None:
                raise ValueError("world-change marker lacks active Driver step")
            frames[raw["sequence_id"]] = (raw, context)
    if pending_task is not None or count < 2:
        raise ValueError("world-change actor trace incomplete")
    for marker in markers:
        frame = frames.get(marker["observation_sequence_id"])
        if frame is None:
            raise ValueError("world-change marker observation absent")
        raw, context = frame
        if (marker["episode_id"] != raw["episode_id"]
                or marker["controller_clock_id"] != raw["controller_clock_id"]
                or marker["task_id"] != context.get("task_id")
                or marker["attempt_id"] != context.get("attempt_id")
                or marker["scope_id"] != context.get("attempt_id")
                or not 0 <= marker["sampled_at_ns"] - raw["received_at_monotonic_ns"] <= 250_000_000):
            raise ValueError("world-change marker identity/time is not bound to actual Driver step")
    hidden_yaw = _wrap(initial_yaw + 120.)
    for marker in markers[:2]:
        yaw = frames[marker["observation_sequence_id"]][0]["self_state"]["value"]["yaw_degrees"]
        if abs(_wrap(yaw - hidden_yaw)) > .001:
            raise ValueError("world-change actor did not hold the declared hidden yaw")
    returned_yaw = frames[markers[2]["observation_sequence_id"]][0]["self_state"]["value"]["yaw_degrees"]
    if abs(_wrap(returned_yaw - initial_yaw)) > .001:
        raise ValueError("world-change actor did not return to its initial yaw")
    before, after, reobserved = markers
    if before["history"] != after["history"]:
        raise ValueError("hidden block history was refreshed or rewritten")
    for received, _, block in changed_rows:
        if before["sampled_at_ns"] <= received <= start_ns + 30_000_000_000:
            raise ValueError("changed block appeared in hidden actor observations")
    old_sequence = before["history"]["last_seen"]["sequence_id"]
    old_prior_sequences = [sequence for _, sequence, _ in changed_rows
                           if sequence < before["observation_sequence_id"]]
    if (histories.get(old_sequence) != before["history"]
            or before["history"]["block"]["block_id"] != "minecraft:stone"
            or "first_hit_ray" not in before["history"]["block"]["sources"]
            or not old_prior_sequences or old_sequence != max(old_prior_sequences)):
        raise ValueError("old stone history lacks its actual first-hit observation")
    new_sequence = reobserved["history"]["last_seen"]["sequence_id"]
    prior_sequences = [sequence for _, sequence, _ in changed_rows
                       if sequence < reobserved["observation_sequence_id"]]
    if (histories.get(new_sequence) != reobserved["history"]
            or reobserved["history"]["block"]["block_id"] != "minecraft:cobblestone"
            or "first_hit_ray" not in reobserved["history"]["block"]["sources"]
            or not prior_sequences or new_sequence != max(prior_sequences)
            or reobserved["history"]["last_seen"]["request_start_ns"] < start_ns + 30_000_000_000
            or reobserved["sampled_at_ns"] - reobserved["history"]["last_seen"]["request_start_ns"] > 500_000_000):
        raise ValueError("replacement history lacks a fresh latest actual first-hit observation")
    return dict(count=count, episode=episode, controller=controller, initial_yaw=initial_yaw,
                old_sequence=old_sequence, new_sequence=new_sequence,
                hidden_count=sum(before["sampled_at_ns"] <= raw_time <= after["sampled_at_ns"]
                                 for raw_time, _, _ in changed_rows))


def _targeted(raw):
    if raw.get("field_profile") != "interaction_v1":
        raise ValueError("leader block operation lacks interaction profile")
    group = raw.get("targeting")
    value = None if type(group) is not dict else group.get("value")
    if (group.get("status") != "valid" or group.get("source_kind") != "client_perception_filtered"
            or type(value) is not dict or value.get("hit_kind") != "block"):
        raise ValueError("leader operation lacks current block target")
    position = tuple(value.get("block_position") or ())
    blocks = raw["perception"]["value"]["blocks"]
    matches = [block for block in blocks if tuple(block["position"]) == position]
    if len(matches) != 1 or "current_target" not in matches[0]["sources"]:
        raise ValueError("leader current target is absent from lawful block evidence")
    return position, value["face"], matches[0]


def _receipt(action, raw, receipt, operation):
    expected_status = "pending_confirmation" if operation is not None else None
    if (type(receipt) is not dict or receipt.get("schema_version") != "mc2p.client_action_receipt.v2"
            or receipt.get("episode_id") != raw["episode_id"]
            or receipt.get("generation_id") != raw["sequence_id"]
            or receipt.get("request_sequence_id") != action["request_sequence_id"]
            or receipt.get("world_tick") != raw["world_time_ticks"]["value"]
            or receipt.get("on_client_thread") is not True
            or expected_status is not None and receipt.get("status") != expected_status
            or expected_status is None and receipt.get("status") not in {"executed", "confirmed_local", "cancelled"}):
        raise ValueError("leader action receipt is not exactly associated")


def _leader_evidence(directory, support, changed, start_ns):
    previous = None
    count = 0
    stages = []
    effects = []
    episode = controller = None
    for row in iter_segmented_jsonl(directory / "runtime/MC2PLeader/trace"):
        kind, payload = row["record_type"], row["payload"]
        if kind not in {"reset", "step", "close_release"}:
            continue
        raw = payload["result"]["observation"] if kind == "reset" else payload["backend_result"]["observation"]
        snapshot = restore_snapshot(raw)
        if type(snapshot) is not ObservationSnapshotV3 or snapshot.privileged_fields_present:
            raise ValueError("world-change leader trace is not unprivileged V3")
        _sensor(raw)
        count += 1
        if count > MAX_TRACE_OBSERVATIONS:
            raise ValueError("world-change leader trace exceeds bounded observations")
        if episode is None:
            episode, controller = raw["episode_id"], raw["controller_clock_id"]
        if raw["episode_id"] != episode or raw["controller_clock_id"] != controller:
            raise ValueError("world-change leader scope changed")
        if kind != "reset":
            action = payload["decision"]["action"] if kind == "step" else payload["action"]
            if (previous is None or action["episode_id"] != previous["episode_id"]
                    or action["observation_sequence_id"] != previous["sequence_id"]
                    or raw["sequence_id"] != previous["sequence_id"] + 1
                    or raw["request_sequence_id"] != action["request_sequence_id"]
                    or action["movement"] != trace_projection(MovementV1())):
                raise ValueError("leader action is not a neutral continuous Runtime step")
            operation = action["operation"]
            _receipt(action, raw, payload["backend_result"]["receipt"], operation)
            if kind == "step":
                selected = dict(payload["decision"]["selected_intents"])
                if (selected.get("movement") != selected.get("look")
                        or operation is not None and selected.get("operation") != selected.get("look")):
                    raise ValueError("leader operation was not the selected ordinary action")
            if operation is not None:
                kind_name = operation["kind"]
                inventory = previous["inventory"]["value"]
                if kind_name == "select_hotbar":
                    slot = operation["slot"]
                    item = inventory["main"][slot]
                    if not stages:
                        expected = "minecraft:stone"
                    elif stages == ["interact_block", "mine_block"]:
                        expected = "minecraft:cobblestone"
                    else:
                        raise ValueError("leader changed building item outside the declared sequence")
                    if item["empty"] or item["item_id"] != expected:
                        raise ValueError("leader selected a missing or wrong building item")
                elif kind_name in {"interact_block", "mine_block"}:
                    position, face, block = _targeted(previous)
                    operation_position = tuple(operation["block_" + axis] for axis in "xyz")
                    if operation_position != position or operation["face"] != face:
                        raise ValueError("leader operation differs from current block/face")
                    if kind_name == "interact_block":
                        expected_item = "minecraft:stone" if not stages else "minecraft:cobblestone"
                        if (position != support or face != "up" or block["block_id"] != "minecraft:grass_block"
                                or inventory["main_hand"]["item_id"] != expected_item):
                            raise ValueError("leader placement lacks observed support or selected item")
                    else:
                        if (stages != ["interact_block"] or position != changed
                                or block["block_id"] != "minecraft:stone"
                                or raw["request_started_at_monotonic_ns"] < start_ns + 15_000_000_000):
                            raise ValueError("leader mine lacks the actual stone after change start")
                    stages.append(kind_name)
                else:
                    raise ValueError("world-change leader used an undeclared operation")
            block = _block_at(snapshot, changed)
            if block is not None and "first_hit_ray" in block.sources:
                effects.append((raw["sequence_id"], block.block_id))
        previous = raw
    if stages != ["interact_block", "mine_block", "interact_block"]:
        raise ValueError("leader replacement operation sequence incomplete")
    if not any(name == "minecraft:stone" for _, name in effects) or not any(name == "minecraft:cobblestone" for _, name in effects):
        raise ValueError("leader pending receipts lack independent observed world effects")
    return dict(count=count, episode=episode, controller=controller,
                stages=stages, effects=effects)


def _evaluate_world_change(directory: Path) -> dict:
    """Validate one sealed world-change case; raw observations are never retained."""
    directory = _safe_path(Path(directory))
    plan = _json(directory / "case-plan.json")
    if plan != world_change_plan():
        raise ValueError("world-change case plan differs from the frozen timing")
    start = _json(directory / "case-start.json")
    if (set(start) != {"case", "started_at_ns"} or start["case"] != "active-world-change"
            or type(start["started_at_ns"]) is not int or start["started_at_ns"] <= 0):
        raise ValueError("invalid world-change case start")
    manifest = _json(directory / "manifest.json")
    expected_config = trace_projection(PerceptionConfig())
    if (manifest.get("perception_variant") != "active_perception_v1"
            or manifest.get("perception_config") != expected_config):
        raise ValueError("world-change case requires frozen active perception configuration")
    sidecar = _json(directory / "world-change-history.json")
    fields = {"schema_version", "support", "changed", "clock_source", "clock_bindings", "markers"}
    if set(sidecar) != fields or sidecar["schema_version"] != "mc2p.world-change-history.v1":
        raise ValueError("invalid world-change history schema")
    support = _triple(sidecar["support"], "world-change support")
    changed = _triple(sidecar["changed"], "world-change changed block")
    if changed != (support[0], support[1] + 1, support[2]):
        raise ValueError("world-change replacement is not above support")
    markers = sidecar["markers"]
    marker_fields = {"kind", "sampled_at_ns", "observation_sequence_id", "episode_id",
                     "controller_clock_id", "scope_id", "task_id", "attempt_id", "history"}
    if type(markers) is not list or len(markers) != 3:
        raise ValueError("world-change history requires exactly three markers")
    identity = None
    for index, marker in enumerate(markers):
        if (type(marker) is not dict or set(marker) != marker_fields
                or marker["kind"] != MARKER_KINDS[index]
                or type(marker["sampled_at_ns"]) is not int
                or type(marker["observation_sequence_id"]) is not int
                or marker["observation_sequence_id"] < 0
                or any(type(marker[name]) is not str or not marker[name] or len(marker[name]) > 128
                       for name in ("episode_id", "controller_clock_id", "scope_id", "task_id", "attempt_id"))
                or type(marker["history"]) is not dict):
            raise ValueError("invalid world-change marker schema")
        elapsed = marker["sampled_at_ns"] - start["started_at_ns"]
        lower, upper = MARKER_WINDOWS[index]
        if not lower <= elapsed <= upper:
            raise ValueError("world-change marker outside declared window")
        current = tuple(marker[name] for name in ("episode_id", "controller_clock_id", "scope_id", "task_id", "attempt_id"))
        if identity is None:
            identity = current
        elif current != identity:
            raise ValueError("world-change marker task/scope changed")
        if index and (marker["sampled_at_ns"] <= markers[index - 1]["sampled_at_ns"]
                      or marker["observation_sequence_id"] <= markers[index - 1]["observation_sequence_id"]):
            raise ValueError("world-change markers are not strictly ordered")
    actor = _actor_evidence(directory, markers, changed, start["started_at_ns"], expected_config)
    leader = _leader_evidence(directory, support, changed, start["started_at_ns"])
    bindings = sidecar["clock_bindings"]
    if (sidecar["clock_source"] != "time.perf_counter_ns" or type(bindings) is not dict
            or actor["controller"] == leader["controller"] or len(bindings) != 2
            or set(bindings) != {actor["controller"], leader["controller"]}
            or len(set(bindings.values())) != 1
            or any(type(value) is not str or not value for value in bindings.values())):
        raise ValueError("world-change controller clock association invalid")
    checks = [dict(name=name, passed=True) for name in (
        "world_change_markers_bound_to_actual_task",
        "actor_stationary_with_declared_hidden_and_return_yaw",
        "hidden_stone_history_not_refreshed",
        "fresh_cobblestone_reobserved_from_actual_ray",
        "ordinary_leader_replacement_actions_and_effects",
    )]
    metrics = dict(support=list(support), changed=list(changed), actor_observations=actor["count"],
        leader_observations=leader["count"], hidden_changed_observations=actor["hidden_count"],
        old_stone_sequence_id=actor["old_sequence"], new_cobblestone_sequence_id=actor["new_sequence"],
        initial_yaw_degrees=actor["initial_yaw"], leader_operations=list(leader["stages"]),
        marker_elapsed_ns=[marker["sampled_at_ns"] - start["started_at_ns"] for marker in markers])
    return dict(schema_version="mc2p.active-perception-world-change-evidence.v1",
                checks=checks, metrics=metrics)


def evaluate_world_change(directory: Path) -> dict:
    """Public strict boundary: structural corruption is always ValueError."""
    try:
        return _evaluate_world_change(directory)
    except ValueError:
        raise
    except (KeyError, TypeError, IndexError, AttributeError) as error:
        raise ValueError("malformed world-change evidence") from error
