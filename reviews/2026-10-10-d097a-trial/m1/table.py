"""D097-A M1: offline table generation, serialisation and identity-checked loading.

Fixed generator rules (decided before any validation run, recorded in the table identity):
  * candidates: oracle "collect" at the bin centre + corners. Params that produce identical
    candidate inputs at every point they solve are one behaviour (N-air duplicates, trigger values on
    the same tick); the best one per (ground gait, jump gait, trigger value) is kept, ranked by
    (points solved desc, worst goal-region slack desc, worst trigger margin desc, canonical key).
  * pool: the (ground gait, jump gait) families are interleaved round-robin in that rank order
    (so no family is crowded out by landing-slack ranking) and cut at POOL_SIZE = 30. Each pool member
    is then verified (verify() = goal checks + full scan) at every point it solved by the cheap check;
    the pool is ordered by (points ACCEPTED desc, interleave position).
  * every pool Params is evaluated on the 200 TRAINING samples (rollout + potential goal + per-branch
    goal check); an entry-invalid training sample is skipped and counted.
  * row = at most MAX_PARAMS = 3 ordered Params by greedy set cover on cheap coverage with a scan
    veto: each round pool members are examined in order of cheap gain over the still uncovered
    samples (ties: pool order). Examining one means running verify() on its newly covered samples. The
    first one whose confirmed fraction is >= GOOD_FRACTION = 0.95 wins; otherwise examination stops
    before the round's scan budget ROUND_SCAN_BUDGET = 400 would be exceeded and the examined one with
    the most confirmed samples wins (ties: pool order), i.e. a Params the scan rejects often gives way
    to the next best. A sample is covered only when verify() accepts. Rounds stop when nothing
    confirmed is gained. A row with no Params is "unsupported".
  * row statistics (exit band, commitment/recovery boundary ranges) come from the scans of the
    training samples assigned to each chosen Params (first Params, in row order, that confirms them).
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import multiprocessing
import os
import time

import controller as C
import entries as E
import oracle as O
from controller import Params, Tree

POOL_SIZE = 30
MAX_PARAMS = 3
ROUND_SCAN_BUDGET = 400
GOOD_FRACTION = 0.95
CAP_SUPPORTED = 40_000
CAP_NEGATIVE = 200_000
SCHEMA = "d097a-m1-table-v1"
GENERATOR_FILES = ("controller.py", "oracle.py", "table.py")
TEMPLATE_ORDER = tuple(E.TEMPLATES)
GENERATION_RULES = {
    "pool_size": POOL_SIZE, "max_params": MAX_PARAMS, "round_scan_budget": ROUND_SCAN_BUDGET, "good_fraction": GOOD_FRACTION,
    "pool_rank": "behaviour_dedupe;best_per_family_trigger;family_round_robin;order_by_accepted_corner_points",
    "row_rule": "cheap_greedy_cover_with_scan_veto_scan_budget_per_round",
    "bin_points": "centre_plus_corners", "caps": [CAP_SUPPORTED, CAP_NEGATIVE],
}


class IdentityMismatch(Exception):
    pass


def _file_sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def generator_hash() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    digest = hashlib.sha256()
    for name in GENERATOR_FILES:
        digest.update(name.encode() + b"\0" + _file_sha256(os.path.join(here, name)).encode() + b"\n")
    return digest.hexdigest()


def current_identity() -> dict:
    import mc2p.motion_nav.physics_1_21 as physics
    from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
    identity = E.identity()
    identity.update({
        "ruleset": hashlib.sha256(repr(JAVA_1_21_RULESET).encode()).hexdigest(),
        "ruleset_impl": _file_sha256(physics.__file__),
        "generator": generator_hash(),
        "grid": C.grid_hash(),
        "samples": [E.SAMPLES_PER_BIN, E.TRAIN_SEED],
        "rules": hashlib.sha256(json.dumps(GENERATION_RULES, sort_keys=True).encode()).hexdigest(),
        "pool_size": POOL_SIZE,
    })
    return identity


# ---------------------------------------------------------------- row construction

def summarize_scan(inputs, scan) -> dict:
    final = scan.proof.branches[0].states[-1]
    return {
        "speed": 20. * math.hypot(final.velocity_blocks_per_tick[0], final.velocity_blocks_per_tick[2]),
        "x": final.position[0], "z": final.position[2], "ticks": len(inputs),
        "risk": [[b, o, -1 if i.last_abandon_boundary is None else i.last_abandon_boundary,
                  i.first_committed_boundary, i.recovered_boundary]
                 for b, branch in enumerate(scan.proof.branches) for o, i in enumerate(branch.risk_intervals)],
    }


def _range(values):
    return [min(values), max(values)] if values else None


def _stats(summaries) -> dict:
    risk = {}
    for summary in summaries:
        for branch, ordinal, abandon, committed, recovered in summary["risk"]:
            risk.setdefault((branch, ordinal), []).append((abandon, committed, recovered))
    return {
        "samples": len(summaries),
        "exit_speed_bps": _range([s["speed"] for s in summaries]),
        "exit_x": _range([s["x"] for s in summaries]),
        "exit_z": _range([s["z"] for s in summaries]),
        "ticks": _range([s["ticks"] for s in summaries]),
        "risk": [{"branch": b, "ordinal": o, "n": len(rows),
                  "last_abandon": _range([r[0] for r in rows]),
                  "first_committed": _range([r[1] for r in rows]),
                  "recovered": _range([r[2] for r in rows])} for (b, o), rows in sorted(risk.items())],
    }


def _pool(template_id: str, entry_bin, geometry, cap: int):
    """Returns (pool, accepted corner points per pool member, cheap-tally, oracle steps, invalid, behaviours)."""
    tally, steps, invalid, points = {}, [], 0, {}
    for point_index, spec in enumerate(E.bin_corner_points(template_id, entry_bin)):
        try:
            points[point_index] = E.make_request(spec)
        except E.EntryInvalid:
            invalid += 1
            continue
        point = O.enumerate_point(*points[point_index], geometry, mode="collect", cap=cap)
        steps.append(point.steps)
        for params, (margin, slack, behaviour) in point.passing.items():
            count, worst_slack, worst_margin, trace = tally.get(params, (0, math.inf, math.inf, ()))
            tally[params] = (count + 1, min(worst_slack, slack), min(worst_margin, margin),
                             trace + ((point_index, behaviour),))
    behaviours = {}                       # identical candidates at every solved point -> one entry
    for params, (count, slack, margin, trace) in tally.items():
        kept = behaviours.get(trace)
        if kept is None or (-margin, params.sort_key()) < (-tally[kept][2], kept.sort_key()):
            behaviours[trace] = params

    def rank(p):
        return (-tally[p][0], -tally[p][1], -tally[p][2], p.sort_key())
    best = {}                             # best behaviour per (ground gait, jump gait, trigger value)
    for params in behaviours.values():
        key = (params.ground_gait, params.jump_gait or "", params.takeoff_d if params.takeoff_d is not None
               else params.brake_b)
        if key not in best or rank(params) < rank(best[key]):
            best[key] = params
    families = {}
    for key, params in best.items():
        families.setdefault(key[:2], []).append(params)
    for members in families.values():
        members.sort(key=rank)
    shortlist = []
    for depth in range(max((len(m) for m in families.values()), default=0)):
        for family in sorted(families, key=lambda f: rank(families[f][0])):
            if depth < len(families[family]):
                shortlist.append(families[family][depth])
    shortlist = shortlist[:POOL_SIZE]
    accepted = {}
    for params in shortlist:
        accepted[params] = sum(
            C.verify(*points[index], tuple(E.ALPHABET[i] for i in behaviour))[0]
            for index, behaviour in tally[params][3])
    pool = sorted(shortlist, key=lambda p: (-accepted[p], shortlist.index(p)))
    return pool, accepted, tally, steps, invalid, len(behaviours)


def build_row(template_id: str, entry_bin) -> tuple[dict, dict]:
    started = time.process_time()
    geometry = C.Geometry.for_template(template_id)
    cap = CAP_SUPPORTED if E.TEMPLATES[template_id].expect_supported else CAP_NEGATIVE
    pool, accepted, tally, point_steps, invalid_points, behaviours = _pool(template_id, entry_bin, geometry, cap)
    row = {"template": template_id, "bin": entry_bin.bin_id,
           "points": {"n": len(point_steps), "invalid": invalid_points, "steps": point_steps,
                      "over_cap": sum(s > cap for s in point_steps), "solved_by_any": len(tally), "behaviours": behaviours,
                      "best_points_solved": max((v[0] for v in tally.values()), default=0),
                      "pool_accepted_points": [accepted[p] for p in pool]},
           "pool_size": len(pool), "train": None, "params": [], "status": "unsupported"}
    if pool:
        samples = E.sample_entries(template_id, entry_bin, E.SAMPLES_PER_BIN, E.TRAIN_SEED)
        entries, passes, inputs, invalid = {}, [set() for _ in pool], {}, 0
        for index, spec in enumerate(samples):
            try:
                entries[index] = E.make_request(spec)
            except E.EntryInvalid:
                invalid += 1
                continue
            tree = Tree(*entries[index], geometry)
            for j, params in enumerate(pool):
                node, reason = C.evaluate(tree, params)
                if node is not None:
                    passes[j].add(index)
                    inputs[j, index] = node.inputs
        cache = {}

        def confirm(j, index):
            if (j, index) not in cache:
                ok, why, scan = C.verify(*entries[index], inputs[j, index])
                cache[j, index] = summarize_scan(inputs[j, index], scan) if ok else None
            return cache[j, index]

        covered, chosen = set(), []
        for _ in range(MAX_PARAMS):
            examined, spent = [], 0                        # (confirmed gain, -pool rank, pool rank, samples)
            for j in sorted((j for j in range(len(pool)) if j not in chosen),
                            key=lambda j: (-len(passes[j] - covered), j)):
                fresh = sorted(passes[j] - covered)
                if not fresh or (examined and spent + len(fresh) > ROUND_SCAN_BUDGET):
                    break
                newly = {i for i in fresh if confirm(j, i)}
                spent += len(fresh)
                examined.append((len(newly), -j, j, newly))
                if len(newly) >= GOOD_FRACTION * len(fresh):
                    break
            if not examined or max(examined)[0] == 0:
                break
            gain, _, j, newly = max(examined)
            chosen.append(j)
            covered |= newly
        assigned = {j: [] for j in chosen}
        for index in sorted(covered):
            for j in chosen:
                if index in passes[j] and confirm(j, index):
                    assigned[j].append(cache[j, index])
                    break
        row["train"] = {"samples": E.SAMPLES_PER_BIN, "invalid": invalid, "covered": len(covered),
                        "pool_cheap_union": len(set().union(*passes)),
                        "pool_cheap_best": max(len(p) for p in passes), "scans": len(cache),
                        "scans_rejected": sum(v is None for v in cache.values())}
        row["params"] = [{"params": pool[j].to_dict(), "pool_rank": j, "cheap_pass": len(passes[j]),
                          "assigned": _stats(assigned[j])} for j in chosen]
        row["status"] = "supported" if chosen else "unsupported"
    return row, {"cpu_seconds": time.process_time() - started, "point_steps": point_steps}


def _task(task):
    template_id, bin_id = task
    entry_bin = next(b for b in E.entry_bins() if b.bin_id == bin_id)
    row, stats = build_row(template_id, entry_bin)
    return task, row, stats


def generate(workers: int = 3, reverse: bool = False, log=print) -> tuple[dict, dict]:
    """Return (table, generation stats). The table is independent of worker count and task order."""
    tasks = [(t, b.bin_id) for t in TEMPLATE_ORDER for b in E.entry_bins()]
    if reverse:
        tasks.reverse()
    started, results = time.time(), {}
    with multiprocessing.Pool(workers) as pool:
        for task, row, stats in pool.imap_unordered(_task, tasks, chunksize=1):
            results[task] = (row, stats)
            log(f"[{len(results)}/{len(tasks)}] {task[0]}/{task[1]} {row['status']} "
                f"covered={row['train'] and row['train']['covered']} cpu={stats['cpu_seconds']:.0f}s "
                f"maxsteps={max(stats['point_steps'], default=0)}")
    order = [(t, b.bin_id) for t in TEMPLATE_ORDER for b in E.entry_bins()]
    table = {"schema": SCHEMA, "identity": current_identity(), "generation_rules": GENERATION_RULES,
             "rows": [results[key][0] for key in order]}
    stats = {"wall_seconds": time.time() - started, "workers": workers, "reverse_order": reverse,
             "cpu_seconds_total": sum(results[key][1]["cpu_seconds"] for key in order),
             "tasks": [{"template": key[0], "bin": key[1], "cpu_seconds": results[key][1]["cpu_seconds"],
                        "point_steps": results[key][1]["point_steps"]} for key in order]}
    return table, stats


# ---------------------------------------------------------------- serialisation / loading

def serialize(table: dict) -> str:
    return json.dumps(table, sort_keys=True, indent=1, ensure_ascii=True) + "\n"


def write_table(table: dict, path: str) -> str:
    text = serialize(table)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class Table:
    rows: dict                       # (template, bin) -> row dict
    sha256: str

    def params(self, template_id: str, bin_id: str) -> list[Params]:
        return [Params.from_dict(entry["params"]) for entry in self.rows[template_id, bin_id]["params"]]


def load_table(path: str, identity: dict | None = None) -> Table:
    """Load a table; reject it unless its bound identity equals the current code/data identity (G4)."""
    with open(path, "rb") as handle:
        raw = handle.read()
    table = json.loads(raw)
    expected = identity if identity is not None else current_identity()
    if table.get("schema") != SCHEMA:
        raise IdentityMismatch(f"schema {table.get('schema')!r}")
    if table.get("identity") != expected:
        differing = sorted(k for k in set(expected) | set(table.get("identity", {}))
                           if expected.get(k) != table.get("identity", {}).get(k))
        raise IdentityMismatch(f"table identity differs in {differing}")
    return Table({(row["template"], row["bin"]): row for row in table["rows"]}, hashlib.sha256(raw).hexdigest())
