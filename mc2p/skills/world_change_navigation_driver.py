"""B11 coordinator for navigation, one confirmed placement, and replanning."""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import (
    ContractViolation, FieldStatusV0, require_nonnegative_int,
)
from mc2p.motion_nav.movement_transition import GoalState
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.navigation_session import NavigationSession
from mc2p.motion_nav.world_interaction import (
    BlockPlacementTransaction,
    PlacementFailureKind,
    PlacementState,
)
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1, RuntimeStepResultV1
from mc2p.skills.block_placement_driver import RuntimeBlockPlacementDriver
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver


@dataclass(frozen=True, slots=True)
class WorldChangeNavigationReport:
    state: str
    reason: str
    confirmed_placements: int
    active_interaction_id: str | None
    terminal: bool


class RuntimeWorldChangeNavigationDriver:
    """Keep one final goal while placement temporarily owns the input source."""

    def __init__(
        self,
        runtime: PlayerRuntimeV1,
        session: NavigationSession,
        *,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        if type(runtime) is not PlayerRuntimeV1 or type(session) is not NavigationSession:
            raise ContractViolation("world-change navigation requires formal Runtime and session")
        self.runtime = runtime
        self.session = session
        self._clock = clock_ns
        self.navigation = RuntimeNavigationDriver(
            runtime, session, clock_ns=clock_ns,
        )
        self.placement: RuntimeBlockPlacementDriver | None = None
        self._active_interaction_id: str | None = None
        self._confirmed_placements = 0
        self._state = "ready"
        self._reason = "not_started"

    @property
    def report(self) -> WorldChangeNavigationReport:
        return WorldChangeNavigationReport(
            self._state,
            self._reason,
            self._confirmed_placements,
            self._active_interaction_id,
            self._state in {"success", "failed", "cancelled"},
        )

    def start(
        self,
        goal_id: str,
        goal_revision: int,
        goal: GoalState,
        now_ns: int,
    ) -> None:
        if self._state != "ready":
            raise ContractViolation("world-change navigation already started")
        self.navigation.start(goal_id, goal_revision, goal, now_ns)
        self._sync_navigation()

    def tick(
        self,
        profile: BehaviorProfileV0,
        owner_deadline_ns: int,
    ) -> RuntimeStepResultV1 | None:
        if type(profile) is not BehaviorProfileV0:
            raise ContractViolation("world-change navigation requires a behavior profile")
        require_nonnegative_int(owner_deadline_ns, "world-change owner deadline")
        if self.report.terminal or self._state == "ready":
            raise ContractViolation("world-change navigation cannot tick")
        if self.placement is not None:
            return self._tick_placement(profile, owner_deadline_ns)
        if self.navigation.state == "interaction_required":
            interaction = self.session.required_interaction
            if interaction is None:
                raise ContractViolation("navigation lost its required interaction")
            inventory_reason = self._inventory_reason(
                interaction.required_placements,
                interaction.requirement.expected_item_id,
            )
            if inventory_reason is not None:
                self.navigation.release(inventory_reason)
                self._state = "failed"
                self._reason = inventory_reason
                return None
            self.navigation.suspend_for_interaction()
            transaction = BlockPlacementTransaction(interaction.requirement)
            modes = self.session.profiles.ground_modes
            approach_mode = (
                None if modes is None else modes.require(MovementMode.CROUCH)
            )
            self.placement = RuntimeBlockPlacementDriver(
                self.runtime, transaction, approach_mode=approach_mode,
                clock_ns=self._clock,
            )
            self.placement.start()
            self._active_interaction_id = interaction.requirement.interaction_id
            self._state = "placing"
            self._reason = "world_interaction_started"
            return self._tick_placement(profile, owner_deadline_ns)
        result = self.navigation.tick(profile, owner_deadline_ns)
        self._sync_navigation()
        return result

    def cancel(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ContractViolation("world-change cancellation reason is required")
        if self.placement is not None:
            self.placement.cancel(reason)
            self.placement = None
        if not self.session.report.terminal:
            self.session.cancel(reason)
        self._state = "cancelled"
        self._reason = reason.strip()

    def release(self) -> None:
        """Release any remaining Runtime source after a terminal result."""
        if not self.report.terminal:
            raise ContractViolation("active world-change navigation cannot be released")
        if self.placement is not None:
            self.placement.release()
            self.placement = None
        if self.navigation.source is not None:
            if not self.navigation.release("world_change_navigation_finished"):
                raise ContractViolation(
                    "world-change navigation still owns an unfinished body action"
                )

    def _sync_navigation(self) -> None:
        mapping = {
            "success": "success",
            "failed": "failed",
            "cancelled": "cancelled",
            "interaction_required": "interaction_required",
        }
        self._state = mapping.get(self.navigation.state, "navigating")
        self._reason = self.navigation.reason

    def _tick_placement(
        self,
        profile: BehaviorProfileV0,
        owner_deadline_ns: int,
    ) -> RuntimeStepResultV1 | None:
        placement = self.placement
        if placement is None:
            raise ContractViolation("world-change placement is missing")
        result = placement.tick(profile, owner_deadline_ns)
        placement_report = placement.transaction.report
        if placement_report.state is PlacementState.COMPLETE:
            assert self._active_interaction_id is not None
            self.session.confirm_required_interaction(
                self._active_interaction_id,
            )
            self._confirmed_placements += 1
            self.placement = None
            self._active_interaction_id = None
            self.navigation.resume_after_interaction()
            self._sync_navigation()
        elif placement_report.state in {
            PlacementState.FAILED,
            PlacementState.CANCELLED,
        }:
            dependency_changed = (
                placement_report.failure_kind
                is PlacementFailureKind.WORLD_DEPENDENCY_CHANGED
            )
            placement.release()
            self.placement = None
            self._active_interaction_id = None
            if dependency_changed:
                # The interaction became stale because the real world
                # changed.  Rebind navigation so its sole world owner can
                # ingest that observation and decide whether the new fact
                # opens a route or blocks it.
                self.navigation.resume_after_interaction()
                self._sync_navigation()
            else:
                self._fail(placement_report.reason)
        else:
            self._state = "placing"
            self._reason = placement_report.reason
        return result

    def _inventory_reason(
        self,
        required_count: int,
        expected_item_id: str,
    ) -> str | None:
        inventory = self.runtime.observation.inventory
        if inventory.status is not FieldStatusV0.VALID or inventory.value is None:
            return None
        held = inventory.value.main_hand
        if held.empty or held.item_id != expected_item_id:
            return "required_bridge_item_not_in_main_hand"
        if held.count < required_count:
            return "insufficient_bridge_materials"
        return None

    def _fail(self, reason: str) -> None:
        if not self.session.report.terminal:
            self.session.cancel(reason)
        self._state = "failed"
        self._reason = reason
