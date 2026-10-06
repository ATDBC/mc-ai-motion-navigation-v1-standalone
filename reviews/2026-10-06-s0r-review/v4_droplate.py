"""Does the S0R-C-01 fix (airborne needs_state) generalise beyond seed 163?

    PYTHONPATH=. python -B v4_droplate.py drop-late 200 <out.json>      (at 1f0fefe and at e108a32)
    python -B v4_droplate.py --compare <1f0fefe.json> <e108a32.json>

Runs the first N seeds of a group of the R28 v4 product manifest (the manifest the R28-C-05
regression test draws seed 163 from) through the formal simulator chain and records outcome,
reason and invariant violations.  Read-only.
"""
import collections
import json
import sys
from multiprocessing import Pool
from pathlib import Path


def one(job):
    group_id, seed = job
    from tests.sim.product_cases import product_scenario
    from tests.sim.runner import run
    manifest = json.loads(Path("tests/sim/manifests/navigation-product-r28-v4.json").read_text("utf-8"))
    group = next(x for x in manifest["groups"] if x["id"] == group_id)
    scenario, _ = product_scenario(manifest, group, seed)
    r = run(scenario)
    return seed, r.outcome, r.reason, len(r.violations)


def main():
    if sys.argv[1] == "--compare":
        a = {r[0]: r for r in json.loads(Path(sys.argv[2]).read_text())}
        b = {r[0]: r for r in json.loads(Path(sys.argv[3]).read_text())}
        flips = [(s, a[s][1:3], b[s][1:3]) for s in sorted(a) if a[s][1:3] != b[s][1:3]]
        print(f"changed outcomes: {len(flips)} of {len(a)}")
        for seed, old, new in flips:
            print(f"  seed {seed}: {old[0]}/{old[1]} -> {new[0]}/{new[1]}")
        return
    group_id, n, output = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    with Pool(2, maxtasksperchild=20) as pool:
        rows = pool.map(one, [(group_id, s) for s in range(n)], chunksize=4)
    counts = collections.Counter((o, r) for _, o, r, _ in rows)
    print(group_id, n, "seeds:", counts.most_common(), "violations:", sum(v for *_, v in rows))
    Path(output).write_text(json.dumps(rows))


if __name__ == "__main__":
    main()
