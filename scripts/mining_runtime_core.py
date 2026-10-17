"""Bounded survival mining scenario shared by reference and independent client probes.

Every target comes from V3 block discovery plus a fresh targeting query. No world queries,
actor commands, or backend.step calls.
Evaluation re-reads the Runtime trace; milestone numbers are only indices, not success flags.
"""
from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
import time
import uuid

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import (ActionIntentV1, CloseScreenV1, InteractBlockV1, LookV1,
    MineBlockV1, MovementV1, OpenInventoryV1, SelectHotbarV1)
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation, FieldStatusV0
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.task import TaskIntentV0, SuccessCriterionV0, ComparisonOperatorV0
from mc2p.runtime.trace import trace_projection
from mc2p.skills.targeting import confirmed_block_target
from mc2p.skills.active_perception import ActivePerceptionCoordinator
from mc2p.skills.follow_tracking import project_playground_view
from mc2p.skills.interaction_aim import InteractionAim, ActionEvidenceInvalidations, select_work_face
from mc2p.skills.navigation_memory import NavigationMemory
from mc2p.skills.perception_needs import PerceptionConfig, PerceptionNeed
from scripts.control_probe_core import append_jsonl, write_json_atomic

STAGES = ("before", "started", "interrupted", "recovered", "removed", "picked", "aimed_place", "placed", "confirmed", "cancelled")
FACE_OFFSET = dict(up=(0, 1, 0), down=(0, -1, 0), north=(0, 0, -1), south=(0, 0, 1), west=(-1, 0, 0), east=(1, 0, 0))
NAVIGATION = ObservationRequestV3()
INTERACTION = ObservationRequestV3("interaction_v1")


def action_record(label: str, result, previous_sequence: int) -> dict:
    return dict(label=label, sequence=previous_sequence if result.observation is None else result.observation.sequence_id,
        request=trace_projection(None if result.decision is None else result.decision.action), report=trace_projection(result.report))


def _raw_targeted_block(observation: dict) -> tuple[tuple[int, int, int], dict, dict]:
    if (observation["schema_version"] != "mc2p.observation.v3"
            or observation["field_profile"] != "interaction_v1"):
        raise ValueError("mining evidence requires interaction Observation V3")
    perception = observation["perception"]["value"]
    if perception["knowledge_model"] != "block_state_v1" or "block_rays" in perception:
        raise ValueError("mining evidence requires native block-state knowledge")
    targeting = observation["targeting"]
    target = targeting["value"]
    if targeting["status"] != "valid" or target["hit_kind"] != "block":
        raise ValueError("mining evidence requires a current block target")
    position = tuple(target["block_position"])
    if len(position) != 3 or any(type(value) is not int for value in position):
        raise ValueError("invalid targeted block position")
    matches = [block for block in perception["blocks"] if tuple(block["position"]) == position]
    if len(matches) != 1 or "current_target" not in matches[0]["sources"]:
        raise ValueError("targeted block is absent from current block knowledge")
    return position, target, matches[0]


def target_position(observation: dict) -> tuple[int, int, int] | None:
    try:
        return _raw_targeted_block(observation)[0]
    except (KeyError, TypeError, ValueError):
        return None


def current_block_target(observation: ObservationSnapshotV3, *, now_ns: int | None = None):
    if type(observation) is not ObservationSnapshotV3:
        raise ContractViolation("mining requires exact ObservationSnapshotV3")
    target = observation.targeting.value
    if (observation.targeting.status is not FieldStatusV0.VALID or target is None
            or target.hit_kind != "block"):
        return None
    checked_at = time.perf_counter_ns() if now_ns is None else now_ns
    if not confirmed_block_target(observation, block_position=target.block_position, face=target.face,
                                  now_ns=checked_at, controller_clock_id=observation.controller_clock_id):
        return None
    block = next((value for value in observation.perception.value.blocks
                  if value.position == target.block_position), None)
    return None if block is None else (target, block)


