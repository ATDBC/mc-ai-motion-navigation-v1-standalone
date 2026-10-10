"""Candidate builders for Sets A-D (frozen matrix, rejected candidates, random entries, M1 output)."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import random

from experiments.motion_navigation.trajectory_proto.commitment import BoundaryInputs
from experiments.motion_navigation.trajectory_proto.contracts import KnownInputApplication, SearchBudget
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence
from experiments.motion_navigation.trajectory_proto.scenarios import primitive_fixture
from mc2p.motion_nav.world_model import BlockGeometry, CellFact, CellKnowledge

import common
import entries as E
import mutants

M1_ACCEPTED = Path(__file__).resolve().parent.parent / "m1" / "accepted_candidates.jsonl"


@dataclass
class Cand:
    set: str
    id: str
    request: object
    world: object
    inputs: tuple
    evidence: object = None


def set_a():
    inputs_by_key = common.load_set_a()
    out = []
    for scenario_id, tier_id in common.set_a_cases():
        key = common.case_key(scenario_id, tier_id)
        fixture = common.fixture_for(scenario_id, tier_id)
        out.append(Cand("A", key, fixture.request, fixture.world, inputs_by_key[key]))
    return out


def _spec_from_row(row) -> E.EntrySpec:
    return E.EntrySpec(row["template_id"], row["gait"], int(row["ticks"]), float(row["offset"]),
                       float(row["heading_deg"]))


def set_c():
    raw = json.loads(common.SET_C_CACHE.read_text(encoding="utf-8"))
    out = []
    for row in raw["rows"]:
        if row["status"] != "found":
            continue
        spec = _spec_from_row(row)
        request, world = E.make_request(spec)
        out.append(Cand("C", f"{spec.template_id}/{row['bin_id']}/{spec.ticks}",
                        request, world, common.inputs_from_json(row["inputs"])))
    return out


def set_d(path: Path = M1_ACCEPTED):
    out = []
    with open(path, encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if not line.strip():
                continue
            row = json.loads(line)
            spec = _spec_from_row(row)
            request, world = E.make_request(spec)
            out.append(Cand("D", f"{index}:{spec.template_id}/{spec.gait}/{spec.ticks}/{spec.offset:.4f}",
                            request, world, common.inputs_from_json(row["inputs"])))
    return out


# ------------------------------------------------------------------------------- Set B
SPECIAL_FIXTURES = (        # (fixture scenario, tier, source positive whose inputs are replayed)
    ("unknown_landing", None, "gap_start_1_width_1:A3"),
    ("unknown_landing", None, "jump_gap_continue:A3"),
    ("one_twelfth_support", None, "gap_start_1_width_1:A3"),
    ("one_twelfth_support", None, "jump_gap_continue:A3"),
    ("collision_only", None, "flat_walk:A3"),
    ("collision_only", None, "jump_gap_1:A3"),
    ("gap_start_4_width_3_a3", None, "gap_start_4_width_2:A3"),
    ("gap_start_4_width_3_a3", None, "gap_start_4_width_1:A3"),
    ("tiny_budget", None, "flat_walk:A3"),
)
FUZZ_FIXTURES = (("gap_start_1_width_1", "A15"), ("one_twelfth_support", "A15"),
                 ("unknown_landing", "A15"), ("collision_only", "A15"), ("turn_90", "A15"),
                 ("jump_up_after_turn", "A15"), ("gap_start_4_width_2", "A15"), ("flat_sprint", "A15"))


def request_level_cases():
    """Cheap rejections decided before/around the first physics step: classification equivalence."""
    fixture = common.fixture_for("gap_start_1_width_1", "A3")
    inputs = common.load_set_a()["gap_start_1_width_1:A3"]
    request, world = fixture.request, fixture.world
    good = _boundary_evidence(request, inputs)
    out = []

    def add(label, req=request, wld=world, cand=inputs, evidence=None):
        out.append(Cand("B", f"request_level/{label}", req, wld, cand, evidence))

    add("missing_late_prelude", replace(request, branch_preludes=((), None)))
    # tamper with one receipt of the late branch: wrong absolute tick -> STALE input ledger
    rows = [list(branch) for branch in good]
    bad = rows[1][3]
    rows[1][3] = BoundaryInputs(bad.boundary, bad.irrevocable_inputs, bad.absolute_tick + 1, bad.applications)
    add("ledger_tick_shift", evidence=tuple(tuple(branch) for branch in rows))
    rows = [list(branch) for branch in good]
    bad = rows[0][5]
    rows[0][5] = BoundaryInputs(bad.boundary, bad.irrevocable_inputs, bad.absolute_tick, None)
    add("missing_application_receipt", evidence=tuple(tuple(branch) for branch in rows))
    rows = [list(branch) for branch in good]
    bad = rows[0][2]
    shifted = tuple(replace(a, control_sequence=a.control_sequence + 1) for a in bad.applications)
    rows[0][2] = BoundaryInputs(bad.boundary, bad.irrevocable_inputs, bad.absolute_tick, shifted)
    add("ledger_sequence_shift", evidence=tuple(tuple(branch) for branch in rows))
    add("geometry_revision_changed", wld=common.edited_world(world, {}, geometry_revision=4))
    add("timing_branch_budget", replace(request, budget=SearchBudget(4096, 65536, 40, 1)))
    add("node_budget_5", replace(request, budget=SearchBudget(5, 65536, 40, 2)))
    add("physics_budget_100", replace(request, budget=SearchBudget(4096, 100, 40, 2)))
    add("physics_budget_300", replace(request, budget=SearchBudget(4096, 300, 40, 2)))
    walk = request.supported_inputs[0]
    add("tick_budget_41", cand=(walk,) * 41)
    # late branch prelude replays a different waiting input -> entry no longer matches
    prelude = request.branch_preludes[1][0]
    other = replace(prelude, tick_input=walk)
    add("prelude_input_mismatch", replace(request, branch_preludes=((), (other,))))
    # late entry moved: replay no longer reproduces it
    late = request.entry_states[1]
    moved = replace(late, position=(late.position[0] + .01, late.position[1], late.position[2]))
    add("late_entry_moved", replace(request, entry_states=(request.entry_states[0], moved)))
    stone = world.cell((0, 0, 0))
    add("landing_unsupported_block",
        wld=common.edited_world(world, {(0, 0, 2): CellFact(CellKnowledge.BLOCK, stone.stamp,
                                                            BlockGeometry.unsupported("minecraft:test"))}))
    add("landing_fluid_block",
        wld=common.edited_world(world, {(0, 0, 2): CellFact(CellKnowledge.BLOCK, stone.stamp,
                                                            BlockGeometry.full_cube("minecraft:water", fluid=True))}))
    return out


def drop_cands():
    fixture = common.fixture_for("flat_walk", "A3")
    return [Cand("B", f"drop/{label}", request, world, cand)
            for label, request, world, cand in mutants.drop_cases(fixture, None)]


def fuzz_cands(seed: int = 5, per_fixture: int = 100):
    rng = random.Random(seed)
    out = []
    for scenario_id, tier_id in FUZZ_FIXTURES:
        fixture = primitive_fixture(scenario_id, tier_id)
        alphabet, stop = fixture.request.supported_inputs, fixture.request.stop_input
        for index in range(per_fixture):
            seq = []
            target = rng.randint(8, 36)
            while len(seq) < target:
                seq += [rng.choice(alphabet)] * rng.randint(1, 6)
            seq = tuple(seq[:40])
            if rng.random() < .7:
                seq = seq[:rng.randint(6, len(seq))] + (stop,) * rng.randint(0, 14)
            out.append(Cand("B", f"fuzz/{scenario_id}/{index}", fixture.request, fixture.world, seq[:40]))
    return out


def set_b(fuzz_per_fixture: int = 100):
    out = []
    a_inputs = common.load_set_a()
    for cand in set_a():
        for label, inputs in mutants.input_mutants(cand.inputs, cand.request.supported_inputs,
                                                   cand.request.stop_input, seed_key=cand.id):
            out.append(Cand("B", f"mutant/{cand.id}/{label}", cand.request, cand.world, inputs))
    for scenario_id, tier_id, source in SPECIAL_FIXTURES:
        fixture = common.fixture_for(scenario_id, tier_id)
        base = a_inputs[source]
        out.append(Cand("B", f"fixture/{scenario_id}/{source}/orig", fixture.request, fixture.world, base))
        for label, inputs in mutants.input_mutants(base, fixture.request.supported_inputs,
                                                   fixture.request.stop_input, seed_key=scenario_id + source):
            out.append(Cand("B", f"fixture/{scenario_id}/{source}/{label}", fixture.request,
                            fixture.world, inputs))
    out += request_level_cases()
    out += drop_cands()
    out += fuzz_cands(per_fixture=fuzz_per_fixture)
    out += double_gap_cands()
    return out


# ---------------------------------------------------------------- hand-built witnesses for tests
LABELS = {"W": dict(forward=1., jump=False, sprint=False), "J": dict(forward=1., jump=True, sprint=False),
          "N": dict(forward=0., jump=False, sprint=False), "S": dict(forward=1., jump=False, sprint=True),
          "SJ": dict(forward=1., jump=True, sprint=True)}


def parse_labels(text: str, request):
    """'W W J N' -> project TickInputs of the request's alphabet (heading 0)."""
    from mc2p.motion_nav.physics_types import TickInput
    out = []
    for token in text.split():
        spec = LABELS[token]
        command = TickInput(spec["forward"], 0., spec["jump"], False, spec["sprint"], 0.)
        if command not in request.supported_inputs:
            raise ValueError(f"{token} is not in the request alphabet")
        out.append(command)
    return tuple(out)


