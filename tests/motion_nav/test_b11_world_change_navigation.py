from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
import unittest

from mc2p.contracts.action_receipt import behavior_receipt_from_mapping
from mc2p.contracts.action_v1 import ActionSnapshotV1, InteractBlockV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v2 import ItemStackV2
from mc2p.contracts.observation_v3 import TargetingStateV3
from mc2p.contracts.reset import ResetRequestV0, ResetResultV0
from mc2p.motion_nav.bridge_planner import BridgePlacementPolicy
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles
from mc2p.motion_nav.world_model import Aabb
from mc2p.runtime.backend_v1 import BackendStepResultV1
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.world_change_navigation_driver import RuntimeWorldChangeNavigationDriver
from tests.follow_v3_fixtures import follow_snapshot, observed_block
from tests.motion_nav.test_b07_step_route import step_profile
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_navigation_session import _InlinePlanner
from tests.test_action_receipt import receipt_value
from tests.test_player_runtime import _RecordingTrace


class _WorldChangeBackend:
    action_schema_version = "mc2p.action-snapshot.v1"
    observation_schema_version = "mc2p.client_observation.v3"

    def __init__(self, clock, *, gap_count=1, item_count=3):
        self.clock = clock
        self.gap_count = gap_count
        self.goal_x = gap_count + 1
        self.sequence = 0
        self.movement_tick = 1
        self.position_x = 0.5
        self.yaw = 0.0
        self.pitch = 0.0
        self.sneaking = False
        self.placed_cells: set[int] = set()
        self.external_cells: set[int] = set()
        self.item_count = item_count
        self.actions: list[ActionSnapshotV1] = []

    @property
    def placed(self):
        return bool(self.placed_cells)

    def _work_x(self):
        return min(self.gap_count, len(self.placed_cells))

    def _blocks(self, interaction: bool):
        values = []
        current = (self._work_x(), 63, 0)
        for x in range(-1, self.goal_x + 2):
            for z in range(-1, 2):
                for y in range(61, 68):
                    block_id = None
                    if (y == 63 and (z != 0 or x not in range(1, self.goal_x)
                                     or x in self.placed_cells
                                     or x in self.external_cells)):
                        block_id = (
                            "minecraft:dirt" if z == 0 and x in self.placed_cells
                            else "minecraft:stone" if z == 0 and x in self.external_cells
                            else "minecraft:stone"
                        )
                    elif z != 0 and y in (64, 65):
                        block_id = "minecraft:stone"
                    sources = ("first_hit_ray",) if block_id else ("air_query",)
                    if interaction and (x, y, z) == current:
                        sources = tuple(sorted(set(sources) | {"current_target"}))
                    values.append(observed_block(
                        (x, y, z),
                        block_id or "minecraft:air",
                        kind="full_cube" if block_id else "empty",
                        sources=sources,
                    ))
        return tuple(values)

    def observation(self, *, request_sequence_id, interaction: bool):
        work_x = self._work_x()
        target_visible = (
            interaction
            and abs(self.yaw - 90.0) <= 2.0
            and self.position_x >= work_x + 1.0
        )
        snapshot = follow_snapshot(
            sequence=self.sequence,
            received=self.clock[0],
            position=(self.position_x, 64.0, 0.5),
            yaw=self.yaw,
            pitch=self.pitch,
            episode="episode-b11-runtime",
            blocks=tuple(
                replace(block, sources=tuple(source for source in block.sources
                                              if source != "current_target"))
                for block in self._blocks(target_visible)
            ),
            entities=[],
            profile="interaction_v1" if interaction else "navigation_v1",
            self_changes={
                "movement_tick_id": self.movement_tick,
                "velocity": {"x": 0.0, "y": 0.0, "z": 0.0},
                "is_sneaking": self.sneaking,
                "pose": "crouching" if self.sneaking else "standing",
            },
        )
        empty = ItemStackV2(True, None, 0, 0, 0, False, None)
        held = (
            ItemStackV2(
                False, "minecraft:dirt", self.item_count, 0, 0, False, None,
            )
            if self.item_count > 0 else empty
        )
        inventory = replace(
            snapshot.inventory.value,
            main=(held,) + (empty,) * 35,
            selected_hotbar_slot=0,
            main_hand=held,
        )
        changes = {
            "request_sequence_id": request_sequence_id,
            "inventory": replace(snapshot.inventory, value=inventory),
        }
        if target_visible:
            current = (self._work_x(), 63, 0)
            face = "east"
            target = TargetingStateV3(
                "block", current, None, face,
                Vec3V0(float(current[0] + 1), 63.5, 0.5), 2.0,
            )
            blocks = tuple(
                replace(block, sources=tuple(sorted(set(block.sources) | {"current_target"})))
                if block.position == current else block
                for block in snapshot.perception.value.blocks
            )
            changes.update(
                targeting=replace(snapshot.targeting, value=target),
                perception=replace(
                    snapshot.perception,
                    value=replace(snapshot.perception.value, blocks=blocks),
                ),
            )
        return replace(snapshot, **changes)

    def reset(self, request):
        return ResetResultV0(
            request.request_id,
            request.episode_id,
            True,
            self.observation(request_sequence_id=None, interaction=False),
        )

    def step(self, action, deadline, *, observation_request=None):
        self.actions.append(action)
        operation = action.operation
        work_x = self._work_x()
        if (operation == InteractBlockV1(work_x, 63, 0, "east")
                and 1 <= work_x + 1 < self.goal_x):
            self.placed_cells.add(work_x + 1)
            self.item_count -= 1
        self.yaw = (self.yaw + action.look.yaw_delta_degrees + 180.0) % 360.0 - 180.0
        self.pitch = max(-90.0, min(90.0, self.pitch + action.look.pitch_delta_degrees))
        self.sneaking = action.movement.sneak
        active = action.movement != MovementV1()
        direction_x = (
            -math.sin(math.radians(self.yaw)) * action.movement.forward
            + math.cos(math.radians(self.yaw)) * action.movement.strafe
        )
        if abs(direction_x) > 1.0e-6:
            self.position_x = max(
                0.2,
                min(self.goal_x + 0.5,
                    self.position_x + math.copysign(0.13, direction_x)),
            )
        if active:
            self.movement_tick += 1
        self.sequence += 1
        self.clock[0] += 50_000_000
        interaction = observation_request is not None and observation_request.field_profile == "interaction_v1"
        observed = self.observation(
            request_sequence_id=action.request_sequence_id,
            interaction=interaction,
        )
        applications = ([{
            "schema_version": "mc2p.input-application.v1",
            "movement_tick_id": self.movement_tick,
            "episode_id": action.episode_id,
            "request_sequence_id": action.request_sequence_id,
            "sampled_at_jvm_ns": self.movement_tick,
            "state": "leased",
            "forward": float(action.movement.forward),
            "strafe": float(action.movement.strafe),
            "jump": action.movement.jump,
            "sneak": action.movement.sneak,
            "sprint": action.movement.sprint,
        }] if active else [])
        dispatched = operation is not None
        receipt = behavior_receipt_from_mapping({
            **receipt_value(
                episode_id=action.episode_id,
                generation_id=self.sequence,
                request_sequence_id=action.request_sequence_id,
                world_tick=observed.world_time_ticks.value,
                input_samples=self.movement_tick,
                leased_input_samples=1 if active else 0,
                status="pending_confirmation" if dispatched else "executed",
                reason="block_use_dispatched" if dispatched else "controls_applied",
            ),
            "schema_version": "mc2p.client_action_receipt.v3",
            "dropped_input_samples": 0,
            "oldest_retained_input_tick": self.movement_tick,
            "input_applications": applications,
        })
        return BackendStepResultV1(observed, 0.0, False, False, receipt)

    def close(self):
        pass


