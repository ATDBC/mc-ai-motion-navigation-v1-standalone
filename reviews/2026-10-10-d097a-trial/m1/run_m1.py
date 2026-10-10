"""D097-A M1 driver: generate / compare / validate / reverify / g6 / timing / gates.

Run from the project checkout with PYTHONPATH=<project>:<review dir>:<review dir>/m1.
Validation is run ONCE against a frozen table; every command writes into this directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing
import os
import time

import controller as C
import entries as E
import oracle as O
import table as T

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = os.path.join(HERE, "table.json")


def _path(name):
    return os.path.join(HERE, name)


def _dump(value, name):
    with open(_path(name), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, sort_keys=True, indent=1) + "\n")


def percentile(values, q):
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1] if ordered else None  # nearest rank


def log_to(name):
    handle = open(_path(name), "a", encoding="utf-8", newline="\n")

    def log(message):
        handle.write(f"{time.strftime('%H:%M:%S')} {message}\n")
        handle.flush()
    return log


# ------------------------------------------------------------------ generate / compare

def cmd_generate(args):
    log = log_to(f"generation_{args.tag}.log")
    cpu0 = time.process_time()
    table, stats = T.generate(args.workers, args.reverse, log)
    stats["cpu_seconds_parent"] = time.process_time() - cpu0
    digest = T.write_table(table, _path(f"table_{args.tag}.json"))
    stats.update({"tag": args.tag, "table_sha256": digest, "table_bytes": os.path.getsize(_path(f"table_{args.tag}.json"))})
    _dump(stats, f"generation_{args.tag}.json")
    log(f"done sha256={digest} cpu={stats['cpu_seconds_total']:.0f}s wall={stats['wall_seconds']:.0f}s")


def cmd_compare(args):
    a, b = (open(_path(f"table_{tag}.json"), "rb").read() for tag in ("A", "B"))
    result = {"sha256_A": hashlib.sha256(a).hexdigest(), "sha256_B": hashlib.sha256(b).hexdigest(),
              "identical": a == b, "bytes": len(a), "size_le_1MB": len(a) <= 1_000_000}
    if a == b:
        with open(TABLE, "wb") as handle:
            handle.write(a)
    _dump(result, "table_comparison.json")
    print(result)


# ------------------------------------------------------------------ validation

def _candidate_line(spec, entry_bin, params, inputs, index):
    return {"template_id": spec.template_id, "bin": entry_bin.bin_id, "index": index, "gait": spec.gait,
            "ticks": spec.ticks, "offset": spec.offset, "heading_deg": spec.heading_deg,
            "params": params.to_dict(),
            "inputs": [[c.forward, c.strafe, c.jump, c.sneak, c.sprint, c.movement_yaw_radians] for c in inputs]}


def validate_bin(task):
    template_id, bin_id = task
    table = T.load_table(TABLE)
    entry_bin = next(b for b in E.entry_bins() if b.bin_id == bin_id)
    template = E.TEMPLATES[template_id]
    geometry = C.Geometry.for_template(template_id)
    row_params = table.params(template_id, bin_id)
    cap = T.CAP_SUPPORTED if template.expect_supported else T.CAP_NEGATIVE
    started, samples, accepted_lines = time.process_time(), [], []
    for index, spec in enumerate(E.sample_entries(template_id, entry_bin, E.SAMPLES_PER_BIN, E.VALIDATION_SEED)):
        record = {"template": template_id, "bin": bin_id, "i": index}
        try:
            request, world = E.make_request(spec)
        except E.EntryInvalid as error:
            samples.append({**record, "status": "entry_invalid", "why": str(error)})
            continue
        request = C.unbounded(request)          # budget swap outside the timed region
        action_ns, tried, accepted = 0, 0, None
        scan_rejects = 0
        for params in row_params[:3]:
            tried += 1
            began = time.perf_counter_ns()
            node, reason = C.evaluate(C.Tree(request, world, geometry), params)
            action_ns += time.perf_counter_ns() - began
            if node is None:
                continue
            ok, why, scan = C.verify(request, world, node.inputs)
            if ok:
                accepted = (params, node.inputs)
                break
            scan_rejects += 1
        record.update({"tried": tried, "scan_rejects": scan_rejects,
                       "action_start_us": round(action_ns / 1000., 1) if row_params else None})
        if accepted:
            record.update({"status": "accepted", "param_index": row_params.index(accepted[0]),
                           "ticks": len(accepted[1])})
            accepted_lines.append(_candidate_line(spec, entry_bin, *accepted, index))
        elif not template.expect_supported:
            record["status"] = "unsupported"
        else:
            result = O.enumerate_point(request, world, geometry, mode="feasible", cap=cap)
            record["oracle_steps"] = result.steps
            record["oracle_scan_steps"] = result.scan_steps
            record["oracle_over_cap"] = result.over_cap
            record["status"] = ("oracle_unresolved" if result.feasible is None else
                                "table_miss" if result.feasible else "infeasible")
            if result.first:
                record["oracle_params"] = result.first[0].to_dict()
        samples.append(record)
    return task, samples, accepted_lines, time.process_time() - started


def cmd_validate(args):
    log = log_to("validation.log")
    table = T.load_table(TABLE)          # identity-checked
    log(f"table sha256={table.sha256}")
    tasks = [(t, b.bin_id) for t in T.TEMPLATE_ORDER for b in E.entry_bins()]
    results, started = {}, time.time()
    with multiprocessing.Pool(args.workers) as pool:
        for task, samples, lines, cpu in pool.imap_unordered(validate_bin, tasks, chunksize=1):
            results[task] = (samples, lines, cpu)
            counts = {}
            for s in samples:
                counts[s["status"]] = counts.get(s["status"], 0) + 1
            log(f"[{len(results)}/{len(tasks)}] {task[0]}/{task[1]} {counts} cpu={cpu:.0f}s")
    with open(_path("validation_samples.jsonl"), "w", encoding="utf-8", newline="\n") as samples_file, \
            open(_path("accepted_candidates.jsonl"), "w", encoding="utf-8", newline="\n") as accepted_file:
        for task in tasks:
            for sample in results[task][0]:
                samples_file.write(json.dumps(sample, sort_keys=True) + "\n")
            for line in results[task][1]:
                accepted_file.write(json.dumps(line, sort_keys=True) + "\n")
    summary = summarise_validation(tasks, results)
    summary.update({"table_sha256": table.sha256, "wall_seconds": time.time() - started,
                    "cpu_seconds_total": sum(r[2] for r in results.values())})
    _dump(summary, "validation_results.json")
    log("validation done")


def summarise_validation(tasks, results) -> dict:
    per_template, per_bin = {}, []
    for task in tasks:
        counts = {}
        for s in results[task][0]:
            counts[s["status"]] = counts.get(s["status"], 0) + 1
        per_bin.append({"template": task[0], "bin": task[1], **{k: counts.get(k, 0) for k in
                        ("accepted", "table_miss", "infeasible", "entry_invalid", "unsupported", "oracle_unresolved")}})
        total = per_template.setdefault(task[0], {})
        for k, v in counts.items():
            total[k] = total.get(k, 0) + v
    for template_id, total in per_template.items():
        accepted, misses = total.get("accepted", 0), total.get("table_miss", 0) + total.get("oracle_unresolved", 0)
        total["success_rate"] = accepted / (accepted + misses) if accepted + misses else None
        total["denominator"] = accepted + misses
    return {"per_template": per_template, "per_bin": per_bin}


# ------------------------------------------------------------------ re-verification of accepted candidates (G2)

def _reverify(lines):
    from mc2p.motion_nav.physics_types import TickInput
    bad, cpu = [], time.process_time()
    for line in lines:
        spec = E.EntrySpec(line["template_id"], line["gait"], line["ticks"], line["offset"], line["heading_deg"])
        request, world = E.make_request(spec)
        inputs = tuple(TickInput(f, s, j, n, p, y) for f, s, j, n, p, y in line["inputs"])
        ok, why, scan = C.verify(request, world, inputs)
        if not ok:
            bad.append([line["template_id"], line["bin"], line["index"], why])
    return bad, time.process_time() - cpu


def cmd_reverify(args):
    with open(_path("accepted_candidates.jsonl"), encoding="utf-8") as handle:
        lines = [json.loads(x) for x in handle]
    chunks = [lines[i::args.workers * 8] for i in range(args.workers * 8)]
    with multiprocessing.Pool(args.workers) as pool:
        parts = pool.map(_reverify, chunks)
    bad = [b for part in parts for b in part[0]]
    _dump({"accepted_candidates": len(lines), "reverified_rejected": len(bad), "rejected": bad,
           "cpu_seconds": sum(p[1] for p in parts)}, "reverify.json")
    print(len(lines), len(bad))


# ------------------------------------------------------------------ G6 mixed fixture

def cmd_g6(args):
    from experiments.motion_navigation.trajectory_proto import m0_probe
    fixture = m0_probe.build_mixed_action_fixture()
    geometry = C.Geometry(1.0, (0.5, 1.0, 3.341460582123732))
    started = time.process_time()
    result = O.enumerate_point(fixture.fixture.request, fixture.fixture.world, geometry, mode="feasible")
    collect = O.enumerate_point(fixture.fixture.request, fixture.fixture.world, geometry, mode="collect")
    inputs = result.first[1] if result.first else None
    ok = None
    if inputs:
        ok = C.verify(fixture.fixture.request, fixture.fixture.world, inputs)[:2]
    out = {"feasible": result.feasible, "params": result.first[0].to_dict() if result.first else None,
           "steps_feasible_mode": result.steps, "scan_steps": result.scan_steps, "classes": result.classes,
           "steps_collect_mode": collect.steps, "collect_passing_params": len(collect.passing),
           "verify": list(ok) if ok else None,
           "matches_expected_W1_SJ_W5_N14": inputs == fixture.expected_inputs,
           "inputs_alphabet_index": [E.ALPHABET.index(i) for i in inputs] if inputs else None,
           "inputs": [[c.forward, c.strafe, c.jump, c.sneak, c.sprint, c.movement_yaw_radians] for c in inputs] if inputs else None,
           "cpu_seconds": time.process_time() - started}
    _dump(out, "g6_mixed_fixture.json")
    print(out["feasible"], out["matches_expected_W1_SJ_W5_N14"], out["steps_feasible_mode"])


# ------------------------------------------------------------------ G7b timing (single process, secondary measurement)

def cmd_timing(args):
    table = T.load_table(TABLE)
    rows, all_three = [], []
    for template_id in E.SUPPORTED_TEMPLATES:
        geometry = C.Geometry.for_template(template_id)
        for entry_bin in E.entry_bins():
            row_params = table.params(template_id, entry_bin.bin_id)
            if not row_params:
                continue
            for spec in E.sample_entries(template_id, entry_bin, E.SAMPLES_PER_BIN, E.VALIDATION_SEED)[:args.per_bin]:
                try:
                    request, world = E.make_request(spec)
                except E.EntryInvalid:
                    continue
                request = C.unbounded(request)
                total, first_pass = 0, None
                for params in row_params[:3]:
                    began = time.perf_counter_ns()
                    node, reason = C.evaluate(C.Tree(request, world, geometry), params)
                    total += time.perf_counter_ns() - began
                    if node is not None and first_pass is None:
                        first_pass = total
                rows.append((first_pass if first_pass is not None else total) / 1e6)
                all_three.append(total / 1e6)
    out = {"samples": len(rows), "as_run_ms": {q: percentile(rows, q) for q in (.5, .95, .99, 1.)},
           "all_three_params_ms": {q: percentile(all_three, q) for q in (.5, .95, .99, 1.)},
           "note": "single process, other agents' processes active; validation samples (seed 20261011), first per_bin of each bin"}
    _dump(out, "g7b_timing_single_process.json")
    print(out)


# ------------------------------------------------------------------ table summary

def cmd_table_summary(args):
    table = T.load_table(TABLE)
    summary = {}
    for (template_id, bin_id), row in table.rows.items():
        entry = summary.setdefault(template_id, {"rows": 0, "supported": 0, "unsupported": 0, "params_per_row": {},
                                                  "train_covered_min": None, "train_covered_total": 0,
                                                  "train_samples_total": 0, "rows_below_99pct_train": [],
                                                  "max_point_steps_collect": 0, "points_over_cap": 0})
        entry["rows"] += 1
        entry[row["status"]] += 1
        n = len(row["params"])
        entry["params_per_row"][n] = entry["params_per_row"].get(n, 0) + 1
        entry["max_point_steps_collect"] = max(entry["max_point_steps_collect"], max(row["points"]["steps"], default=0))
        entry["points_over_cap"] += row["points"]["over_cap"]
        if row["train"]:
            covered, total = row["train"]["covered"], row["train"]["samples"] - row["train"]["invalid"]
            entry["train_covered_total"] += covered
            entry["train_samples_total"] += total
            if covered < .99 * total:
                entry["rows_below_99pct_train"].append([bin_id, covered, total])
    _dump({"table_sha256": table.sha256, "per_template": summary}, "table_summary.json")
    print(json.dumps(summary, sort_keys=True)[:600])


# ------------------------------------------------------------------ gates

def cmd_gates(args):
    validation = json.load(open(_path("validation_results.json")))
    samples = [json.loads(x) for x in open(_path("validation_samples.jsonl"), encoding="utf-8")]
    comparison = json.load(open(_path("table_comparison.json")))
    gen = json.load(open(_path("generation_A.json")))
    g6 = json.load(open(_path("g6_mixed_fixture.json")))
    reverify = json.load(open(_path("reverify.json")))
    timing = json.load(open(_path("g7b_timing_single_process.json")))
    tests = json.load(open(_path("unit_tests.json")))
    templates = validation["per_template"]
    supported = {t: v for t, v in templates.items() if E.TEMPLATES[t].expect_supported}
    gates = {}
    gates["G1"] = {"threshold": ">= 0.99 per template (gap5 excluded)",
                   "observed": {t: {"rate": v["success_rate"], "accepted": v.get("accepted", 0),
                                    "denominator": v["denominator"], "infeasible": v.get("infeasible", 0),
                                    "entry_invalid": v.get("entry_invalid", 0)} for t, v in supported.items()},
                   "pass": all(v["success_rate"] is not None and v["success_rate"] >= .99 for v in supported.values())}
    accepted_samples = sum(1 for s in samples if s["status"] == "accepted")
    unverified = accepted_samples - (reverify["accepted_candidates"] - reverify["reverified_rejected"])
    gates["G2"] = {"threshold": "every accepted candidate passes scan + two goal checks; table-only accepts == 0",
                   "observed": {"accepted_samples": accepted_samples, "accepted_candidates_file": reverify["accepted_candidates"],
                                "table_only_accepts": unverified,
                                "reverified_rejected": reverify["reverified_rejected"]},
                   "pass": unverified == 0 and reverify["reverified_rejected"] == 0}
    gates["G3"] = {"threshold": "rows keep bin->params->exit/boundary ranges; two generations byte-identical; <= 1 MB",
                   "observed": comparison, "pass": comparison["identical"] and comparison["size_le_1MB"]}
    gates["G4"] = {"threshold": "identity change -> load rejected (unit test)",
                   "observed": tests, "pass": tests["identity_tests_passed"]}
    cpu_hours = gen["cpu_seconds_total"] / 3600.
    steps_a = [s for task in gen["tasks"] for s in task["point_steps"]]
    supported_steps = [s for task in gen["tasks"] if E.TEMPLATES[task["template"]].expect_supported for s in task["point_steps"]]
    negative_steps = [s for task in gen["tasks"] if not E.TEMPLATES[task["template"]].expect_supported for s in task["point_steps"]]
    oracle_steps = [s["oracle_steps"] for s in samples if "oracle_steps" in s]
    gates["G5"] = {"threshold": "CPU <= 8 h; supported point <= 40,000 steps; gap5 point <= 200,000",
                   "observed": {"generation_cpu_hours": cpu_hours, "points": len(steps_a),
                                "max_supported_point_steps_collect_mode": max(supported_steps),
                                "supported_points_over_40000": sum(s > 40_000 for s in supported_steps),
                                "max_gap5_point_steps": max(negative_steps),
                                "validation_oracle_feasible_mode_runs": len(oracle_steps),
                                "max_validation_oracle_steps_incl_scans": max(oracle_steps, default=0),
                                "validation_oracle_runs_over_40000": sum(s > 40_000 for s in oracle_steps)},
                   "pass": (cpu_hours <= 8 and max(supported_steps) <= 40_000 and max(negative_steps) <= 200_000
                            and not any(s > 40_000 for s in oracle_steps))}
    gates["G6"] = {"threshold": "verified candidate on mixed fixture", "observed": g6,
                   "pass": bool(g6["feasible"]) and g6["verify"] is not None and g6["verify"][0] is True}
    gap5 = templates.get("gap5", {})
    gates["G7"] = {"threshold": "all gap5 validation entries typed unsupported, zero permits",
                   "observed": gap5, "pass": gap5.get("accepted", 0) == 0 and
                   gap5.get("unsupported", 0) + gap5.get("entry_invalid", 0) == sum(
                       v for k, v in gap5.items() if k in ("accepted", "unsupported", "entry_invalid", "table_miss",
                                                          "infeasible", "oracle_unresolved"))}
    action = [s["action_start_us"] / 1000. for s in samples if s.get("action_start_us") is not None
              and E.TEMPLATES[s["template"]].expect_supported]
    p95 = percentile(action, .95)
    gates["G7b"] = {"threshold": "P95 <= 15 ms, >= 200 samples",
                    "observed": {"validation_run_samples": len(action), "p50_ms": percentile(action, .5),
                                 "p95_ms": p95, "p99_ms": percentile(action, .99), "max_ms": max(action),
                                 "single_process_rerun": timing},
                    "pass": len(action) >= 200 and p95 <= 15.}
    _dump(gates, "gates.json")
    for name, gate in gates.items():
        print(name, "PASS" if gate["pass"] else "FAIL")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    g = sub.add_parser("generate"); g.add_argument("--tag", required=True); g.add_argument("--workers", type=int, default=3)
    g.add_argument("--reverse", action="store_true"); g.set_defaults(func=cmd_generate)
    sub.add_parser("compare").set_defaults(func=cmd_compare)
    v = sub.add_parser("validate"); v.add_argument("--workers", type=int, default=3); v.set_defaults(func=cmd_validate)
    r = sub.add_parser("reverify"); r.add_argument("--workers", type=int, default=3); r.set_defaults(func=cmd_reverify)
    sub.add_parser("g6").set_defaults(func=cmd_g6)
    t = sub.add_parser("timing"); t.add_argument("--per-bin", type=int, default=10); t.set_defaults(func=cmd_timing)
    sub.add_parser("gates").set_defaults(func=cmd_gates)
    sub.add_parser("table-summary").set_defaults(func=cmd_table_summary)
    arguments = parser.parse_args()
    arguments.func(arguments)