def double_gap_fixture(tier: str = "A5"):
    """Two one-block holes (z=1 and z=4) with a two-block platform between them."""
    base = common.fixture_for("gap_start_1_width_1", tier)
    stone = base.world.cell((0, 0, 0))
    air = CellFact(CellKnowledge.AIR, stone.stamp, None)
    world = common.edited_world(base.world, {(x, 0, 4): air for x in range(-9, 10)})
    return replace(base, world=world)


def double_gap_cands():
    """Hop - land on the middle platform - hop again, with the second jump placed around the landing
    boundary.  Many verify with ONE risk interval spanning both hops (the landing boundary has a jump
    in flight, so it cannot close the interval)."""
    from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
    fixture = double_gap_fixture("A5")
    request, world = fixture.request, fixture.world
    W, S, J, SJ, N = (parse_labels(t, request)[0] for t in ("W", "S", "J", "SJ", "N"))
    counter = CountedPhysics(SearchBudget(10 ** 7, 10 ** 8, 40, 2))
    out = []
    for g in (W, S):
        for a in (2, 4, 6, 8, 10, 12):
            for j1 in (J, SJ):
                probe = (g,) * a + (j1,) + (g,) * 20
                state, states = request.entry_states[0], [request.entry_states[0]]
                for command in probe:
                    state = counter.step(state, command, world).next_state
                    states.append(state)
                landing = next((i for i in range(a + 2, len(states)) if states[i].on_ground), None)
                if landing is None:
                    continue
                for delta in (-1, 0, 1, 2):
                    for j2 in (J, SJ):
                        for g2 in (W, N):
                            inputs = (probe[:landing + delta] + (j2,) + (g2,) * 2 + (N,) * 16)[:40]
                            out.append(Cand("B", f"double_gap/{'W' if g == W else 'S'}{a}/"
                                                 f"{'J' if j1 == J else 'SJ'}/d{delta}/"
                                                 f"{'J' if j2 == J else 'SJ'}/{'W' if g2 == W else 'N'}",
                                            request, world, inputs))
    return out
