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
from mc2p.contracts.observation_request_v3 import (
    MAX_AIR_QUERY_POSITIONS,
    ObservationRequestV3,
    merge_observation_requests,
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
from mc2p.motion_nav.execution_supervisor import ExecutionSupervisor
from mc2p.motion_nav.body_control import StopCause
from mc2p.motion_nav.motion_residual import MotionResidualTracker
from mc2p.motion_nav.body_control import HandoffDisposition
from mc2p.motion_nav.world_interaction import (
    BlockPlacementTransaction,
    PlacementProposal,
    PlacementState,
)
from mc2p.runtime.player_runtime_v1 import (
    PlayerRuntimeV1,
    RuntimeStateV1,
    RuntimeStepResultV1,
)


_STEP_WINDOW_NS = 500_000_000
_DEFAULT_PREPARATION_TIMEOUT_OBSERVATIONS = 120
_DEFAULT_SELECTION_TIMEOUT_OBSERVATIONS = 20


class RuntimeBlockPlacementDriver:
    """Contribute one placement through Runtime's existing ordered boundary."""

    def __init__(
        self,
        runtime: PlayerRuntimeV1,
        transaction: BlockPlacementTransaction,
        *,
        approach_mode: GroundModeProfile | None = None,
        preparation_timeout_observations: int = (
            _DEFAULT_PREPARATION_TIMEOUT_OBSERVATIONS
        ),
        selection_timeout_observations: int = (
            _DEFAULT_SELECTION_TIMEOUT_OBSERVATIONS
        ),
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
        for value, name in (
            (preparation_timeout_observations, "placement preparation timeout"),
            (selection_timeout_observations, "placement selection timeout"),
        ):
            if type(value) is not int or not 1 <= value <= 1_200:
                raise ContractViolation(f"{name} must be within 1..1200 observations")
        self.runtime = runtime
        self.transaction = transaction
        transaction.bind_clock(runtime.monotonic_ns)
        self._clock = clock_ns
        self._approach_mode = approach_mode
        self._approach: FixedRouteController | None = None
        self._approach_missing_cells: tuple[tuple[int, int, int], ...] = ()
        self._approach_waiting_for_information = False
        self._last_movement_confirmed = True
        self.source: IntentSourceV1 | None = None
        self._sequence = 0
        self._prepared: PlacementProposal | None = None
        self._prepared_intent_id: str | None = None
        self._prepared_deadline_ns: int | None = None
        self._preparation_timeout_observations = preparation_timeout_observations
        self._selection_timeout_observations = selection_timeout_observations
        self._preparation_started_sequence: int | None = None
        self._selection_started_sequence: int | None = None
        self._body_supervisor = ExecutionSupervisor()
        self._release_anchor = MotionResidualTracker()
        self._stop_cause: StopCause | None = None

    def start(self) -> None:
        if self.source is not None or self.transaction.report.terminal:
            raise ContractViolation("block placement driver cannot start")
        self.source = self.runtime.register_ordered_source("block-placement")

    @property
    def prepared_placement(self) -> PlacementProposal | None:
        """Expose the immutable proposal for evidence and controlled tests."""
        return self._prepared

    @property
    def has_prepared_frame(self) -> bool:
        return self._prepared is not None

    @property
    def body_release_evidence(self):
        """Read the supervisor's latest evidence; callers cannot authorize release."""
        return self._body_supervisor.last_handoff

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
        ready = self.transaction.report.state is PlacementState.READY
        preparing = ready and self._selection_started_sequence is None
        if preparing and self._preparation_started_sequence is None:
            self._preparation_started_sequence = observation.sequence_id
        preparation_age = (
            None if self._preparation_started_sequence is None
            else observation.sequence_id - self._preparation_started_sequence
        )
        if (preparing and preparation_age is not None
                and preparation_age >= self._preparation_timeout_observations):
            self.transaction.fail("preparation_timeout")
        placement = self.transaction.propose(observation, frame)
        selection_age = (
            None if self._selection_started_sequence is None
            else observation.sequence_id - self._selection_started_sequence
        )
        if placement.operation is None:
            self._selection_started_sequence = None
        elif (selection_age is not None
              and selection_age >= self._selection_timeout_observations):
            self.transaction.fail("operation_selection_timeout")
            placement = self.transaction.propose(observation, frame)
        intents: tuple[OrderedIntentV1, ...] = ()
        intent_id = None
        movement = self._movement_for(placement, frame)
        observation_request = self._observation_request(
            placement.observation_request,
        )
        look = (
            self._aim_command()
            if placement.operation is None
            and placement.reason == "target_not_aligned"
            else (
                self._look_toward_cell(self._approach_missing_cells[0])
                if (self._approach_missing_cells
                    and self._approach_waiting_for_information) else None
            )
        )
        if (placement.operation is not None or look is not None
                or movement != MovementV1() or self.transaction.report.terminal):
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
            observation_request=observation_request,
        )

    def _observation_request(
        self,
        placement_request: ObservationRequestV3,
    ) -> ObservationRequestV3:
        if not self._approach_missing_cells:
            return placement_request
        existing = set(placement_request.air_positions)
        available = MAX_AIR_QUERY_POSITIONS - len(existing)
        additions = tuple(
            position
            for position in self._approach_missing_cells
            if position not in existing
        )[:available]
        if not additions:
            return placement_request
        return merge_observation_requests((
            placement_request,
            ObservationRequestV3("interaction_v1", additions),
        ))

    def _movement_for(
        self,
        placement: PlacementProposal,
        frame: NavigationFrame,
    ) -> MovementV1:
        self._approach_missing_cells = ()
        self._approach_waiting_for_information = False
        if self.transaction.report.terminal:
            if (self.transaction.report.state is PlacementState.COMPLETE
                    and self._stop_cause is None
                    and self._release_evidence(frame).disposition is HandoffDisposition.QUIESCENT):
                # Successful placement resumes standing navigation. A proven
                # neutral tail lets the current owner release sneak first.
                return MovementV1()
            return MovementV1(sneak=frame.body.is_on_ground)
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
            self._approach_missing_cells = decision.missing_cells
            self._approach_waiting_for_information = (
                decision.state is FixedRouteState.NEEDS_INFORMATION
            )
            if (decision.state in {
                    FixedRouteState.BLOCKED,
                    FixedRouteState.INPUT_LOST,
                    FixedRouteState.FAILED,
                } and frame.body.is_on_ground):
                # A push or a delayed command can move the body outside the
                # old short approach corridor. Re-anchor from the observed
                # grounded state; the unchanged preparation deadline still
                # bounds repeated recovery and sneak keeps the edge guarded.
                self._approach = None
                return MovementV1(sneak=True)
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
        return self._look_toward_point(face_point)

    def _look_toward_cell(self, position: tuple[int, int, int]) -> LookV1:
        return self._look_toward_point(tuple(axis + 0.5 for axis in position))

    def _look_toward_point(self, point: tuple[float, float, float]) -> LookV1:
        observation = self.runtime.observation
        own = observation.self_state.value
        if own is None:
            return LookV1()
        x, y, z = point
        eye_height = own.eye_height_blocks or 1.62
        dx = x - own.position.x
        dy = y - (own.position.y + eye_height)
        dz = z - own.position.z
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
            self._selection_started_sequence = None
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
            dispatched_monotonic_ns=result.dispatched_monotonic_ns,
        )
        if selected:
            self._preparation_started_sequence = None
            self._selection_started_sequence = None
        elif self._selection_started_sequence is None:
            self._selection_started_sequence = proposal.observation_sequence

    def tick(
        self,
        profile: BehaviorProfileV0,
        owner_deadline_ns: int,
    ) -> RuntimeStepResultV1 | None:
        if type(profile) is not BehaviorProfileV0:
            raise ContractViolation("block placement tick requires behavior profile")
        proposal = self.prepare_proposal(owner_deadline_ns)
        if self.transaction.report.terminal:
            if self.release():
                self.discard_prepared()
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
        if not isinstance(reason, str) or not reason.strip():
            raise ContractViolation("placement cancellation reason is required")
        self._stop_cause = StopCause.CANCELLED
        # A terminal business result can still own unfinished body control.
        # Repeated stop requests must not rewrite the confirmed transaction.
        if not self.transaction.report.terminal:
            self.transaction.cancel(reason)
        if self.source is None:
            return
        if self._approach is not None:
            self._approach.cancel()
        self.release()

    def release(self) -> bool:
        source = self.source
        if source is None:
            return True
        if self.runtime.state is RuntimeStateV1.READY:
            frame = self.runtime.navigation_observation_adapter.latest_frame
            if frame is None:
                return False
            # Derive release state from the current formal body observation.
            # Input uncertainty remains owned by Runtime's authoritative ledger.
            evidence = self._release_evidence(frame)
            if evidence.disposition is not HandoffDisposition.QUIESCENT:
                self.runtime.cancel_source(source.source_id)
                return False
            if (self.transaction.report.state is PlacementState.COMPLETE
                    and self._stop_cause is None
                    and frame.body.pose != "standing"):
                self.runtime.cancel_source(source.source_id)
                return False
            self.runtime.cancel_source(source.source_id)
            self.runtime.unregister_ordered_source(source)
        self.source = None
        return True

    def _release_evidence(self, frame):
        self._release_anchor.reset()
        self._release_anchor.observe(self.runtime.observation, frame, self.runtime.input_ledger)
        return self._body_supervisor.evaluate_quiescence(
            frame, self.runtime.input_ledger, self._release_anchor.anchor)

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
