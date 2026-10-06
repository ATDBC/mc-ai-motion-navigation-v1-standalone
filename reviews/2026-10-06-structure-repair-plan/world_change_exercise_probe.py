"""Can a cheap world-change scenario exercise walk-recipe revalidation?  (1f0fefe, simulator only)

    PYTHONPATH=. python -B world_change_exercise_probe.py

No current baseline set calls route_validation.replay_walk_validation_recipe.  This probe adds
world edits along the walking line (stone floor cell -> bottom slab, or a full block placed on
the route) to (a) one F1 follow scenario through a test-only backend subclass and (b) the ordinary
flat_walk point task, and counts calls of the revalidation entry points.  Behaviour is not changed
apart from the injected edits.

Reading the follow slab rows: the simulator's calculator backend stops with "unsupported" when the
body walks onto the edited bottom slab, PlayerRuntimeV1 reports a retryable BACKEND_IO failure,
and the exception is RuntimeNavigationDriver re-running the same frame afterwards (see
io_failure_probe.py).  Use full-block edits for a world-change baseline until the calculator
backend supports that step.
"""
import collections
from dataclasses import replace

COUNTS = collections.Counter()


def install():
    import mc2p.motion_nav.route_admission as ra
    original = ra.replay_walk_validation_recipe
    def replay(*a, **k):
        COUNTS["replay_walk_validation_recipe"] += 1
        return original(*a, **k)
    ra.replay_walk_validation_recipe = replay
    for owner, name in ((ra.ActiveRouteTracker, "validate"), (ra.RouteAdmitter, "admit_ground_direct")):
        orig = getattr(owner, name)
        def wrapped(*a, __o=orig, __n=name, **k):
            COUNTS[__n] += 1
            return __o(*a, **k)
        setattr(owner, name, wrapped)


def follow_case(label, edits):
    import tests.sim.known_world_following as kwf
    from tests.sim.backend import Perturbations
    base_init = kwf.FollowingBackend.__init__
    def init(self, *a, **k):
        base_init(self, *a, **k)
        self.perturbations = Perturbations(world_edits=edits)
    kwf.FollowingBackend.__init__ = init
    try:
        COUNTS.clear()
        try:
            result = kwf.run_scenario(kwf.SCENARIO_BY_NAME["straight_2_0"])
        except Exception as error:  # report, do not hide: an escaping exception is itself a finding
            print(f"follow straight_2_0 + {label:<34} EXCEPTION {type(error).__name__}: {error} {dict(COUNTS)}")
            return
        print(f"follow straight_2_0 + {label:<34} passed={result['passed']} state={result['terminal_session_state']}"
              f" violations={len(result['safety_violations'])} {dict(COUNTS)}")
    finally:
        kwf.FollowingBackend.__init__ = base_init


def point_case(label, edits):
    from tests.sim.backend import Perturbations
    from tests.sim.runner import run
    from tests.sim.scenarios import SCENARIOS
    base = next(s for s in SCENARIOS if s.name == "flat_walk")
    COUNTS.clear()
    try:
        r = run(replace(base, name="wc-" + label.split()[0], perturbations=Perturbations(world_edits=edits)))
    except Exception as error:
        print(f"point flat_walk + {label:<36} EXCEPTION {type(error).__name__}: {error} {dict(COUNTS)}")
        return
    print(f"point flat_walk + {label:<36} outcome={r.outcome} reason={r.reason} violations={len(r.violations)} {dict(COUNTS)}")


if __name__ == "__main__":
    install()
    # follow lane: target starts ahead on +z and walks away; the bot walks along x=0.5
    for tick in (60, 100):
        follow_case(f"slab at z+6 floor, tick {tick}", {tick: {(0, 63, z): "minecraft:stone_slab[type=bottom]" for z in range(8, 14)}})
    follow_case("stone block on route, tick 80", {80: {(0, 64, 12): "minecraft:stone"}})
    point_case("slab on route floor, tick 10", {10: {(0, 63, 5): "minecraft:stone_slab[type=bottom]"}})
    point_case("stone block on route, tick 10", {10: {(0, 64, 5): "minecraft:stone"}})
