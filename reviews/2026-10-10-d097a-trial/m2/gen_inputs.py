"""Generate verified inputs for Set A (frozen matrix) and Set C (random entries, seed 77).

Usage: python gen_inputs.py a | c
Writes set_a_inputs.json / set_c_inputs.json next to this file (inputs come from the project's
m0_probe.run_case; they are the candidates the project's own full scan accepted).
"""
from __future__ import annotations

import json
import sys
import time

import common
import entries as E
from experiments.motion_navigation.trajectory_proto import m0_probe
from experiments.motion_navigation.trajectory_proto.scenarios import RepresentativeFixture

SEED_C = 77
BINS_C = ("rest-L", "rest-R", "walk-C-s", "walk-L-n", "walk-R-p", "walk-L-p", "walk-R-n",
          "sprint-C-s", "sprint-L-n", "sprint-R-p")
SAMPLES_PER_BIN_C = 2


def gen_a():
    rows, meta = {}, {}
    for scenario_id, tier_id in common.set_a_cases():
        started = time.perf_counter()
        if scenario_id == "mixed_ground_jump_air":
            inputs = m0_probe.build_mixed_action_fixture().expected_inputs
            result = m0_probe.run_case(scenario_id, tier_id, options=m0_probe.FORMAL_OPTIONS)
            same = result.inputs == inputs
            status = result.status.value
        else:
            result = m0_probe.run_case(scenario_id, tier_id, options=m0_probe.FORMAL_OPTIONS)
            inputs, same, status = result.inputs, True, result.status.value
        key = common.case_key(scenario_id, tier_id)
        rows[key] = common.inputs_to_json(inputs)
        meta[key] = {"status": status, "ticks": len(inputs), "physics_steps": result.physics_steps,
                     "same_as_expected": same, "seconds": round(time.perf_counter() - started, 2)}
        print(key, meta[key], flush=True)
    common.SET_A_CACHE.write_text(json.dumps({"inputs": rows, "meta": meta}, indent=1), encoding="utf-8")


def gen_c():
    current = {}
    original = m0_probe._fixture_for

    def fixture_for(scenario_id, tier_id):
        if scenario_id == "trial":
            request, world = E.make_request(current["spec"])
            return RepresentativeFixture("trial", request, world, "d097a m2 set C")
        return original(scenario_id, tier_id)

    m0_probe._fixture_for = fixture_for
    bins = {b.bin_id: b for b in E.entry_bins()}
    out = []
    for template_id in E.SUPPORTED_TEMPLATES:
        for bin_id in BINS_C:
            # two samples per bin, seed 77 (not the registered 20261010/20261011)
            for spec in E.sample_entries(template_id, bins[bin_id], SAMPLES_PER_BIN_C, SEED_C):
                current["spec"] = spec
                started = time.perf_counter()
                try:
                    result = m0_probe.run_case("trial", None, options=m0_probe.FORMAL_OPTIONS)
                    status, inputs, steps = result.status.value, result.inputs, result.physics_steps
                except E.EntryInvalid as error:
                    status, inputs, steps = f"entry_invalid:{error}", (), 0
                row = {"template_id": template_id, "bin_id": bin_id, "gait": spec.gait,
                       "ticks": spec.ticks, "offset": spec.offset, "heading_deg": spec.heading_deg,
                       "status": status, "physics_steps": steps,
                       "seconds": round(time.perf_counter() - started, 2),
                       "inputs": common.inputs_to_json(inputs)}
                out.append(row)
                print(template_id, bin_id, status, len(inputs), steps, row["seconds"], flush=True)
    common.SET_C_CACHE.write_text(json.dumps({"seed": SEED_C, "rows": out}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    {"a": gen_a, "c": gen_c}[sys.argv[1]]()
