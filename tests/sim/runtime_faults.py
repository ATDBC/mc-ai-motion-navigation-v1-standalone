"""Reusable formal Runtime scenarios for backend loss and world changes."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.observation_v2 import ObservationGroupV2
from mc2p.contracts.observation_v3 import TrackedEntityStateV3
from mc2p.contracts.report import FailureCodeV0, FailureV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles
from mc2p.runtime.backend_v1 import BackendIOFailure
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.known_world_follow_driver import KnownWorldFollowDriver
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver, RuntimeNavigationDriverState
from tests.sim.backend import CalculatorBackend, Scene
from tests.sim.runner import CONFIG, InlinePlannerWorker, InlineMotionWorker, Scenario, _goal, seed_memory
from tests.sim.scenarios import SCENARIOS
from tests.test_player_runtime import _RecordingTrace

TRACK = "player-target-1"
TERMINAL = {RuntimeNavigationDriverState.SUCCESS, RuntimeNavigationDriverState.FAILED,
            RuntimeNavigationDriverState.CANCELLED, RuntimeNavigationDriverState.STOPPED,
            RuntimeNavigationDriverState.INTERACTION_REQUIRED}


def gap_scenario():
    solids = {(x, 63, z): "minecraft:grass_block"
              for x in range(-2, 3) for z in range(-2, 10) if z != 3}
    return Scenario("io-gap", Scene(solids, ((-2, 2), (60, 70), (-2, 9))),
                    (.55, 64., .65), (.5, 64., 8.5), max_ticks=200)


class FaultBackend(CalculatorBackend):
    failure = None
    attempted_writes = 0
    target = (.5, 64., 8.5)

    def step(self, *args, **kwargs):
        self.attempted_writes += 1
        if self.failure is not None:
            raise self.failure
        return super().step(*args, **kwargs)

    def observation(self, **kwargs):
        snapshot = super().observation(**kwargs)
        own = snapshot.self_state.value.position
        relative = tuple(t - p for t, p in zip(self.target, (own.x, own.y, own.z)))
        perception = snapshot.perception.value
        entity = replace(perception.visible_entities[0], track_id=TRACK,
                         entity_type="minecraft:player", relative_position=Vec3V0(*relative))
        tracked = TrackedEntityStateV3(
            TRACK, "minecraft:player", Vec3V0(*relative), Vec3V0(0., 0., 0.),
            0., 0., Vec3V0(.6, 1.8, .6), "standing", True, True, False, 20., 20.,
        )
        return replace(snapshot,
            perception=replace(snapshot.perception,
                               value=replace(perception, visible_entities=(entity,))),
            tracked_entity=ObservationGroupV2.valid(snapshot.world_time_ticks.value,
                                                    "client_registered_entity", tracked))


@dataclass
class RuntimeCase:
    clock: list
    backend: FaultBackend
    runtime: PlayerRuntimeV1
    session: NavigationSession
    driver: RuntimeNavigationDriver
    manager: KnownWorldFollowDriver | None

    @property
    def components(self):
        return self.clock, self.backend, self.runtime, self.session, self.driver, self.manager

    def close(self):
        self.session.close()
        self.runtime.close()

    def step(self):
        if self.manager is not None:
            self.manager.update(self.clock[0])
        return self.driver.tick(BehaviorProfileV0(), self.clock[0] + 500_000_000)

    def row(self):
        return {"movement_tick": self.backend.movement_tick,
                "position": self.backend.state.position,
                "velocity": self.backend.state.velocity_blocks_per_tick,
                "on_ground": self.backend.state.on_ground,
                "applied_movement": asdict(self.backend.applied[-1]) if self.backend.applied else None,
                "source_bound": self.driver.source is not None,
                "driver_state": self.driver.state.value, "driver_reason": self.driver.reason,
                "session_state": self.session.report.state.value,
                "session_reason": self.session.report.reason}


def build_runtime_case(*, follow=False, gap=False):
    case = gap_scenario() if gap else next(s for s in SCENARIOS if s.name == "flat_walk")
    if follow and gap:
        solids = {**case.scene.solids, **{(x, 63, z): "minecraft:stone"
                  for x in range(-2, 3) for z in range(6, 13)}}
        case = replace(case, scene=Scene(solids, ((-3, 3), (60, 68), (-2, 14))))
    clock = [100_000_000]
    backend = FaultBackend(clock, case.scene.with_floor(), case.start, case.yaw_degrees)
    backend.target = case.goal
    runtime = PlayerRuntimeV1(backend, _RecordingTrace(), lambda: clock[0])
    assert runtime.reset(ResetRequestV0("reset-io", backend.episode, "test", 1, 10_000_000_000)).succeeded
    seed_memory(runtime, backend.scene)
    session = NavigationSession("io-session", NavigationSessionProfiles.load(CONFIG),
                                planner_worker=InlinePlannerWorker(), motion_worker=InlineMotionWorker(),
                                clock_ns=lambda: clock[0])
    driver = RuntimeNavigationDriver(runtime, session, clock_ns=lambda: clock[0],
                                    observation_request=ObservationRequestV3("navigation_v1", entity_track_id=TRACK))
    manager = None
    if follow:
        manager = KnownWorldFollowDriver(driver, task_id="io-follow-task", goal_id="io-goal", target_track_id=TRACK)
        manager.start(clock[0])
    else:
        driver.start("io-goal", 1, _goal(case.goal), clock[0])
    return RuntimeCase(clock, backend, runtime, session, driver, manager)


@contextmanager
def runtime_case(**kwargs):
    case = build_runtime_case(**kwargs)
    try:
        yield case
    finally:
        case.close()


def run_io_case(*, follow, phase, retryable):
    from unittest.mock import patch
    with runtime_case(follow=follow, gap=phase == "airborne") as case:
        trace = []
        for _ in range(150):
            if case.manager is not None:
                case.manager.update(case.clock[0])
            velocity = case.backend.state.velocity_blocks_per_tick
            if ((phase == "airborne" and not case.backend.state.on_ground)
                    or (phase != "airborne" and any(abs(v) > .02 for v in (velocity[0], velocity[2])))):
                if phase == "braking":
                    assert not case.driver.release("io-braking")
                    assert case.driver.state == RuntimeNavigationDriverState.STOPPING
                break
            assert case.driver.state not in TERMINAL, case.driver.reason
            case.step()
            trace.append(case.row())
        else:
            raise AssertionError("required body phase was not entered")
        phase_entry = case.row()
        case.backend.failure = BackendIOFailure(FailureV0(
            FailureCodeV0.BACKEND_IO, "injected transport loss", retryable, "test_backend"))
        result = case.driver.tick(BehaviorProfileV0(), case.clock[0] + 500_000_000)
        failure = result.report.failure
        assert failure.code is FailureCodeV0.BACKEND_IO and failure.retryable == retryable
        disposition = case.runtime.last_failure_disposition.disposition.value
        assert disposition == ("recreate_runtime" if retryable else "end_episode")
        assert (case.driver.state, case.driver.reason) == (RuntimeNavigationDriverState.FAILED, "control_unavailable")
        assert (case.session.report.state.value, case.session.report.reason) == ("failed", "control_unavailable")
        assert case.driver.source is None and case.driver.release("terminal-release")
        writes = case.backend.attempted_writes
        with patch.object(case.session, "propose", wraps=case.session.propose) as proposals:
            for _ in range(3):
                if case.manager is not None:
                    case.manager.update(case.clock[0])
                if case.driver.state not in TERMINAL:
                    case.step()
            assert proposals.call_count == 0
        assert case.backend.attempted_writes == writes
        if case.manager is not None:
            assert case.manager.state.value == "ended"
        trace.append(case.row())
        return {"passed": True, "phase": phase, "follow": follow, "retryable": retryable,
                "phase_entry": phase_entry, "failure": asdict(failure), "disposition": disposition,
                "terminal": case.row(), "terminal_propose_calls": 0,
                "writes_after_failure": case.backend.attempted_writes - writes, "trace": trace}