class WorldChangeNavigationIntegrationTests(unittest.TestCase):
    def _fixture(self, *, gap_count=1, item_count=3, maximum_blocks=3):
        clock = [100_000_000]
        backend = _WorldChangeBackend(
            clock, gap_count=gap_count, item_count=item_count,
        )
        runtime = PlayerRuntimeV1(backend, _RecordingTrace(), lambda: clock[0])
        reset = runtime.reset(ResetRequestV0(
            "reset-b11", "episode-b11-runtime", "test", 1, 10_000_000_000,
        ))
        self.assertTrue(reset.succeeded, reset.failure)
        self.addCleanup(runtime.close)
        profiles = replace(
            NavigationSessionProfiles.load(
                Path(__file__).resolve().parents[2] / "config/motion-navigation",
            ),
            air=(),
        )
        session = NavigationSession(
            "b11-runtime-session",
            profiles,
            planner_worker=_InlinePlanner(),
            bridge_policy=BridgePlacementPolicy(maximum_blocks=maximum_blocks),
            clock_ns=lambda: clock[0],
        )
        self.addCleanup(session.close)
        driver = RuntimeWorldChangeNavigationDriver(
            runtime, session, clock_ns=lambda: clock[0],
        )
        goal = GoalState(
            Aabb(
                backend.goal_x + 0.4, 63.95, 0.4,
                backend.goal_x + 0.6, 64.05, 0.6,
            ),
            GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),
            frozenset({"standing"}),
            0.6,
        )
        driver.start("goal-b11", 1, goal, clock[0])
        return clock, backend, driver

    def _run(self, driver, clock, limit=160):
        for _ in range(limit):
            driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
            if driver.report.terminal:
                break

    def test_one_gap_is_placed_confirmed_and_replanned_before_crossing(self):
        clock, backend, driver = self._fixture(maximum_blocks=1)
        self._run(driver, clock)
        self.assertEqual(driver.report.state, "success", driver.report)
        self.assertEqual(driver.report.confirmed_placements, 1)
        interactions = [
            action.operation for action in backend.actions
            if action.operation is not None
        ]
        self.assertEqual(interactions, [InteractBlockV1(0, 63, 0, "east")])
        operation_index = next(
            index for index, action in enumerate(backend.actions)
            if action.operation is not None
        )
        crossing_index = next(
            index for index, action in enumerate(backend.actions)
            if index > operation_index
            and (action.movement.forward != 0 or action.movement.strafe != 0)
        )
        self.assertLess(operation_index, crossing_index)
        self.assertTrue(backend.placed)
        driver.release()
        self.assertIsNone(driver.navigation.source)

    def test_three_gap_bridge_confirms_each_block_before_advancing(self):
        clock, backend, driver = self._fixture(
            gap_count=3, item_count=3, maximum_blocks=3,
        )
        self._run(driver, clock)
        self.assertEqual(driver.report.state, "success", driver.report)
        self.assertEqual(driver.report.confirmed_placements, 3)
        self.assertEqual(backend.placed_cells, {1, 2, 3})
        interactions = [
            action.operation for action in backend.actions
            if action.operation is not None
        ]
        self.assertEqual(interactions, [
            InteractBlockV1(0, 63, 0, "east"),
            InteractBlockV1(1, 63, 0, "east"),
            InteractBlockV1(2, 63, 0, "east"),
        ])
        driver.release()
        self.assertIsNone(driver.navigation.source)

    def test_known_insufficient_inventory_fails_before_partial_bridge(self):
        clock, backend, driver = self._fixture(
            gap_count=2, item_count=1, maximum_blocks=2,
        )
        self._run(driver, clock)
        self.assertEqual(driver.report.state, "failed", driver.report)
        self.assertEqual(driver.report.reason, "insufficient_bridge_materials")
        self.assertEqual(backend.placed_cells, set())
        self.assertFalse(any(
            action.operation is not None for action in backend.actions
        ))
        driver.release()

    def test_external_block_claiming_the_bridge_cell_replans_without_clicking(self):
        clock, backend, driver = self._fixture(maximum_blocks=1)
        for _ in range(20):
            driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
            if driver.report.state == "interaction_required":
                break
        self.assertEqual(driver.report.state, "interaction_required")
        backend.external_cells.add(1)

        self._run(driver, clock)

        self.assertEqual(driver.report.state, "success", driver.report)
        self.assertEqual(driver.report.confirmed_placements, 0)
        self.assertFalse(any(
            action.operation is not None for action in backend.actions
        ))
        driver.release()

    def test_goal_revision_reanchors_after_placement_advanced_runtime_world(self):
        clock, backend, driver = self._fixture(maximum_blocks=1)
        for _ in range(40):
            driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
            if driver.report.state == "interaction_required":
                break
        self.assertEqual(driver.report.state, "interaction_required")
        self.assertGreater(driver.runtime.observation.sequence_id, 0)

        revised_goal = GoalState(
            Aabb(
                backend.goal_x + 0.35, 63.95, 0.35,
                backend.goal_x + 0.65, 64.05, 0.65,
            ),
            GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),
            frozenset({"standing"}),
            0.6,
        )
        driver.navigation.replace_goal(
            "goal-b11", 2, revised_goal, clock[0],
        )

        self.assertEqual(driver.session.report.goal_revision, 2)


if __name__ == "__main__":
    unittest.main()
