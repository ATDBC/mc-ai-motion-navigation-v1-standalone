"""Windows P1 baseline for one complete isolated D098 decision.

The measured total is lookup + controller + incremental proof + permit
validation.  It is an experiment metric, not a formal Runtime deadline.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import platform
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.motion_navigation.trajectory_proto import m0_probe
from experiments.motion_navigation.trajectory_proto.frozen_templates import EntrySpec, make_request
from experiments.motion_navigation.trajectory_proto.decision import (
    DecisionContext,
    FrozenInputAdapter,
    TableControllerAdapter,
    complete_decision,
)
from experiments.motion_navigation.trajectory_proto.motion_table import load_frozen_table
from experiments.motion_navigation.trajectory_proto.physics import trajectory_digest
from mc2p.motion_nav.physics_types import TickInput


DATA = ROOT / "experiments/motion_navigation/trajectory_proto/data"


@dataclass(frozen=True, slots=True)
class DecisionParts:
    lookup_ns: int
    controller_ns: int
    proof_ns: int
    permit_ns: int
    trace_ns: int = 0

    @property
    def total_ns(self) -> int:
        return (self.lookup_ns + self.controller_ns + self.trace_ns
                + self.proof_ns + self.permit_ns)


def _input(row) -> TickInput:
    forward, strafe, jump, sneak, sprint, yaw = row
    return TickInput(float(forward), float(strafe), bool(jump), bool(sneak), bool(sprint), float(yaw))


def _set_a(limit: int):
    payload = json.loads((DATA / "d097a-set-a-sample.json").read_text(encoding="utf-8"))["inputs"]
    preferred = ("flat_walk:A3", "jump_gap_1:A3", "turn_90:A15",
                 "jump_up_after_turn:A15")
    for key in preferred[:limit]:
        scenario, tier = key.split(":")
        fixture = m0_probe._fixture_for(scenario, tier or None)
        yield "A", key, fixture.request, fixture.world, tuple(_input(row) for row in payload[key]), None, None


def _set_c(limit: int):
    rows = json.loads((DATA / "d097a-set-c-sample.json").read_text(encoding="utf-8"))["rows"]
    selected = 0
    for row in rows:
        if row["status"] != "found":
            continue
        template = row["template_id"]
        spec = EntrySpec(template, row["gait"], row["ticks"], row["offset"], row["heading_deg"])
        request, world = make_request(spec)
        inputs = tuple(_input(item) for item in row["inputs"])
        yield "C", f"{template}/{row['bin_id']}/{selected}", request, world, inputs, None, None
        selected += 1
        if selected >= limit:
            break


def _set_d(limit: int):
    rows = json.loads((DATA / "d097a-accepted-sample.json").read_text(encoding="utf-8"))["rows"]
    for index, row in enumerate(rows[:limit]):
        spec = EntrySpec(row["template_id"], row["gait"], row["ticks"],
                         row["offset"], row["heading_deg"])
        request, world = make_request(spec)
        yield ("D", f"{row['template_id']}/{row['bin']}/{index}", request, world,
               tuple(_input(item) for item in row["inputs"]), row["bin"], row["template_id"])


def _semantic_signature(proof) -> str:
    return trajectory_digest((
        proof.status,
        proof.reason,
        tuple((branch.timing_branch, branch.states,
               tuple(sorted(branch.tails.items())), branch.risk_intervals,
               branch.damage_points) for branch in proof.branches),
        proof.dependencies,
        tuple((permit.kind, permit.boundary, permit.commands, permit.locked_count,
               permit.dependency_facts) for permit in proof.permits),
    ))


def measure_case(case, table, *, runahead_steps=None) -> dict:
    set_id, case_id, request, world, frozen_inputs, bin_id, template_id = case
    adapter = (TableControllerAdapter(table, template_id, bin_id, frozen_inputs)
               if set_id == "D" else FrozenInputAdapter(frozen_inputs))
    context = DecisionContext.create(request)
    started = time.perf_counter_ns()
    decision = complete_decision(
        context, request, world, adapter,
        proof_mode="strict" if runahead_steps is None else "strict_runahead",
        runahead_steps=60 if runahead_steps is None else runahead_steps,
    )
    total_ns = time.perf_counter_ns() - started
    if decision.status.value != "VERIFIED_CANDIDATE":
        raise RuntimeError(f"decision rejected {case_id}: {decision.status}/{decision.reason}")
    inputs = decision.inputs
    parts = DecisionParts(
        lookup_ns=decision.parts_ns["lookup"],
        controller_ns=decision.parts_ns["controller"],
        proof_ns=decision.parts_ns["proof"],
        permit_ns=decision.parts_ns["permit"],
        trace_ns=decision.parts_ns["trace"],
    )
    return {
        "set": set_id,
        "case": case_id,
        "input_ticks": len(inputs),
        "permits": len(decision.result.permits),
        "input_source": decision.input_source,
        "physics_steps": decision.physics_steps,
        "damage_state_updates": decision.result.damage_state_updates,
        "legacy_damage_state_visits": decision.result.legacy_damage_state_visits,
        "trace_reuse": (None if decision.result.trace_reuse is None
                         else decision.result.trace_reuse.status.value),
        "proof_mode": "strict" if runahead_steps is None else "strict_runahead",
        "runahead_steps": runahead_steps,
        "semantic_signature": _semantic_signature(decision.result),
        "decision_samples": [
            {"kind": sample.kind, "boundary": sample.boundary,
             "wall_ns": sample.wall_ns, "physics_steps": sample.physics_steps}
            for sample in decision.decision_timings
        ],
        "stage_physics_steps": decision.stage_physics_steps,
        "parts_ms": {key.removesuffix("_ns") + "_ms": value / 1_000_000.
                     for key, value in asdict(parts).items()},
        "decision_ms": total_ns / 1_000_000.,
        "measured_parts_ms": parts.total_ns / 1_000_000.,
    }


def _percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _decision_summary(rows, kind):
    samples = [sample for row in rows for sample in row["decision_samples"]
               if sample["kind"] == kind]
    values = [sample["wall_ns"] / 1_000_000. for sample in samples]
    return {
        "samples": len(values),
        "p50": None if not values else _percentile(values, .50),
        "p95": None if not values else _percentile(values, .95),
        "p99": None if not values else _percentile(values, .99),
        "max": None if not values else max(values),
        "physics_steps": sum(sample["physics_steps"] for sample in samples),
    }


def calibrate_runahead(table, *, budgets=(24, 32, 40, 48, 60), limit=6, rounds=20):
    cases = tuple(_set_a(limit)) + tuple(_set_c(limit))
    strict = {case[1]: measure_case(case, table)["semantic_signature"] for case in cases}
    budget_rows = []
    selected = None
    for budget in budgets:
        rows = [measure_case(case, table, runahead_steps=budget)
                for _ in range(rounds) for case in cases]
        if any(row["semantic_signature"] != strict[row["case"]] for row in rows):
            raise RuntimeError(f"runahead {budget} changed a frozen semantic signature")
        ordinary = _decision_summary(rows, "tick")
        commitment = _decision_summary(rows, "commit")
        passed = (ordinary["samples"] > 0 and commitment["samples"] > 0
                  and ordinary["p95"] <= 8. and commitment["p95"] <= 45.)
        budget_rows.append({
            "runahead_steps": budget,
            "rows": rows,
            "ordinary_ms": ordinary,
            "commitment_ms": commitment,
            "total_physics_steps": sum(row["physics_steps"] for row in rows),
            "semantic_signatures_match": True,
            "passes_calibration_gate": passed,
        })
        if selected is None and passed:
            selected = budget
    return {
        "schema": "d098-p2.3-runahead-calibration-v1",
        "calibration_sets": ["A", "C"],
        "budgets": budget_rows,
        "selected_runahead_steps": selected,
        "selection_rule": "first budget with ordinary P95 <= 8 ms and commitment P95 <= 45 ms",
    }


def verify_runahead_on_d(table, runahead_steps, *, limit=6, rounds=20):
    cases = tuple(_set_d(limit))
    strict = {case[1]: measure_case(case, table)["semantic_signature"] for case in cases}
    rows = [measure_case(case, table, runahead_steps=runahead_steps)
            for _ in range(rounds) for case in cases]
    if any(row["semantic_signature"] != strict[row["case"]] for row in rows):
        raise RuntimeError("frozen D verification changed a semantic signature")
    return {
        "schema": "d098-p2.3-runahead-d-verification-v1",
        "verification_set": "D",
        "frozen_runahead_steps": runahead_steps,
        "rows": rows,
        "ordinary_ms": _decision_summary(rows, "tick"),
        "commitment_ms": _decision_summary(rows, "commit"),
        "total_physics_steps": sum(row["physics_steps"] for row in rows),
        "semantic_signatures_match": True,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--calibrate-runahead", action="store_true")
    parser.add_argument("--verify-runahead", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.limit < 1 or args.rounds < 1:
        parser.error("limit and rounds must be positive")
    table = load_frozen_table()
    if args.calibrate_runahead and args.verify_runahead is not None:
        parser.error("choose calibration or D verification, not both")
    if args.calibrate_runahead:
        payload = calibrate_runahead(table, limit=args.limit, rounds=args.rounds)
        text = json.dumps(payload, sort_keys=True, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8", newline="\n")
        else:
            print(text, end="")
        return 0
    if args.verify_runahead is not None:
        payload = verify_runahead_on_d(
            table, args.verify_runahead, limit=args.limit, rounds=args.rounds)
        text = json.dumps(payload, sort_keys=True, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8", newline="\n")
        else:
            print(text, end="")
        return 0
    cases = tuple(_set_a(args.limit)) + tuple(_set_c(args.limit)) + tuple(_set_d(args.limit))
    rows = [measure_case(case, table) for _ in range(args.rounds) for case in cases]
    durations = [row["decision_ms"] for row in rows]
    payload = {
        "schema": "d098-p1-complete-decision-baseline-v2",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "rounds": args.rounds,
        "sets": sorted({row["set"] for row in rows}),
        "rows": rows,
        "summary_ms": {
            "samples": len(durations),
            "p50": _percentile(durations, .50),
            "p95": _percentile(durations, .95),
            "p99": _percentile(durations, .99),
            "max": max(durations),
        },
        "gate": "P1 baseline only; no 10/50 or 8/15/30 claim",
    }
    text = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8", newline="\n")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