class _MiningPerception:
    """One camera owner for this bounded probe; no direct backend or world access.

    The callback returns (Runtime result, submitted intent id) and owns the
    original total action/time budget. A remembered coordinate filters current
    discovery; it never inserts an unseen block into the evidence.
    """
    def __init__(self, runtime, step, deadline, directory, *, clock=None):
        self.runtime, self.step, self.deadline, self.directory = runtime, step, deadline, directory
        self.clock = clock or time.perf_counter_ns
        self.scope = 'mining-work-' + uuid.uuid4().hex
        self.memory = NavigationMemory(self.scope)
        self.coordinator = ActivePerceptionCoordinator('active_perception_v1', PerceptionConfig(), clock=self.clock)
        self.aim = InteractionAim(self.coordinator)
        self.invalidations = ActionEvidenceInvalidations()
        self._confirmed = None

    def observe(self):
        now, observation = self.clock(), self.runtime.observation
        snapshot = self.memory.observe(observation, now_ns=now,
            controller_clock_id=observation.controller_clock_id, scope_id=self.scope)
        return now, snapshot, project_playground_view(observation, now, observation.controller_clock_id)

    def _look_step(self, decision, view):
        result, identifier = self.step('work_face_look', look=decision.look, observation_request=INTERACTION)
        now, snapshot, post = self.observe()
        receipt = None if result.backend_result is None else result.backend_result.receipt
        action = None if result.decision is None else result.decision.action
        selected = {} if result.decision is None else dict(result.decision.selected_intents)
        before, after = view.base.own, post.base.own
        applied = (before is not None and after is not None
            and abs((after.yaw-before.yaw-decision.look.yaw_delta_degrees+180)%360-180) <= .001
            and abs(after.pitch-before.pitch-decision.look.pitch_delta_degrees) <= .001)
        accepted = bool(result.observation == self.runtime.observation and result.report.failure is None
            and receipt is not None and receipt.status in {'executed', 'confirmed_local'}
            and selected.get('movement') == identifier and selected.get('look') == identifier
            and action is not None and action.operation is None and action.movement == MovementV1()
            and action.look == decision.look and applied)
        self.aim.feedback(accepted, snapshot.latest, now)
        append_jsonl(self.directory / 'mining-perception.jsonl', dict(scope_id=self.scope,
            variant=self.coordinator.variant, config=trace_projection(self.coordinator.config),
            before_sequence=view.base.sequence_id, after_sequence=post.base.sequence_id,
            decision=trace_projection(decision), selected=accepted,
            planner=self.coordinator.status(), planning_ns=self.coordinator.planning_elapsed_ns))
        if not accepted:
            raise RuntimeError('unconfirmed mining look: selection, receipt or applied pose mismatch')

    def aim_for(self, operation_kind, *, block_position=None,
                allowed_block_ids=('minecraft:grass_block', 'minecraft:dirt'), face='up'):
        self._confirmed = None
        try:
            for _ in range(400):
                now, snapshot, view = self.observe()
                if now >= self.deadline:
                    raise TimeoutError('work aim task deadline exceeded')
                latest = snapshot.latest
                if (latest is None or not latest.available or not view.base.available
                        or view.base.own is None or view.base.gui_open or view.base.own.dead
                        or view.base.own.unsupported_motion):
                    raise RuntimeError('work aim body or observation unavailable')
                if self.aim.target is None:
                    candidates = latest if block_position is None else replace(latest,
                        blocks=tuple(b for b in latest.blocks if b.position == block_position))
                    target = select_work_face(candidates, self.scope,
                        allowed_block_ids=allowed_block_ids, face=face)
                    if target is not None and self.invalidations.allows(target.block, latest.stamp):
                        self.aim.begin(target, operation_kind=operation_kind, deadline_ns=self.deadline)
                    else:
                        # Looking into unknown space is a proposal, not knowledge.
                        # Each bounded scan still uses the same gaze controller.
                        own = view.base.own
                        yaw = math.radians(own.yaw + 15)
                        point = Vec3V0(own.position.x-3*math.sin(yaw),
                            own.position.y+1.62-3*math.tan(math.radians(35)), own.position.z+3*math.cos(yaw))
                        need = PerceptionNeed('mining-discovery', 'search_attention', self.scope,
                            latest.stamp, 'search', 50, self.deadline, point, 'filtered_check')
                        decision = self.coordinator.decide(snapshot, view, (need,), None, None, now,
                            min(self.deadline, now+250_000_000))
                        if decision.reason != 'selected_task_fragment':
                            raise RuntimeError('work discovery failed: '+decision.reason)
                        self._look_step(decision, view)
                        continue
                decision = self.aim.decide(snapshot, view, now)
                if decision.reason != 'selected_task_fragment':
                    raise RuntimeError('work aim failed: '+decision.reason)
                current = current_block_target(self.runtime.observation, now_ns=now)
                target = self.aim.target
                if ('interaction-work-face' in decision.completed_need_ids and decision.look == LookV1()
                        and current is not None and current[0].block_position == target.block
                        and current[0].face == target.face and current[1].block_id in allowed_block_ids
                        and self.invalidations.allows(target.block, latest.stamp)):
                    self.aim.release('operation_dispatch')
                    self._confirmed = (operation_kind, target.block, target.face)
                    return current
                self._look_step(decision, view)
            raise TimeoutError('work aim exceeded bounded action search')
        finally:
            self.aim.release('work_aim_finished')
            self.coordinator.clear_control('work_aim_finished')

    def before_operation(self, operation):
        if type(operation) not in {MineBlockV1, InteractBlockV1}:
            raise ContractViolation('work dependency requires block operation')
        now, snapshot, _ = self.observe()
        current = current_block_target(self.runtime.observation, now_ns=now)
        block = (operation.block_x, operation.block_y, operation.block_z)
        if self._confirmed != (operation.kind, block, operation.face):
            raise RuntimeError('operation requires a confirmed work goal')
        if (self.aim.target is not None or self.coordinator.gaze.pending_control is not None
                or current is None or current[0].block_position != block or current[0].face != operation.face
                or not self.invalidations.allows(block, snapshot.latest.stamp)):
            raise RuntimeError('operation requires released aim and fresh current block evidence')
        dependencies = (block,)
        if type(operation) is InteractBlockV1:
            dependencies += (tuple(a+b for a,b in zip(block, FACE_OFFSET[operation.face])),)
        if not all(self.invalidations.allows(b, snapshot.latest.stamp) for b in dependencies):
            raise RuntimeError('operation dependencies need a new causal observation')
        # Mark before dispatch, retaining invalidation even if execution fails.
        self.invalidations.mark(dependencies, snapshot.latest.stamp)


