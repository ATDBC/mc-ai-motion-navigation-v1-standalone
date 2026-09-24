"""C1-A wrapper: fixed stand-off navigation followed by one shared strike."""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Callable

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation, FieldStatusV0, require_nonnegative_int
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.observation_v2 import VisibleEntityV2
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.motion_nav.navigation_session import NavigationSessionPort
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1, RuntimeStateV1, RuntimeStepResultV1
from mc2p.skills.fixed_melee import (
    CombatTargetV1, MAX_COARSE_ATTACK_DISTANCE_BLOCKS,
    combat_standoff_goal_state,
)
from mc2p.skills.melee_strike_driver import MeleeStrikeDriver, TASK_LIMIT_NS
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver


@dataclass(frozen=True, slots=True)
class FixedMeleeReportV1:
    state: str
    reason: str
    target_revision: int
    attack_submitted: bool
    hit_observed: bool
    attack_submissions: int
    terminal: bool
    schema_version: str = "mc2p.fixed-melee-report.v1"


class FixedMeleeDriver:
    def __init__(
        self,
        runtime: PlayerRuntimeV1,
        navigation_session: NavigationSessionPort,
        *,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        if (type(runtime) is not PlayerRuntimeV1
                or not isinstance(navigation_session, NavigationSessionPort)):
            raise ContractViolation("fixed melee driver requires Runtime/navigation session")
        if runtime.state is not RuntimeStateV1.READY or type(runtime.observation) is not ObservationSnapshotV3:
            raise ContractViolation("fixed melee driver requires ready V3 Runtime")
        self.runtime = runtime
        self.navigation_session = navigation_session
        self._clock = clock_ns
        self._target: CombatTargetV1 | None = None
        self._task_deadline_ns = 0
        self._state, self._reason = "ready", "not_started"
        self.approach_driver: RuntimeNavigationDriver | None = None
        self.strike_driver: MeleeStrikeDriver | None = None

    @property
    def report(self) -> FixedMeleeReportV1:
        if self.strike_driver is not None:
            report = self.strike_driver.report
            return FixedMeleeReportV1(
                report.state, report.reason, report.target_revision,
                report.attack_submitted, report.hit_observed,
                report.attack_submissions, report.terminal,
            )
        revision = 0 if self._target is None else self._target.revision
        return FixedMeleeReportV1(
            self._state, self._reason, revision, False, False, 0,
            self._state in {"complete", "failed", "cancelled"},
        )

    @staticmethod
    def _visible_target(observation: ObservationSnapshotV3,
                        track_id: str) -> VisibleEntityV2 | None:
        if (observation.perception.status is not FieldStatusV0.VALID
                or observation.perception.value is None):
            return None
        return next((entity for entity in observation.perception.value.visible_entities
                     if entity.track_id == track_id), None)

    def _start_strike(self, now_ns: int) -> None:
        assert self._target is not None
        self.strike_driver = MeleeStrikeDriver(
            self.runtime, clock_ns=self._clock,
            task_deadline_ns=self._task_deadline_ns,
        )
        self.strike_driver.start(self._target, now_ns)
        self._state, self._reason = (
            self.strike_driver.report.state, self.strike_driver.report.reason,
        )

    def _prepare_target(self, now_ns: int) -> None:
        assert self._target is not None
        observation = self.runtime.observation
        assert type(observation) is ObservationSnapshotV3
        entity = self._visible_target(observation, self._target.track_id)
        distance = None if entity is None else math.hypot(
            entity.relative_position.x, entity.relative_position.z,
        )
        if distance is not None and distance > MAX_COARSE_ATTACK_DISTANCE_BLOCKS:
            if observation.field_profile != "navigation_v1":
                self._state, self._reason = "failed", "navigation_observation_required"
                return
            own = observation.self_state.value
            if own is None:
                self._state, self._reason = "failed", "self_state_unavailable"
                return
            _, goal = combat_standoff_goal_state(
                own.position, entity.relative_position,
            )
            if self.approach_driver is None:
                self.approach_driver = RuntimeNavigationDriver(
                    self.runtime, self.navigation_session, clock_ns=self._clock,
                    observation_request=ObservationRequestV3(
                        "navigation_v1", entity_track_id=self._target.track_id,
                    ),
                )
                self.approach_driver.start(
                    self._target.goal_id, self._target.revision, goal, now_ns,
                )
            else:
                self.approach_driver.replace_goal(
                    self._target.goal_id, self._target.revision, goal, now_ns,
                )
            self._state, self._reason = "approaching", "outside_stable_attack_distance"
        else:
            if self.approach_driver is not None:
                self.approach_driver.release("target_entered_attack_range")
                self.approach_driver = None
            self._start_strike(now_ns)

    def start(self, target: CombatTargetV1, now_ns: int) -> None:
        if type(target) is not CombatTargetV1:
            raise ContractViolation("fixed melee start requires CombatTargetV1")
        require_nonnegative_int(now_ns, "fixed melee start time")
        if self._target is not None or self._state != "ready":
            raise ContractViolation("fixed melee driver already started")
        observation = self.runtime.observation
        if (type(observation) is not ObservationSnapshotV3
                or target.episode_id != observation.episode_id
                or now_ns < observation.received_at_monotonic_ns):
            raise ContractViolation("fixed melee target/session mismatch")
        self._target = target
        self._task_deadline_ns = now_ns + TASK_LIMIT_NS
        self._prepare_target(now_ns)

    def replace_target(self, target: CombatTargetV1, now_ns: int) -> None:
        if type(target) is not CombatTargetV1:
            raise ContractViolation("replacement requires CombatTargetV1")
        require_nonnegative_int(now_ns, "target replacement time")
        if self._target is None or self.report.terminal:
            raise ContractViolation("fixed melee driver is not replaceable")
        if (target.task_id != self._target.task_id or target.goal_id != self._target.goal_id
                or target.episode_id != self._target.episode_id
                or target.revision <= self._target.revision):
            raise ContractViolation("target replacement identity/revision mismatch")
        if self.strike_driver is not None:
            submitted = self.strike_driver.report.attack_submitted
            self.strike_driver.replace_target(target, now_ns)
            if not submitted:
                self._target = target
            self._state, self._reason = (
                self.strike_driver.report.state, self.strike_driver.report.reason,
            )
            return
        self._target = target
        self._prepare_target(now_ns)

    def tick(self, profile: BehaviorProfileV0,
             owner_deadline_ns: int) -> RuntimeStepResultV1 | None:
        if type(profile) is not BehaviorProfileV0:
            raise ContractViolation("fixed melee tick requires BehaviorProfileV0")
        if self._target is None:
            raise ContractViolation("fixed melee driver has not started")
        if self.report.terminal:
            raise ContractViolation("fixed melee driver is terminal")
        if self.approach_driver is not None:
            if self.approach_driver.state == "success":
                self.approach_driver.release("combat_standoff_reached")
                self.approach_driver = None
                self._start_strike(self._clock())
                return self.strike_driver.tick(profile, owner_deadline_ns)
            result = self.approach_driver.tick(profile, owner_deadline_ns)
            if self.approach_driver.state == "success":
                self.approach_driver.release("combat_standoff_reached")
                self.approach_driver = None
                self._start_strike(self._clock())
                return result
            if self.approach_driver.state in {"failed", "cancelled", "stopped", "blocked"}:
                reason = "approach/" + str(self.approach_driver.reason)
                if self.approach_driver.source is not None:
                    self.approach_driver.release(reason)
                self.approach_driver = None
                self._state, self._reason = "failed", reason
                return result
            return result
        assert self.strike_driver is not None
        result = self.strike_driver.tick(profile, owner_deadline_ns)
        self._state, self._reason = (
            self.strike_driver.report.state, self.strike_driver.report.reason,
        )
        return result

    def cancel(self, profile: BehaviorProfileV0,
               reason: str) -> RuntimeStepResultV1 | None:
        if type(profile) is not BehaviorProfileV0 or type(reason) is not str or not reason.strip():
            raise ContractViolation("fixed melee cancel requires profile and reason")
        if self._target is None or self.report.terminal:
            raise ContractViolation("fixed melee driver cannot be cancelled")
        if self.approach_driver is not None:
            result = self.approach_driver.stop(profile, reason)
            self.approach_driver = None
            self._state, self._reason = "cancelled", "cancelled_before_submit"
            return result
        assert self.strike_driver is not None
        result = self.strike_driver.cancel(profile, reason)
        self._state, self._reason = (
            self.strike_driver.report.state, self.strike_driver.report.reason,
        )
        return result
