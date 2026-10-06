"""One retryable backend I/O failure during a running route.  (1f0fefe, simulator only)

    PYTHONPATH=. python -B io_failure_probe.py

The calculator backend's `step` raises once at the given movement tick, as a bridge read timeout
would.  PlayerRuntimeV1 reports FailureV0(BACKEND_IO, retryable=True), goes to state FAILED and
classifies it as RECREATE_RUNTIME.  The probe prints how the formal chain ends: a typed outcome,
or an exception escaping RuntimeNavigationDriver.tick.  It also prints the driver state right
after the failed step.  Nothing else is changed.
"""
from dataclasses import replace

import mc2p.skills.navigation_session_driver as driver_module
import tests.sim.backend as sim_backend
import tests.sim.known_world_following as following

FAIL_AT = {"tick": None}
AFTER_FAILURE = []


def install():
    original_step = sim_backend.CalculatorBackend.step

    def step(self, action, deadline, **kwargs):
        if self.movement_tick == FAIL_AT["tick"] and not getattr(self, "_probe_failed", False):
            self._probe_failed = True
            raise RuntimeError("simulated bridge read timeout")
        return original_step(self, action, deadline, **kwargs)
    sim_backend.CalculatorBackend.step = step

    original_adopt = driver_module.RuntimeNavigationDriver.adopt_result

    def adopt(self, result):
        original_adopt(self, result)
        if result.report.failure is not None:
            AFTER_FAILURE.append(f"driver={self.state}/{self.reason} runtime={self.runtime.state.value} "
                                 f"disposition={self.runtime.last_failure_disposition.disposition.value}")
    driver_module.RuntimeNavigationDriver.adopt_result = adopt


def point(tick):
    from tests.sim.runner import run
    from tests.sim.scenarios import SCENARIOS
    base = next(s for s in SCENARIOS if s.name == "flat_walk")
    r = run(replace(base, name="io-failure"))
    return f"outcome={r.outcome} reason={r.reason} violations={len(r.violations)}"


def follow(tick):
    r = following.run_scenario(following.SCENARIO_BY_NAME["straight_2_0"])
    return f"passed={r['passed']} driver={r['terminal_driver_state']} session={r['terminal_session_state']}"


if __name__ == "__main__":
    install()
    for label, case, ticks in (("point flat_walk", point, (5, 20)), ("follow straight_2_0", follow, (30, 120))):
        for tick in ticks:
            FAIL_AT["tick"] = tick
            AFTER_FAILURE.clear()
            try:
                ending = case(tick)
            except Exception as error:  # an escaping exception is the finding
                ending = f"EXCEPTION {type(error).__name__}: {error}"
            print(f"{label:<20} I/O failure at movement tick {tick:>3}: {ending}")
            print(f"{'':<20} right after the failed step: {AFTER_FAILURE[0] if AFTER_FAILURE else '-'}")
