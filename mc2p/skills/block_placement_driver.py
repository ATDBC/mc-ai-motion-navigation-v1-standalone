"""Runtime adapter for one B11 observation-confirmed block placement."""
from __future__ import annotations

import json
import math
import time
from typing import Callable

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, LookV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.intent_source import (
    ControlFrameProposalV1,
    IntentSourceV1,
    OrderedIntentV1,
    ordered_intent_id,
)
from mc2p.contracts.task import (
    ComparisonOperatorV0,
    SuccessCriterionV0,
    TaskIntentV0,
)
from mc2p.motion_nav.fixed_route import (
    FixedRoute, FixedRouteConfig, FixedRouteController, FixedRouteState,
    RoutePoint,
)
from mc2p.motion_nav.ground_modes import GroundModeProfile
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.world_interaction import (
    BlockPlacementTransaction,
    PlacementProposal,
)
from mc2p.runtime.player_runtime_v1 import (
    PlayerRuntimeV1,
    RuntimeStateV1,
    RuntimeStepResultV1,
)


_STEP_WINDOW_NS = 500_000_000


class RuntimeBlockPlacementDriver:
    """Contribute one placement through Runtime's existing ordered boundary."""

    def __init__(
        self,
        runtime: PlayerRuntimeV1,
        transaction: BlockPlacementTransaction,
        *,
        approach_mode: GroundModeProfile | None = None,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        if type(runtime) is not PlayerRuntimeV1:
            raise ContractViolation("block placement driver requires formal Runtime")
        if type(transaction) is not BlockPlacementTransaction:
            raise ContractViolation("block placement driver requires a transaction")
        if approach_mode is not None and type(approach_mode) is not GroundModeProfile:
            raise ContractViolation("block placement approach mode must be typed")
        if (transaction.requirement.requires_sneak
                and (approach_mode is None
                     or approach_mode.mode is not MovementMode.CROUCH)):
            raise ContractViolation(
                "edge placement requires the calibrated crouch mode"
            )
        if runtime.state is not RuntimeStateV1.READY:
            raise ContractViolation("block placement driver requires ready Runtime")
        self.runtime = runtime
        self.transaction = transaction
        self._clock = clock_ns
        self._approach_mode = approach_mode
        self._approach: FixedRouteController | None = None
        self._last_movement_confirmed = True
        self.source: IntentSourceV1 | None = None
        self._sequence = 0
        self._prepared: PlacementProposal | None = None
        self._prepared_intent_id: str | None = None
        self._prepared_deadline_ns: int | None = None

    def start(self) -> None:
        if self.source is not None or self.transaction.report.terminal:
            raise ContractViolation("block placement driver cannot start")
        self.source = self.runtime.register_ordered_source("block-placement")

    @property
    def prepared_placement(self) -> PlacementProposal | None:
        """Expose the immutable proposal for evidence and controlled tests."""
        return self._prepared

    def prepare_proposal(self, owner_deadline_ns: int) -> ControlFrameProposalV1:
        if self.source is None or self._prepared is not None:
            raise ContractViolation("block placement driver is not ready to prepare")
        require_nonnegative_int(owner_deadline_ns, "placement owner deadline")
        now = self._clock()
        deadline = min(owner_deadline_ns, now + _STEP_WINDOW_NS)
        if deadline <= now:
            raise ContractViolation("block placement action window expired")
        observation = self.runtime.observation
        frame = self.runtime.navigation_observation_adapter.latest_frame
        if frame is None:
            raise ContractViolation("block placement requires current world frame")
        placement = self.transaction.propose(observation, frame)
        intents: tuple[OrderedIntentV1, ...] = ()
        intent_id = None
        movement = self._movement_for(placement, frame)
        look = (
            self._aim_command()
            if placement.operation is None
            and placement.reason == "target_not_aligned"
            else None
        )
        if (placement.operation is not None or look is not None
                or movement != MovementV1()):
            self.runtime.cancel_source(self.source.source_id)
            self._sequence += 1
            intent_id = ordered_intent_id(self.source, self._sequence)
            intent = ActionIntentV1(
                intent_id=intent_id,
                source_id=self.source.source_id,
                episode_id=observation.episode_id,
                observation_sequence_id=observation.sequence_id,
                priority=ActionPriorityV0.TASK,
                submitted_at_monotonic_ns=now,
                expires_at_monotonic_ns=deadline,
                movement=movement,
                look=look,
                operation=placement.operation,
            )
            intents = (OrderedIntentV1(self.source, self._sequence, intent),)
        self._prepared = placement
        self._prepared_intent_id = intent_id
        self._prepared_deadline_ns = deadline
        return ControlFrameProposalV1(
            intents=intents,
            observation_request=placement.observation_request,
        )

    def _movement_for(
        self,
        placement: PlacementProposal,
        frame: NavigationFrame,
    ) -> MovementV1:
        requirement = self.transaction.requirement
        if not requirement.requires_sneak:
            return MovementV1()
        if placement.reason == "work_position_not_reached":
            if self._approach is None:
                assert self._approach_mode is not None
                self._approach = FixedRouteController(
                    self._approach_mode.motion,
                    FixedRouteConfig(
                        endpoint_tolerance_blocks=requirement.work_position_tolerance,
                        preferred_support_fraction=0.30,
                        maximum_cross_track_blocks=0.20,
                        lookahead_min_blocks=0.20,
                        lookahead_max_blocks=0.50,
                        corner_braking_lookahead_blocks=0.50,
                    ),
                    mode_profile=self._approach_mode,
                )
                self._approach.start(FixedRoute(
                    requirement.interaction_id + "/edge-approach",
                    (
                        RoutePoint(*frame.body.position),
                        RoutePoint(*requirement.work_position),
                    ),
                ), frame)
            decision = self._approach.decide(
                frame, input_confirmed=self._last_movement_confirmed,
            )
            if decision.state in {
                FixedRouteState.BLOCKED,
                FixedRouteState.INPUT_LOST,
                FixedRouteState.FAILED,
                FixedRouteState.UNSUPPORTED,
            }:
                self.transaction.fail("edge_approach_" + decision.reason)
                return MovementV1()
            return decision.movement
        return MovementV1(sneak=True)

    def _aim_command(self) -> LookV1:
        observation = self.runtime.observation
        own = observation.self_state.value
        if own is None:
            return LookV1()
        requirement = self.transaction.requirement
        x, y, z = requirement.support
        face_point = {
            "down": (x + 0.5, y, z + 0.5),
            "up": (x + 0.5, y + 1.0, z + 0.5),
            "north": (x + 0.5, y + 0.5, z),
            "south": (x + 0.5, y + 0.5, z + 1.0),
            "west": (x, y + 0.5, z + 0.5),
            "east": (x + 1.0, y + 0.5, z + 0.5),
        }[requirement.face]
        eye_height = own.eye_height_blocks or 1.62
        dx = face_point[0] - own.position.x
        dy = face_point[1] - (own.position.y + eye_height)
        dz = face_point[2] - own.position.z
        horizontal = math.hypot(dx, dz)
        if horizontal <= 1.0e-9:
            desired_yaw = own.yaw_degrees
        else:
            desired_yaw = math.degrees(math.atan2(-dx, dz))
        desired_pitch = -math.degrees(math.atan2(dy, max(horizontal, 1.0e-9)))
        yaw_error = (desired_yaw - own.yaw_degrees + 180.0) % 360.0 - 180.0
        pitch_error = max(-90.0, min(90.0, desired_pitch)) - own.pitch_degrees
        return LookV1(
            max(-15.0, min(15.0, yaw_error)),
            max(-10.0, min(10.0, pitch_error)),
        )

    def adopt_result(self, result: RuntimeStepResultV1) -> None:
        if self._prepared is None or self._prepared_deadline_ns is None:
            raise ContractViolation("block placement driver has no prepared proposal")
        if type(result) is not RuntimeStepResultV1:
            raise ContractViolation("block placement Runtime result is invalid")
        proposal = self._prepared
        intent_id = self._prepared_intent_id
        self._prepared = None
        self._prepared_intent_id = None
        self._prepared_deadline_ns = None
        selected_movement = (
            intent_id is not None
            and result.decision is not None
            and ("movement", intent_id) in result.decision.selected_intents
        )
        self._last_movement_confirmed = (
            intent_id is None or selected_movement
        )
        if proposal.operation is None:
            return
        selected = (
            result.decision is not None
            and ("operation", intent_id) in result.decision.selected_intents
        )
        receipt = result.backend_result.receipt if result.backend_result is not None else None
        if receipt is None:
            raise ContractViolation("placement dispatch lacks a client receipt")
        self.transaction.register_dispatch(
            proposal,
            selected=selected,
            receipt_status=receipt.status,
            receipt_reason=receipt.reason,
            control_sequence=result.decision.action.request_sequence_id,
        )

    def tick(
        self,
        profile: BehaviorProfileV0,
        owner_deadline_ns: int,
    ) -> RuntimeStepResultV1 | None:
        if type(profile) is not BehaviorProfileV0:
            raise ContractViolation("block placement tick requires behavior profile")
        proposal = self.prepare_proposal(owner_deadline_ns)
        if self.transaction.report.terminal:
            self.discard_prepared()
            self.release()
            return None
        assert self._prepared_deadline_ns is not None
        result = self.runtime.control_frame(
            self._task(self._prepared_deadline_ns),
            profile,
            self._prepared_deadline_ns,
            proposals=(proposal,),
        )
        self.adopt_result(result)
        return result

    def discard_prepared(self) -> None:
        if self._prepared is None:
            raise ContractViolation("block placement driver has no prepared proposal")
        self._prepared = None
        self._prepared_intent_id = None
        self._prepared_deadline_ns = None

    def cancel(self, reason: str) -> None:
        if self._prepared is not None:
            raise ContractViolation("prepared placement must be adopted or discarded")
        self.transaction.cancel(reason)
        self.release()

    def release(self) -> None:
        source = self.source
        if source is None:
            return
        if self.runtime.state is RuntimeStateV1.READY:
            self.runtime.cancel_source(source.source_id)
            self.runtime.unregister_ordered_source(source)
        self.source = None

    def _task(self, deadline_ns: int) -> TaskIntentV0:
        requirement = self.transaction.requirement
        return TaskIntentV0(
            task_id="block-placement-runtime",
            task_type="place_block",
            parameters_json=json.dumps({
                "interaction_id": requirement.interaction_id,
                "request_id": requirement.request_id,
                "goal_id": requirement.goal_id,
                "goal_revision": requirement.goal_revision,
            }, sort_keys=True, separators=(",", ":")),
            success_criteria=(SuccessCriterionV0(
                "block_placement_confirmed",
                ComparisonOperatorV0.EQUAL,
                1,
                "boolean",
            ),),
            priority=100,
            deadline_monotonic_ns=deadline_ns,
            interruptible=True,
            max_risk=0.0,
        )
