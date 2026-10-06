"""Which route-admission / revalidation entry points does each S0 baseline set actually exercise?

    PYTHONPATH=. python -B path_exercise_probe.py [--seeds 20]
    PYTHONPATH=. python -B path_exercise_probe.py --coordination

Run from a checkout of 1f0fefe.  Counts calls (by monkeypatching, no behaviour change) of the
entry points that grew most during F1, over:
  * v7 product tasks, first N seeds of every group (the S0 product baseline uses all 200), and
  * the 10 F1 follow simulation scenarios (not part of the S0 baseline), and
  * with --coordination, every 8th job of the 1,448 coordination set (plus faults if listed).
"""
import argparse
import collections
import functools
import json
import multiprocessing
import pathlib

ENTRIES = [
    ("mc2p.motion_nav.route_admission", "RouteAdmitter", "admit"),
    ("mc2p.motion_nav.route_admission", "RouteAdmitter", "admit_surface"),
    ("mc2p.motion_nav.route_admission", "RouteAdmitter", "admit_current_request"),
    ("mc2p.motion_nav.route_admission", "RouteAdmitter", "admit_local_direct"),
    ("mc2p.motion_nav.route_admission", "RouteAdmitter", "admit_ground_direct"),
    ("mc2p.motion_nav.route_admission", "ActiveRouteTracker", "validate"),
    ("mc2p.motion_nav.route_validation", None, "replay_walk_validation_recipe"),
    ("mc2p.motion_nav.route_validation", None, "query_surface_walk_edge"),
    ("mc2p.motion_nav.navigation_session", "NavigationSession", "_wait_for_active_terminal"),
]
COUNTS = collections.Counter()


def install():
    import importlib
    for module_name, cls_name, fn_name in ENTRIES:
        module = importlib.import_module(module_name)
        owner = getattr(module, cls_name) if cls_name else module
        original = getattr(owner, fn_name)
        key = f"{cls_name + '.' if cls_name else ''}{fn_name}"

        @functools.wraps(original)
        def wrapper(*args, __original=original, __key=key, **kwargs):
            COUNTS[__key] += 1
            return __original(*args, **kwargs)
        setattr(owner, fn_name, wrapper)
        if not cls_name:
            # functions imported by name elsewhere must be patched where they are used
            for other in ("mc2p.motion_nav.route_admission", "mc2p.motion_nav.navigation_session"):
                mod = importlib.import_module(other)
                if getattr(mod, fn_name, None) is original:
                    setattr(mod, fn_name, wrapper)


def run_product(job):
    install()
    from tests.sim.motion_delivery import DeterministicMotionWorker
    from tests.sim.product_cases import product_scenario
    from tests.sim.runner import run
    manifest, group, seed = job
    scenario, _ = product_scenario(manifest, group, seed)
    worker = DeterministicMotionWorker(manifest["motion_delivery_profile"])
    run(scenario, motion_factory=lambda: worker, control_step=worker.control_step)
    return group["family"], dict(COUNTS)


def run_follow(name):
    install()
    from tests.sim.known_world_following import SCENARIO_BY_NAME, run_scenario
    run_scenario(SCENARIO_BY_NAME[name])
    return "follow", dict(COUNTS)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=20)
    args = parser.parse_args()
    manifest = json.loads(pathlib.Path("tests/sim/manifests/navigation-product-r28-v7.json").read_text("utf-8"))
    from tests.sim.known_world_following import SCENARIOS
    with multiprocessing.Pool(4, maxtasksperchild=1) as pool:
        product = pool.map(run_product, [(manifest, g, s) for g in manifest["groups"] for s in range(args.seeds)], chunksize=1)
        follow = pool.map(run_follow, [s.name for s in SCENARIOS], chunksize=1)
    table = collections.defaultdict(collections.Counter)
    tasks = collections.Counter()
    for family, counts in product + follow:
        tasks[family] += 1
        for key, value in counts.items():
            table[family][key] += value
    keys = [f"{c + '.' if c else ''}{f}" for _, c, f in ENTRIES]
    families = sorted(tasks)
    print(f"calls per entry point; product = first {args.seeds} seeds of each v7 group, follow = 10 F1 scenarios\n")
    print(f"{'entry point':<44}" + "".join(f"{f + ' (' + str(tasks[f]) + ')':>18}" for f in families))
    for key in keys:
        print(f"{key:<44}" + "".join(f"{table[f][key]:>18}" for f in families))




def run_coordination(job):
    install()
    import scripts.navigation_migration_evidence as nme
    identifier, kind, data = job
    try:
        nme._execute(kind, data)
    except Exception:
        pass
    return "coordination", dict(COUNTS)


def main_coordination(step=8):
    """Every `step`-th case of the 1,448 coordination set plus the 4 supplementary faults."""
    import scripts.navigation_migration_evidence as nme
    jobs = list(nme.jobs())
    sample = jobs[::step]
    with multiprocessing.Pool(4, maxtasksperchild=1) as pool:
        rows = pool.map(run_coordination, sample, chunksize=1)
    total = collections.Counter()
    for _, counts in rows:
        total.update(counts)
    keys = [f"{c + '.' if c else ''}{f}" for _, c, f in ENTRIES]
    print(f"\ncoordination set, every {step}th of {len(jobs)} jobs ({len(sample)} run):")
    for key in keys:
        print(f"  {key:<44}{total[key]:>8}")


if __name__ == "__main__":
    import sys
    if "--coordination" in sys.argv:
        main_coordination()
    else:
        main()
