"""v7 makes the first motion job of every product task a 7-tick cold start.  What if the worker were warm?

    PYTHONPATH=. python -B v7_warm_probe.py [--seeds 200]

Run from a checkout of 3c15744.  Uses the project's own DeterministicMotionWorker and control
step unchanged, except that the first job gets the per-action warm delay instead of 7 ticks
(a long-running bot reuses its worker across tasks; see NavigationSession.spawn_successor).
Only the two drop groups are affected, since no other group submits motion jobs.
"""
import argparse
import collections
import json
import multiprocessing
import pathlib

from tests.sim.motion_delivery import DeterministicMotionWorker
from tests.sim.product_cases import product_scenario
from tests.sim.product_metrics import extract_metrics
from tests.sim.runner import run

MANIFEST = pathlib.Path("tests/sim/manifests/navigation-product-r28-v7.json")


class WarmMotionWorker(DeterministicMotionWorker):
    def submit(self, job):
        accepted = super().submit(job)
        if accepted and self.records[-1]["cold_start"]:
            record = self.records[-1]
            warm = self.profile["warm_ticks"][record["action"]]
            record.update(cold_start=False, delay_ticks=warm, ready_tick=record["submission_tick"] + warm)
        return accepted


def run_one(job):
    manifest, group, seed, warm = job
    scenario, _ = product_scenario(manifest, group, seed)
    worker = (WarmMotionWorker if warm else DeterministicMotionWorker)(manifest["motion_delivery_profile"])
    result = run(scenario, motion_factory=lambda: worker, control_step=worker.control_step)
    metrics = extract_metrics(result.trace, start_tick=1, start_position=scenario.start,
                              outcome=result.outcome, violations=result.violations)
    return {"id": f"{group['id']}-{seed:06d}", "group": group["id"], "warm": warm,
            "success": metrics["success"], "reason": result.reason,
            "arrival": metrics.get("arrival_ticks"), "jobs": len(worker.records),
            "violations": len(result.violations)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=200)
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text("utf-8"))
    groups = [g for g in manifest["groups"] if g["family"] == "strict_drop"]
    jobs = [(manifest, g, s, warm) for g in groups for s in range(args.seeds) for warm in (False, True)]
    with multiprocessing.Pool(4) as pool:
        rows = pool.map(run_one, jobs, chunksize=1)
    print(f"drop groups, first {args.seeds} seeds; v7 frozen delivery vs warm-only first job\n")
    print(f"{'group':<12}{'model':<14}{'completed':>11}{'jobs/task':>11}{'arrival p50':>13}{'violations':>12}  failure reasons")
    for group in groups:
        for warm in (False, True):
            sel = [r for r in rows if r["group"] == group["id"] and r["warm"] == warm]
            arrivals = sorted(r["arrival"] for r in sel if r["success"] and r["arrival"] is not None)
            reasons = dict(collections.Counter(r["reason"] for r in sel if not r["success"]))
            print(f"{group['id']:<12}{'warm-only' if warm else 'v7 (cold 7)':<14}"
                  f"{sum(r['success'] for r in sel):>7}/{len(sel):<3}{sum(r['jobs'] for r in sel) / len(sel):>11.2f}"
                  f"{arrivals[len(arrivals) // 2] if arrivals else '-':>13}{sum(r['violations'] for r in sel):>12}  {reasons}")
    changed = [(a["id"], a["success"], b["success"]) for a in rows if not a["warm"]
               for b in rows if b["warm"] and b["id"] == a["id"] and a["success"] != b["success"]]
    print(f"\ntasks whose completion differs between the two models: {len(changed)}")
    for item in changed:
        print("  ", item)


if __name__ == "__main__":
    main()
