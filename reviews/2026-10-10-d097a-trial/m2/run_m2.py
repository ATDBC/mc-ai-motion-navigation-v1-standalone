"""D097-A M2 harness.

  python run_m2.py equiv   # G8 on Sets A (frozen matrix), B (rejected candidates), C (random entries)
  python run_m2.py setd    # G8 on every row of ../m1/accepted_candidates.jsonl (rerunnable later)
  python run_m2.py perf    # G10 timing of lazy-strict (and minimal / full scan for comparison)
  python run_m2.py gates   # collect G8/G9/G10 into gates.json

Run from the project checkout:  PYTHONPATH=<checkout>:<reviews dir>:<reviews dir>/m2
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
from pathlib import Path
import platform
import sys
import time

from experiments.motion_navigation.trajectory_proto.commitment import ScanStatus, scan_commitment
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence
from experiments.motion_navigation.trajectory_proto import m0_probe

import candidates as K
import common
import equiv
import incremental as I

HERE = Path(__file__).resolve().parent
OUT = {
    "equiv": HERE / "equivalence_results.json",
    "setd": HERE / "equivalence_setd.json",
    "perf": HERE / "performance.json",
    "perf_samples": HERE / "performance_samples.json",
    "perf_b": HERE / "performance_b.json",
    "mutation": HERE / "mutation_results.json",
    "gates": HERE / "gates.json",
}


def nearest_rank(values, percent):
    ordered = sorted(values)
    return ordered[max(1, math.ceil(percent * len(ordered))) - 1]


def summarize(values):
    values = list(values)
    if not values:
        return {"samples": 0}
    return {"samples": len(values), "p50": nearest_rank(values, .5), "p95": nearest_rank(values, .95),
            "p99": nearest_rank(values, .99), "max": max(values), "mean": sum(values) / len(values)}


def platform_info():
    return {"system": platform.system(), "release": platform.release(), "python": platform.python_version(),
            "cpus": os.cpu_count(), "loadavg_start": os.getloadavg()}


# ------------------------------------------------------------------------------ equivalence
def run_candidate(cand):
    evidence = cand.evidence if cand.evidence is not None else _boundary_evidence(cand.request, cand.inputs)
    scan = equiv.scan_commitment(cand.request, cand.inputs, cand.world, evidence)
    lazy = I.lazy_prove(cand.request, cand.inputs, cand.world, evidence)
    minimal = I.lazy_prove(cand.request, cand.inputs, cand.world, evidence, mode="minimal")
    row = equiv.compare(scan, lazy)
    mini = equiv.compare(scan, minimal)
    row.update({
        "set": cand.set, "id": cand.id, "ticks": len(cand.inputs),
        "decisions": collections.Counter(d.kind for d in lazy.decisions),
        "minimal": {"status": mini["lazy_status"], "reason": mini["lazy_reason"],
                    "status_match": mini["status_match"], "reason_match": mini["reason_match"],
                    "critical": mini["critical"],
                    # minimal mode skips in-air tails on purpose, so only the intervals are comparable
                    "intervals_equivalent": mini.get("interval_keys_match", False)
                    and mini.get("interval_full_match", False),
                    "steps": mini["lazy_steps"]},
    })
    row["decisions"] = dict(row["decisions"])
    return row


def summarize_rows(rows):
    scan_verified = [r for r in rows if r["scan_status"] == "VERIFIED_CANDIDATE"]
    rejected = [r for r in rows if r["scan_status"] != "VERIFIED_CANDIDATE"]
    return {
        "candidates": len(rows),
        "scan_verified": len(scan_verified),
        "scan_not_verified": len(rejected),
        "scan_status_histogram": dict(collections.Counter(r["scan_status"] for r in rows)),
        "scan_reason_histogram": dict(collections.Counter(
            f"{r['scan_status']}/{r['scan_reason']}" for r in rejected)),
        "status_mismatch": sum(not r["status_match"] for r in rows),
        "reason_mismatch": sum(not r["reason_match"] for r in rows),
        "critical_lazy_verified_where_scan_not": sum(r["critical"] for r in rows),
        "verified_not_equivalent": sum(not r["equivalent"] for r in scan_verified),
        "verified_interval_key_mismatch": sum(not r["interval_keys_match"] for r in scan_verified
                                              if "interval_keys_match" in r),
        "verified_interval_count_mismatch": sum(not r["interval_counts_match"] for r in scan_verified
                                                if "interval_counts_match" in r),
        "verified_with_intervals": sum(1 for r in scan_verified if sum(r.get("n_intervals", [0]))),
        "verified_with_2plus_intervals_in_a_branch": sum(
            1 for r in scan_verified if max(r.get("n_intervals", [0])) >= 2),
        "verified_dependency_set_mismatch": sum(not r.get("dependencies_match", True) for r in scan_verified),
        "minimal_mode_informational": {
            "status_mismatch": sum(not r["minimal"]["status_match"] for r in rows),
            "reason_mismatch": sum(not r["minimal"]["reason_match"] for r in rows),
            "critical": sum(r["minimal"]["critical"] for r in rows),
            "verified_intervals_not_equivalent": sum(not r["minimal"]["intervals_equivalent"]
                                                     for r in scan_verified),
        },
    }


def mismatch_details(rows, limit=40):
    keep = ("set", "id", "scan_status", "scan_reason", "lazy_status", "lazy_reason", "intervals_scan",
            "intervals_lazy", "critical")
    return [{k: r.get(k) for k in keep} for r in rows
            if not (r["equivalent"] and r["reason_match"])][:limit]


def run_set(cands, label):
    rows, started = [], time.perf_counter()
    for index, cand in enumerate(cands):
        rows.append(run_candidate(cand))
        if index % 100 == 99:
            print(f"  {label}: {index + 1}/{len(cands)}", flush=True)
    return {"summary": summarize_rows(rows), "seconds": round(time.perf_counter() - started, 1),
            "mismatches": mismatch_details(rows), "rows": rows}


def cmd_equiv(args):
    payload = {"platform": platform_info(), "sets": {}}
    for name, builder in (("A", K.set_a), ("B", K.set_b), ("C", K.set_c)):
        cands = builder()
        print(f"set {name}: {len(cands)} candidates", flush=True)
        payload["sets"][name] = run_set(cands, name)
        print(json.dumps(payload["sets"][name]["summary"], indent=1), flush=True)
    OUT["equiv"].write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")


def cmd_setd(args):
    path = Path(args.path) if args.path else K.M1_ACCEPTED
    out = OUT["setd"] if path == K.M1_ACCEPTED else HERE / "equivalence_setd_custom.json"
    if not path.exists():
        print(f"Set D not available: {path} does not exist")
        out.write_text(json.dumps({"available": False, "path": str(path)}, indent=1), encoding="utf-8")
        return 0
    cands = K.set_d(path)
    print(f"set D: {len(cands)} rows from {path}", flush=True)
    result = run_set(cands, "D")
    payload = {"available": True, "path": str(path), "platform": platform_info(), **result}
    out.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    print(json.dumps(result["summary"], indent=1))
    return 0


# ------------------------------------------------------------------------------ performance
def timed_scan(cand, world):
    evidence = _boundary_evidence(cand.request, cand.inputs)
    started = time.perf_counter_ns()
    scan = scan_commitment(cand.request, cand.inputs, world, evidence)
    wall = (time.perf_counter_ns() - started) / 1e6
    assert scan.status is ScanStatus.VERIFIED_CANDIDATE, cand.id
    return wall, scan.counts.physics_steps


def timed_lazy(cand, world, mode):
    evidence = _boundary_evidence(cand.request, cand.inputs)
    result = I.lazy_prove(cand.request, cand.inputs, world, evidence, mode=mode)
    assert result.status is ScanStatus.VERIFIED_CANDIDATE, (cand.id, mode, result.status, result.reason)
    return result


def cmd_perf(args):
    if args.population == "b":
        # supplementary: every Set B candidate the full scanner verifies (mutants, double hops, fuzz)
        cands = [c for c in K.set_b()
                 if scan_commitment(c.request, c.inputs, c.world,
                                    c.evidence or _boundary_evidence(c.request, c.inputs)).status
                 is ScanStatus.VERIFIED_CANDIDATE]
    else:
        cands = K.set_a() + K.set_c()
        if K.M1_ACCEPTED.exists() and not args.no_setd:
            cands += K.set_d()
    modes = ("strict",) if args.strict_only else ("strict", "minimal")
    print(f"perf over {len(cands)} verified candidates, {args.passes} passes", flush=True)
    load_start = os.getloadavg()
    samples = []             # (pass, mode, cache, cand, kind, steps, ms)
    whole = []               # per candidate/run: (pass, cache, cand, scan_ms, lazy_strict_ms, lazy_min_ms, commit ms...)
    for pass_index in range(args.passes):
        for cand in cands:
            record = {"pass": pass_index, "cand": cand.id, "set": cand.set}
            for cache in ("cold", "warm"):
                if cache == "cold":
                    views = {m: common.fresh_world(cand.world) for m in ("scan", "strict", "minimal")}
                if not args.strict_only:
                    scan_ms, scan_steps = timed_scan(cand, views["scan"])
                    record[f"{cache}_scan_ms"] = scan_ms
                    record[f"{cache}_scan_steps"] = scan_steps
                for mode in modes:
                    result = timed_lazy(cand, views[mode], mode)
                    record[f"{cache}_{mode}_total_ms"] = sum(d.wall_ns for d in result.decisions) / 1e6
                    record[f"{cache}_{mode}_total_steps"] = result.counts.physics_steps
                    commit_ms = sum(d.wall_ns for d in result.decisions if d.kind == "commit") / 1e6
                    commit_steps = sum(d.physics_steps for d in result.decisions if d.kind == "commit")
                    record[f"{cache}_{mode}_commit_ms"] = commit_ms
                    record[f"{cache}_{mode}_commit_steps"] = commit_steps
                    record["has_commit"] = any(d.kind == "commit" for d in result.decisions)
                    for d in result.decisions:
                        samples.append((pass_index, mode, cache, cand.id, d.kind, d.physics_steps,
                                        d.wall_ns / 1e6))
            whole.append(record)
        print(f"  pass {pass_index + 1}/{args.passes} done", flush=True)
    load_end = os.getloadavg()

    def pick(mode, cache, kind, field, pass_index=None):
        column = 6 if field == "ms" else 5
        return [s[column] for s in samples if s[1] == mode and s[2] == cache and s[4] == kind
                and (pass_index is None or s[0] == pass_index)]

    stats = {}
    for mode in modes:
        for cache in ("cold", "warm"):
            for kind in ("plan", "tick", "commit", "final"):
                key = f"{mode}/{cache}/{kind}"
                stats[key] = {"ms": summarize(pick(mode, cache, kind, "ms")),
                              "physics_steps": summarize(pick(mode, cache, kind, "steps")),
                              "ms_per_pass_p95": [
                                  summarize(pick(mode, cache, kind, "ms", p)).get("p95")
                                  for p in range(args.passes)]}
    commit_cands = [r for r in whole if r["has_commit"]]
    comparison = {}
    for cache in (("cold", "warm") if not args.strict_only else ()):
        comparison[cache] = {
            "n_candidates_with_commit": len({r["cand"] for r in commit_cands}),
            "full_scan_ms": summarize([r[f"{cache}_scan_ms"] for r in commit_cands]),
            "lazy_strict_total_ms": summarize([r[f"{cache}_strict_total_ms"] for r in commit_cands]),
            "strict_commit_ms": summarize([r[f"{cache}_strict_commit_ms"] for r in commit_cands]),
            "minimal_commit_ms": summarize([r[f"{cache}_minimal_commit_ms"] for r in commit_cands]),
            "strict_commit_steps": summarize([r[f"{cache}_strict_commit_steps"] for r in commit_cands]),
            "minimal_commit_steps": summarize([r[f"{cache}_minimal_commit_steps"] for r in commit_cands]),
            "full_scan_steps": summarize([r[f"{cache}_scan_steps"] for r in commit_cands]),
            "all_candidates_full_scan_ms": summarize([r[f"{cache}_scan_ms"] for r in whole]),
        }
    payload = {
        "platform": platform_info(), "loadavg_end": load_end, "loadavg_start_perf": load_start,
        "note": ("Linux; another process (agent M1, ~3 cores) was running concurrently; percentiles are "
                 "nearest-rank; cold = fresh PhysicsWorldView (empty shape cache) per candidate, warm = "
                 "second run on the same view; tick = decisions that permit one command with no open "
                 "risk interval; commit = decisions that cross a commitment point (tails k..recovery)."),
        "population": args.population, "strict_only": args.strict_only,
        "passes": args.passes, "candidates": len(cands),
        "candidates_by_set": dict(collections.Counter(c.set for c in cands)),
        "stats": stats, "commit_comparison": comparison,
    }
    if args.population == "b":
        OUT["perf_b"].write_text(json.dumps(payload, indent=1), encoding="utf-8")
        for key in ("strict/cold/tick", "strict/cold/commit"):
            print(key, json.dumps(stats[key]["ms"]), json.dumps(stats[key]["physics_steps"]))
        return
    OUT["perf"].write_text(json.dumps(payload, indent=1), encoding="utf-8")
    OUT["perf_samples"].write_text(json.dumps({"columns": ["pass", "mode", "cache", "cand", "kind", "steps", "ms"],
                                               "rows": samples, "whole": whole}), encoding="utf-8")
    for key in ("strict/cold/tick", "strict/cold/commit", "strict/warm/tick", "strict/warm/commit",
                "minimal/cold/commit"):
        print(key, json.dumps(stats[key]["ms"]), json.dumps(stats[key]["physics_steps"]))


# ------------------------------------------------------------------------------ gates
def cmd_gates(args):
    gates = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "notes": []}
    # ---- G8
    if OUT["equiv"].exists():
        eq = json.loads(OUT["equiv"].read_text())
        summ = {name: s["summary"] for name, s in eq["sets"].items()}
        a = summ["A"]
        a_ok = (a["candidates"] == 23 and a["scan_verified"] == 23 and a["verified_not_equivalent"] == 0
                and a["status_mismatch"] == 0)
        b, c = summ["B"], summ["C"]
        bc_ok = all(s["status_mismatch"] == 0 and s["critical_lazy_verified_where_scan_not"] == 0
                    and s["verified_not_equivalent"] == 0 for s in (b, c))
        g8 = {"set_A": a, "set_B": b, "set_C": c, "set_A_pass": a_ok, "set_B_C_pass": bc_ok,
              "set_B_scan_rejections": b["scan_not_verified"]}
    else:
        g8 = {"error": "equivalence_results.json missing"}
        a_ok = bc_ok = False
    d_ok = None
    if OUT["setd"].exists():
        sd = json.loads(OUT["setd"].read_text())
        if sd.get("available"):
            s = sd["summary"]
            d_ok = (s["candidates"] > 0 and s["status_mismatch"] == 0 and s["verified_not_equivalent"] == 0
                    and s["critical_lazy_verified_where_scan_not"] == 0
                    and s["scan_verified"] == s["candidates"])
            g8["set_D"] = s
            g8["set_D_pass"] = d_ok
        else:
            g8["set_D"] = "not available when run"
            g8["set_D_pass"] = None
    else:
        g8["set_D"] = "not run"
        g8["set_D_pass"] = None
    g8["passed"] = bool(a_ok and bc_ok and d_ok) if d_ok is not None else None
    g8["verdict"] = ("PASS" if g8["passed"] else "FAIL" if (g8["passed"] is False and d_ok is not None)
                     else "A/B/C " + ("PASS" if (a_ok and bc_ok) else "FAIL") + "; Set D not evaluated")
    gates["G8"] = g8
    # ---- G9
    if OUT["mutation"].exists():
        m = json.loads(OUT["mutation"].read_text())
        gates["G9"] = {"baseline_passes": m["baseline"]["passed"],
                       "faults": {k: {"detected": v["detected"], "failing_tests": v["failing_tests"]}
                                  for k, v in m["faults"].items()},
                       "passed": bool(m["baseline"]["passed"] and all(v["detected"] for v in m["faults"].values())
                                      and len(m["faults"]) == 7)}
    else:
        gates["G9"] = {"error": "mutation_results.json missing", "passed": None}
    # ---- G10
    if OUT["perf"].exists():
        p = json.loads(OUT["perf"].read_text())
        st = p["stats"]
        tick, commit = st["strict/cold/tick"], st["strict/cold/commit"]
        worst_tick = max(x for x in tick["ms_per_pass_p95"] if x is not None)
        worst_commit = max(x for x in commit["ms_per_pass_p95"] if x is not None)
        g10 = {
            "tick_samples": tick["ms"]["samples"], "commit_samples": commit["ms"]["samples"],
            "tick_p95_ms_cold_pooled": tick["ms"]["p95"], "tick_p95_ms_cold_worst_pass": worst_tick,
            "tick_p95_ms_warm_pooled": st["strict/warm/tick"]["ms"]["p95"],
            "commit_p95_ms_cold_pooled": commit["ms"]["p95"], "commit_p95_ms_cold_worst_pass": worst_commit,
            "commit_p95_ms_warm_pooled": st["strict/warm/commit"]["ms"]["p95"],
            "thresholds": {"tick_p95_ms": 10., "commit_p95_ms": 50., "min_tick_samples": 1000},
            "minimal_mode_commit_p95_ms_cold": st["minimal/cold/commit"]["ms"]["p95"],
            "tick_pass": max(tick["ms"]["p95"], worst_tick) <= 10.,
            "commit_pass": max(commit["ms"]["p95"], worst_commit) <= 50.,
            "samples_pass": tick["ms"]["samples"] >= 1000,
            "interpretation": "stricter reading: max(pooled P95, worst per-pass P95), cold shape cache, strict mode",
        }
        g10["passed"] = bool(g10["tick_pass"] and g10["commit_pass"] and g10["samples_pass"])
        g10["population"] = "verified Set A + Set C candidates (+ Set D when present at run time)"
        if OUT["perf_b"].exists():
            pb = json.loads(OUT["perf_b"].read_text())["stats"]
            tb, cb = pb["strict/cold/tick"], pb["strict/cold/commit"]
            g10["supplementary_all_verified_set_B"] = {
                "tick_samples": tb["ms"]["samples"], "tick_p95_ms_cold": tb["ms"]["p95"],
                "commit_samples": cb["ms"]["samples"], "commit_p95_ms_cold": cb["ms"]["p95"],
                "tick_pass": tb["ms"]["p95"] <= 10., "commit_pass": cb["ms"]["p95"] <= 50.}
        gates["G10"] = g10
    else:
        gates["G10"] = {"error": "performance.json missing", "passed": None}
    OUT["gates"].write_text(json.dumps(gates, indent=1), encoding="utf-8")
    print(json.dumps({k: (v.get("verdict") or v.get("passed")) for k, v in gates.items() if isinstance(v, dict)},
                     indent=1))


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("equiv")
    p = sub.add_parser("setd")
    p.add_argument("--path")
    p = sub.add_parser("perf")
    p.add_argument("--passes", type=int, default=3)
    p.add_argument("--no-setd", action="store_true")
    p.add_argument("--population", choices=("main", "b"), default="main")
    p.add_argument("--strict-only", action="store_true")
    sub.add_parser("gates")
    args = parser.parse_args(argv)
    return {"equiv": cmd_equiv, "setd": cmd_setd, "perf": cmd_perf, "gates": cmd_gates}[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