def dirt_count(observation: dict) -> int:
    return sum(item["count"] for item in observation["inventory"]["value"]["main"]
               if not item["empty"] and item["item_id"] == "minecraft:dirt")


def pickup_forward(distance: float) -> int:
    # A one-block-deep drop is outside pickup height while standing at the lip.
    return 1 if distance > .15 else 0


def evaluate_mining_reconnect(previous: dict, samples: list[dict]) -> list[dict]:
    try:
        previous_position, _, previous_block = _raw_targeted_block(previous)
        sample_targets = [_raw_targeted_block(observation) for observation in samples]
        checks = dict(
            reconnected_server_block_persists=len(samples) >= 3 and previous_block["block_id"] == "minecraft:dirt"
                and all(position == previous_position and block["block_id"] in {"minecraft:dirt", "minecraft:grass_block"}
                        for position, _, block in sample_targets),
            reconnected_server_inventory_persists=len(samples) >= 3 and all(o["inventory"]["value"] == previous["inventory"]["value"]
                and dirt_count(o) == 0 for o in samples),
        )
        result = [dict(name=k, passed=v) for k, v in checks.items()]
        result[0].update(observed_block_ids=sorted({block["block_id"] for _, _, block in sample_targets}),
            meaning="same placed coordinate; grass is consistent with normal vanilla spread, not a directly observed conversion event")
        return result
    except (KeyError, TypeError, ValueError, IndexError) as error:
        return [dict(name="well_formed_mining_reconnect", passed=False, detail=str(error))]


