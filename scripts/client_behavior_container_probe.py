"""Normal-client bonus-chest interaction and server-reopened nonempty transfer evidence.

The only fixture privilege is the vanilla new-world bonus-chest option. Navigation
uses self position and current legal V3 blocks, never integrated-server world data.
This reference-clock slice does not claim accelerated use/cooldown equivalence.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import math
from pathlib import Path
import time
import traceback

from mc2p.backends.craftground_runtime import CraftGroundClockModeV0, load_sandbox_manifest
from mc2p.backends.craftground_behavior import CraftGroundBehaviorBackendV1
from mc2p.contracts.action_v1 import ActionSnapshotV1, CloseScreenV1, ClickSlotV1, InteractBlockV1, LookV1, MovementV1
from mc2p.contracts.common import ContractViolation, FieldStatusV0, require_identifier
from mc2p.contracts.observation_request_v3 import OBSERVATION_V3, ObservationRequestV3
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.contracts.reset import ResetRequestV0
from mc2p.runtime.trace import trace_projection
from mc2p.skills.targeting import confirmed_block_target
from scripts.client_behavior_probe_support import sandbox_provenance
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.smoke_test_craftground_structured import _read_diagnostic_tail


NAVIGATION = ObservationRequestV3()
INTERACTION = ObservationRequestV3("interaction_v1")


def nearest_observed_block(observation: ObservationSnapshotV3, block_id: str):
    if type(observation) is not ObservationSnapshotV3:
        raise ContractViolation("container discovery requires exact ObservationSnapshotV3")
    require_identifier(block_id, "container block id")
    if (observation.privileged_fields_present
            or observation.perception.status is not FieldStatusV0.VALID
            or observation.perception.value is None or observation.position.value is None):
        raise ContractViolation("container discovery requires available non-privileged V3 state")
    position = observation.position.value
    candidates = [block for block in observation.perception.value.blocks if block.block_id == block_id]
    def distance_squared(block):
        dx = block.position[0] + .5 - position.x
        dy = block.position[1] + .5 - position.y
        dz = block.position[2] + .5 - position.z
        return dx * dx + dy * dy + dz * dz
    return min(candidates, default=None, key=distance_squared)


def container_operation_from_target(observation: ObservationSnapshotV3, *,
                                    expected_position: tuple[int, int, int], now_ns: int) -> InteractBlockV1:
    if type(observation) is not ObservationSnapshotV3:
        raise ContractViolation("container targeting requires exact ObservationSnapshotV3")
    target = observation.targeting.value
    if (target is None or target.hit_kind != "block"
            or not confirmed_block_target(observation, block_position=expected_position, face=target.face,
                now_ns=now_ns, controller_clock_id=observation.controller_clock_id)):
        raise RuntimeError("fresh targeting query does not hit the expected chest")
    block = next((value for value in observation.perception.value.blocks
                  if value.position == expected_position), None)
    if block is None or block.block_id != "minecraft:chest":
        raise RuntimeError("targeted block is not an observed chest")
    return InteractBlockV1(*expected_position, target.face)


def _counts(gui: dict, *, container: bool) -> Counter:
    return sum((Counter({s["item"]["item_id"]: s["item"]["count"]}) for s in gui["slots"]
                if (s["source_kind"] == "container") == container and not s["item"]["empty"]), Counter())


def evaluate_transfer(before: dict, after: dict, reopened: dict, moved: dict) -> list[dict]:
    delta = Counter({moved["item_id"]: moved["count"]}) if not moved["empty"] else Counter()
    values = {
        "nonempty_stack_selected": bool(delta) and moved["count"] > 0,
        "ordinary_container_handlers": all(g["open"] and g["screen_kind"] == "generic_container"
                                            and g["sync_id"] > 0 for g in (before, after, reopened)),
        "stack_removed_from_container": _counts(before, container=True) == _counts(after, container=True) + delta,
        "stack_added_to_player": _counts(after, container=False) == _counts(before, container=False) + delta,
        "no_cursor_item": all(g["cursor_stack"]["empty"] for g in (before, after, reopened)),
        "new_server_handler_after_reopen": before["gui_session_id"] != reopened["gui_session_id"]
            and before["sync_id"] != reopened["sync_id"],
        "reopened_server_contents_match_transfer": after["slots"] == reopened["slots"],
    }
    return [{"name": name, "passed": passed} for name, passed in values.items()]


def require_live_observation(observation) -> None:
    if observation.is_dead.value is not False:
        raise RuntimeError("player is dead or unavailable in the declared safe fixture")


def run_container_worker(run_dir: Path, sandbox: Path, seed: int, port: int, timeout: float, *, furnace: bool = False) -> int:
    manifest = load_sandbox_manifest(sandbox)
    if manifest.clock_mode is not CraftGroundClockModeV0.REFERENCE_20_TPS:
        raise ValueError("container slice currently requires reference_20_tps; accelerated use timers are unverified")
    if manifest.observation_schema_version != OBSERVATION_V3:
        raise ValueError("container slice requires an explicit Observation V3 sandbox")
    diagnostic = sandbox / "run/mc2p-structured-observation.jsonl"
    offset = diagnostic.stat().st_size if diagnostic.is_file() else 0
    deadline = time.perf_counter_ns() + round((timeout - 15) * 1e9)
    backend = CraftGroundBehaviorBackendV1(port=port, runtime_env_path=sandbox,
        clock_mode=manifest.clock_mode, observation_mode=manifest.observation_mode,
        observation_schema_version=OBSERVATION_V3)
    episode = f"container-{seed}"
    receipts, checks, cleanup_failures = [], [], []
    milestones = {}
    failure = None
    observation_count = 0
    scenario = "flat-furnace-chest" if furnace else "flat-bonus-chest"
    try:
        reset = backend.reset(ResetRequestV0("container-reset", episode, scenario, seed, deadline))
        if not reset.succeeded or reset.observation is None:
            raise RuntimeError(f"container reset failed: {reset.failure}")
        observation = reset.observation

        def record():
            nonlocal observation_count
            append_jsonl(run_dir / "observations.jsonl", trace_projection(observation))
            observation_count += 1
            require_live_observation(observation)

        def step(label, *, operation=None, movement=MovementV1(), look=LookV1(),
                 observation_request=NAVIGATION):
            nonlocal observation
            if len(receipts) >= 2400:
                raise TimeoutError("bounded container probe exhausted its action budget")
            action = ActionSnapshotV1(episode, len(receipts), observation.sequence_id, deadline,
                                      operation=operation, movement=movement, look=look)
            append_jsonl(run_dir / "requests.jsonl", {"label": label, "action": trace_projection(action)})
            observation = backend.step(action, deadline, observation_request=observation_request).observation
            receipt = backend.last_behavior_receipt
            receipts.append(receipt)
            append_jsonl(run_dir / "receipts.jsonl", {"label": label, "receipt": receipt})
            record()
            return receipt

        def look_at(yaw, pitch, label="aim", *, forward=0, interaction=False):
            step(label, look=LookV1((yaw - observation.yaw_degrees.value + 180) % 360 - 180,
                                   pitch - observation.pitch_degrees.value), movement=MovementV1(forward=forward),
                 observation_request=INTERACTION if interaction else NAVIGATION)

        def chest_block():
            return nearest_observed_block(observation, "minecraft:chest")

        def gui():
            return trace_projection(observation.gui.value)

        def milestone(name):
            milestones[name] = {"sequence_id": observation.sequence_id, "gui": gui()}

        def await_container():
            for _ in range(40):
                g = observation.gui.value
                if g.open and g.screen_kind == "generic_container" and any(
                        s.source_kind == "container" and not s.item.empty for s in g.slots):
                    return
                step("await_server_container")
            raise TimeoutError("server did not open a nonempty normal container within 40 actions")

        record()
        for _ in range(8): step("settle")
        origin = observation.position.value
        found = None
        # Bounded exploration on a declared superflat surface; only current V3 block discovery can locate a chest.
        for dx, dz in ((0, 0), (8, 0), (8, 8), (0, 8), (-8, 8), (-8, 0), (-8, -8), (0, -8), (8, -8)):
            for _ in range(120):
                p = observation.position.value
                vx, vz = origin.x + dx - p.x, origin.z + dz - p.z
                if math.hypot(vx, vz) < 0.4: break
                look_at(math.degrees(math.atan2(-vx, vz)), 15, "explore_move", forward=1)
                if chest_block() is not None: break
            for _ in range(4): step("explore_release")
            for _ in range(48):
                found = chest_block()
                if found is not None: break
                look_at(observation.yaw_degrees.value + 7.5, 15, "explore_scan")
            if found is not None: break
        if found is None:
            raise RuntimeError("no bonus chest found by bounded legal blocks; no privileged search fallback")
        target = found.position
        milestones["first_visible_chest"] = {"sequence_id": observation.sequence_id,
            "target": target, "block": trace_projection(found)}
        # Short-term memory of the observed block; use still requires a fresh targeting query.
        for _ in range(160):
            p = observation.position.value
            vx, vz = target[0] + .5 - p.x, target[2] + .5 - p.z
            if math.hypot(vx, vz) < 2.7: break
            look_at(math.degrees(math.atan2(-vx, vz)), 15, "approach", forward=1)
        for _ in range(5): step("approach_release")
        p = observation.position.value
        vx, vz = target[0] + .5 - p.x, target[2] + .5 - p.z
        # Fixture requires a standing player: 1.62 is vanilla standing eye height, not server data.
        look_at(math.degrees(math.atan2(-vx, vz)), math.degrees(math.atan2(
            p.y + 1.62 - (target[1] + .45), math.hypot(vx, vz))), interaction=True)
        op = container_operation_from_target(observation, expected_position=target,
                                             now_ns=time.perf_counter_ns())
        before_reject = gui()
        receipt = step("wrong_block_rejected", operation=replace(op, block_x=op.block_x + 1),
                       observation_request=INTERACTION)
        checks.append({"name": "wrong_target_rejected_without_gui_change", "passed": receipt["status"] == "rejected"
                       and receipt["reason"] == "target_mismatch" and gui() == before_reject})
        receipt = step("open_chest", operation=op, observation_request=INTERACTION)
        checks.append({"name": "normal_block_dispatch_not_fake_confirmation", "passed":
            (receipt["status"], receipt["reason"]) == ("pending_confirmation", "block_use_dispatched")})
        await_container()
        milestone("opened")
        g = observation.gui.value
        source = next(s for s in g.slots if s.source_kind == "container" and not s.item.empty)
        old_click = ClickSlotV1(g.gui_session_id, g.sync_id, g.revision, source.slot_id, 0, "quick_move")
        moved = trace_projection(source.item)
        receipt = step("transfer_nonempty", operation=old_click)
        checks.append({"name": "click_dispatch_not_fake_confirmation", "passed":
            (receipt["status"], receipt["reason"]) == ("pending_confirmation", "slot_click_sent")})
        for _ in range(6): step("await_transfer_sync")
        milestone("after_transfer")
        step("close_chest", operation=CloseScreenV1())
        for _ in range(6): step("await_close_and_cooldown")
        if observation.gui.value.open: raise RuntimeError("normal close did not close the chest")
        step("confirm_reopen_target", observation_request=INTERACTION)
        if container_operation_from_target(observation, expected_position=target,
                                           now_ns=time.perf_counter_ns()) != op:
            raise RuntimeError("fresh targeting query did not confirm chest before reopen")
        step("reopen_chest", operation=op, observation_request=INTERACTION)
        await_container()
        milestone("reopened")
        before_stale = gui()
        receipt = step("stale_nonempty_reference", operation=old_click)
        checks.append({"name": "stale_nonempty_reference_rejected_without_change", "passed":
            (receipt["status"], receipt["reason"]) == ("rejected", "stale_gui_session") and gui() == before_stale})
        receipt = step("final_close", operation=CloseScreenV1())
        checks.append({"name": "normal_close_recovers_after_rejection", "passed":
            receipt["status"] == "confirmed_local" and not observation.gui.value.open})
        checks.extend(evaluate_transfer(*(milestones[k]["gui"] for k in ("opened", "after_transfer", "reopened")), moved))
        milestones["moved_stack"] = moved
        if furnace:
            from scripts.client_behavior_furnace_probe import run_furnace_extension
            checks.extend(run_furnace_extension(step=step, look_at=look_at, get_observation=lambda: observation,
                chest_operation=op, milestone=milestone))
    except Exception as error:
        failure = {"type": type(error).__name__, "message": str(error)}
        (run_dir / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        try: backend.close()
        except Exception as error: cleanup_failures.append(f"{type(error).__name__}: {error}")
    try:
        rows = _read_diagnostic_tail(diagnostic, offset)
        checks.append({"name": "continuous_zero_image_hidden_observations", "passed":
            len(rows) == observation_count == len(receipts) + 1 and observation_count > 1
            and len({r["session_id"] for r in rows}) == 1
            and [r["observation_sequence"] for r in rows] == list(range(1, observation_count + 1)) and all(
                r["image_bytes"] == r["framebuffer_capture_calls"] == r["image_encode_calls"] == 0
                and r["render_world_completions"] == 0 and r["window_visible"] is False
                and r["window_visible_at_creation"] is False for r in rows)})
    except Exception as error:
        failure = failure or {"type": type(error).__name__, "message": str(error)}
    checks.append({"name": "zero_callbacks_and_gui_drawing", "passed": bool(receipts) and all(
        r["action_keyboard_callbacks"] == r["action_mouse_callbacks"] == r["handled_screen_render_completions"] == 0 for r in receipts)})
    cleanup = backend.cleanup_status
    checks.append({"name": "process_and_port_clean", "passed": cleanup is not None and cleanup.process_stopped and cleanup.port_released})
    if failure is None and not all(r["passed"] for r in checks):
        failure = {"type": "ContainerEvidenceFailure", "message": str([r["name"] for r in checks if not r["passed"]])}
    result = {"schema_version": "mc2p.client-container-probe.v1", "status": "passed" if failure is None and not cleanup_failures else "failed",
        "scenario_id": scenario, "seed": seed, "provenance": sandbox_provenance(sandbox),
        "action_count": len(receipts), "observation_count": observation_count, "milestones": milestones,
        "checks": checks, "primary_failure": failure, "cleanup_failures": cleanup_failures}
    write_json_atomic(run_dir / "result.json", result)
    return 0 if result["status"] == "passed" else 2