def run_mining_reconnect(runtime, directory: Path, deadline: int, previous: dict, *, on_observation=lambda: None) -> list[dict]:
    """Read the saved placement from a newly joined ordinary client; no new mining or inventory edits."""
    task = TaskIntentV0("mining-reconnect", "mining-core", "{}",
        (SuccessCriterionV0("persisted_blocks", ComparisonOperatorV0.EQUAL, 1, "blocks"),), 200, deadline, True, 0.0)
    target = target_position(previous)
    if target is None: raise ValueError("missing previously observed placement")
    on_observation()
    samples, count = [], 0
    def step(label, *, look=LookV1(), operation=None, observation_request=INTERACTION):
        nonlocal count
        count += 1
        if count > 20: raise TimeoutError('mining reconnect exceeded 20 actions')
        runtime.cancel_source('mining-reconnect')
        now = time.perf_counter_ns()
        identifier = f'reconnect-{count}'
        runtime.submit_intent(ActionIntentV1(identifier, "mining-reconnect", runtime.observation.episode_id,
            runtime.observation.sequence_id, ActionPriorityV0.TASK, now, min(deadline, now + 2_000_000_000),
            movement=MovementV1(), look=look, operation=operation))
        final = label == 'reconnect_final'
        if final: runtime.cancel("reconnect_verified")
        result = runtime.step(task, BehaviorProfileV0(), min(deadline, now + 5_000_000_000),
                              observation_request=observation_request)
        if result.observation is None or result.report.status.value != ("cancelled" if final else "running"):
            raise RuntimeError(f"mining reconnect failed: {result.report}")
        on_observation()
        return result, identifier
    for i in range(6):
        step('reconnect_settle', operation=OpenInventoryV1() if i==3 else CloseScreenV1() if i==4 else None,
            observation_request=NAVIGATION)
    perception = _MiningPerception(runtime, step, deadline, directory)
    perception.aim_for('interact_block', block_position=target)
    if count > 17: raise TimeoutError('reconnect aim left fewer than three verification samples')
    while count < 20:
        result, _ = step('reconnect_final' if count==19 else 'reconnect_verify')
        samples.append(trace_projection(result.observation))
    write_json_atomic(directory / "mining-reconnect.json", dict(previous=previous, samples=samples))
    return evaluate_mining_reconnect(previous, samples)


def evaluate_mining(records: list[dict], stages: dict) -> list[dict]:
    try:
        if set(stages) != set(STAGES): raise ValueError("missing or extra mining milestone")
        sequences = [stages[k] for k in STAGES]
        if not all(type(i) is int for i in sequences) or not all(a < b for a, b in zip(sequences, sequences[1:])):
            raise ValueError("mining milestones are not strictly ordered")
        steps = {r["payload"]["backend_result"]["observation"]["sequence_id"]: r["payload"]
                 for r in records if r["record_type"] == "step"}
        observations = {r["payload"]["result"]["observation"]["sequence_id"]: r["payload"]["result"]["observation"]
                        for r in records if r["record_type"] == "reset"}
        observations.update({i: s["backend_result"]["observation"] for i, s in steps.items()})
        if any(observation.get("schema_version") != "mc2p.observation.v3"
               for observation in observations.values()):
            raise ValueError("mining trace is not Observation V3")
        s = {k: observations[i] for k, i in stages.items()}
        target, before_target, before_block = _raw_targeted_block(s["before"])
        def action(label): return steps[stages[label]]["decision"]["action"]
        def op_target(op): return tuple(op["block_" + k] for k in "xyz")
        def neutral(a):
            return a["operation"] is None and not any(a["movement"].values()) and not any(a["look"].values())
        support, support_target, support_block = _raw_targeted_block(s["aimed_place"])
        placement_op = action("placed")["operation"]
        placed = tuple(a + b for a, b in zip(support, FACE_OFFSET[support_target["face"]]))
        checks = {
            "survival_legal_surface_target": before_block["block_id"] in {"minecraft:grass_block", "minecraft:dirt"}
                and all(o["self_state"]["value"]["game_mode"] == "survival" and o["is_dead"]["value"] is False for o in observations.values()),
            "mine_interrupt_then_fresh_recovery": all(action(k)["operation"]["kind"] == "mine_block"
                and op_target(action(k)["operation"]) == target
                and steps[stages[k]]["backend_result"]["receipt"]["status"] == "pending_confirmation" for k in ("started", "recovered"))
                and stages["interrupted"] - stages["started"] >= 20
                and target_position(s["started"]) == target_position(s["interrupted"])
                    == target_position(s["recovered"]) == target
                and all(neutral(steps[i]["decision"]["action"]) for i in range(stages["started"] + 1, stages["interrupted"] + 1)),
            "same_aim_observes_target_removal": target_position(s["removed"]) != target
                and _raw_targeted_block(s["removed"])[1]["distance_blocks"] > before_target["distance_blocks"] + .1
                and all(s[k][field] == s["before"][field] for k in ("started", "interrupted", "recovered", "removed")
                        for field in ("position", "yaw_degrees", "pitch_degrees")),
            "server_pickup_adds_expected_dirt": dirt_count(s["before"]) == 0 and dirt_count(s["picked"]) == 1,
            "placement_uses_observed_support_and_dirt": placement_op["kind"] == "interact_block"
                and op_target(placement_op) == support and placement_op["face"] == support_target["face"]
                and support_block["block_id"] in {"minecraft:grass_block", "minecraft:dirt"}
                and s["aimed_place"]["inventory"]["value"]["main_hand"]["item_id"] == "minecraft:dirt"
                and steps[stages["placed"]]["backend_result"]["receipt"]["status"] == "pending_confirmation",
            "placed_dirt_persists_and_costs_one_item": all(target_position(s[k]) == placed
                and _raw_targeted_block(s[k])[2]["block_id"] == "minecraft:dirt"
                and dirt_count(s[k]) == dirt_count(s["picked"]) - 1
                for k in ("placed", "confirmed", "cancelled")) and stages["confirmed"] - stages["placed"] >= 10,
            "runtime_final_cancel_neutral": steps[stages["cancelled"]]["report"]["status"] == "cancelled" and neutral(action("cancelled")),
        }
        return [dict(name=k, passed=bool(v)) for k, v in checks.items()]
    except (KeyError, TypeError, ValueError, IndexError) as error:
        return [dict(name="well_formed_mining_evidence", passed=False, detail=str(error))]


def run_mining_scenario(runtime, directory: Path, deadline: int, *, on_observation=lambda: None) -> dict:
    """Requires an already reset, empty-inventory superflat survival Runtime."""
    task = TaskIntentV0("mining-probe", "mining-core", "{}",
        (SuccessCriterionV0("placed_blocks", ComparisonOperatorV0.EQUAL, 1, "blocks"),), 200, deadline, True, 0.0)
    profile, stages, counter = BehaviorProfileV0(), {}, 0
    def obs(): return trace_projection(runtime.observation)
    def step(label, *, operation=None, movement=MovementV1(), look=LookV1(), ticks=1,
             expected="running", observation_request=NAVIGATION):
        nonlocal counter
        counter += 1
        if counter > 400: raise TimeoutError("mining scenario exceeded 400 actions")
        if type(operation) in {MineBlockV1, InteractBlockV1}:
            perception.before_operation(operation)
        # Remove our previous persistent movement. Only the unique arbiter executes the new snapshot.
        runtime.cancel_source("mining-probe")
        now = time.perf_counter_ns()
        runtime.submit_intent(ActionIntentV1(f"mining-{counter}", "mining-probe", runtime.observation.episode_id,
            runtime.observation.sequence_id, ActionPriorityV0.TASK, now, min(deadline, now + 2_000_000_000),
            movement=movement, look=look, operation=operation, valid_for_ticks=ticks))
        previous_sequence = runtime.observation.sequence_id
        result = runtime.step(task, profile, min(deadline, now + 5_000_000_000),
                              observation_request=observation_request)
        append_jsonl(directory / "actions.jsonl", action_record(label, result, previous_sequence))
        if result.observation is not None: on_observation()
        if result.observation is None or result.report.status.value != expected:
            raise RuntimeError(f"mining stage {label}: {result.report}")
        if result.observation.is_dead.value is not False: raise RuntimeError("mining fixture player died")
        return result
    def mark(label):
        stages[label] = runtime.observation.sequence_id
        write_json_atomic(directory / "mining-stages.json", stages)
        print(f"MINING_STAGE={label} sequence={runtime.observation.sequence_id}", flush=True)
    def camera_step(label, **kwargs):
        result = step(label, **kwargs)
        return result, f'mining-{counter}'
    perception = _MiningPerception(runtime, camera_step, deadline, directory)
    on_observation()
    for _ in range(8): step("settle")
    if dirt_count(obs()) != 0 or runtime.observation.self_state.value.game_mode != "survival":
        raise RuntimeError("mining fixture requires survival and no initial dirt")
    # Essential GUI->mining regression: vanilla release must clear screen attack suppression.
    step("open_before_mining", operation=OpenInventoryV1())
    step("close_before_mining", operation=CloseScreenV1())
    current = perception.aim_for('mine_block')
    before = obs()
    if current is None:
        raise RuntimeError("bounded standing aim did not produce a fresh block target")
    targeted, block = current
    target = targeted.block_position
    if block.block_id not in {"minecraft:grass_block", "minecraft:dirt"} or targeted.face != "up":
        raise RuntimeError("bounded standing aim did not observe a reachable dirt/grass top face")
    mark("before")
    operation = MineBlockV1(*target, targeted.face)
    step("start_mining", operation=operation, ticks=20, observation_request=INTERACTION); mark("started")
    for _ in range(24): step("interrupt_with_neutral", observation_request=INTERACTION)
    mark("interrupted")
    interrupted = current_block_target(runtime.observation)
    if interrupted is None or interrupted[0].block_position != target:
        raise RuntimeError("mining continued after neutral interrupt")
    perception.aim_for('mine_block', block_position=target)
    step("recover_mining", operation=operation, ticks=20, observation_request=INTERACTION); mark("recovered")
    for _ in range(100):
        current = current_block_target(runtime.observation)
        if current is None or current[0].block_position != target: break
        step("renew_same_target", operation=operation, ticks=20, observation_request=INTERACTION)
    else: raise TimeoutError("target remained after 100 bounded mining renewals")
    mark("removed")
    step("release_after_removal", observation_request=INTERACTION)
    # Enter the remembered one-block hole normally; the drop can be below ledge pickup height.
    for _ in range(70):
        if dirt_count(obs()) == 1: break
        p = runtime.observation.position.value
        dx, dz = target[0] + .5 - p.x, target[2] + .5 - p.z
        yaw = math.degrees(math.atan2(-dx, dz))
        step("approach_drop", movement=MovementV1(forward=pickup_forward(math.hypot(dx, dz))),
             look=LookV1((yaw - runtime.observation.yaw_degrees.value + 180) % 360 - 180, 0))
    else: raise TimeoutError("normal pickup did not add one dirt within 70 steps")
    for _ in range(6): step("pickup_release")
    mark("picked")
    # Normal jump/move out towards the observed original standing point, without teleporting.
    origin = before["position"]["value"]
    for _ in range(60):
        p = runtime.observation.position.value
        dx, dz = origin["x"] - p.x, origin["z"] - p.z
        if math.hypot(dx, dz) < .4 and p.y >= origin["y"]: break
        yaw = math.degrees(math.atan2(-dx, dz))
        step("exit_shallow_hole", movement=MovementV1(forward=1, jump=True),
             look=LookV1((yaw - runtime.observation.yaw_degrees.value + 180) % 360 - 180, -runtime.observation.pitch_degrees.value))
    else: raise TimeoutError("normal jump did not exit the one-block mining hole")
    for _ in range(12): step("exit_release")
    inventory = runtime.observation.inventory.value
    slot = next((i for i, item in enumerate(inventory.main[:9]) if not item.empty and item.item_id == "minecraft:dirt"), None)
    if slot is None: raise RuntimeError("collected dirt is not in the observed hotbar; no inventory mutation fallback")
    step("select_collected_dirt", operation=SelectHotbarV1(slot))
    current = perception.aim_for('interact_block')
    if current is None:
        raise RuntimeError("no fresh block target for placement support")
    targeted, block = current
    support = targeted.block_position
    if targeted.face != "up" or block.block_id not in {"minecraft:grass_block", "minecraft:dirt"}:
        raise RuntimeError("no observed normal top-face placement support")
    mark("aimed_place")
    step("place_collected_dirt", operation=InteractBlockV1(*support, targeted.face),
         observation_request=INTERACTION); mark("placed")
    for _ in range(12): step("await_placement_sync", observation_request=INTERACTION)
    mark("confirmed")
    runtime.cancel("mining_probe_complete")
    step("final_cancel", expected="cancelled", observation_request=INTERACTION); mark("cancelled")
    return stages
