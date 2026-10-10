"""D097-A M1 unit tests. Run (from the project checkout, PYTHONPATH as in run_m1.py):
    python3 test_m1.py      # writes unit_tests.json next to this file
They only use TRAIN-seed or synthetic entries, never the validation seed.
"""
import copy
import json
import math
import os
import random
import tempfile
import unittest
from unittest import mock

import controller as C
import entries as E
import oracle as O
import table as T

HERE = os.path.dirname(os.path.abspath(__file__))
BINS = {b.bin_id: b for b in E.entry_bins()}


def _entry(template_id, bin_id, index=0):
    spec = E.bin_corner_points(template_id, BINS[bin_id])[index]
    request, world = E.make_request(spec)
    return request, world, C.Geometry.for_template(template_id)


class ControllerTests(unittest.TestCase):
    def test_deterministic_and_prefix_sharing_does_not_change_result(self):
        request, world, geometry = _entry("gap2", "walk-C-s")
        params = C.Params("W", 0.15, "SJ", "W", 5)
        first = C.evaluate(C.Tree(request, world, geometry), params)
        second = C.evaluate(C.Tree(request, world, geometry), params)
        shared = C.Tree(request, world, geometry)
        for other in (C.Params("S", 0.2, "WJ", "S", 3), C.Params("W", None, brake_b=1.0), params):
            last = C.evaluate(shared, other)
        self.assertEqual(first[1], "ok")
        self.assertEqual(first[0].inputs, second[0].inputs)
        self.assertEqual(first[0].inputs, last[0].inputs)
        self.assertEqual(first[0].states, last[0].states)

    def test_steering_minimises_heading_error_with_alphabet_tie_break(self):
        request, world, geometry = _entry("turn", "rest-C")
        tree = C.Tree(request, world, geometry)
        state = request.entry_states[0]
        for gait in C.GROUND_GAITS:
            choice = tree.steer(gait, state)
            candidates = [c for _, _, c in tree.gaits[gait]]
            best = min(candidates, key=lambda c: (E.heading_error(state, c, "turn"), E.ALPHABET.index(c)))
            self.assertEqual(choice, best)
        # the goal lies ~78 degrees to the west of south: walk heading pi/2 (alphabet index 5) wins
        self.assertEqual(E.ALPHABET.index(tree.steer("W", state)), 5)
        # sprint has the single south heading
        self.assertEqual(E.ALPHABET.index(tree.steer("S", state)), 3)
        rng = random.Random(7)
        for _ in range(50):
            moved = state.__class__(**{**{f: getattr(state, f) for f in state.__dataclass_fields__},
                                       "position": (rng.uniform(-3, 3), 1., rng.uniform(-3, 3))})
            pick = tree.steer("W", moved)
            self.assertEqual(pick, min((c for _, _, c in tree.gaits["W"]),
                                       key=lambda c: (E.heading_error(moved, c, "turn"), E.ALPHABET.index(c))))

    def test_geometry_matches_entries_helpers(self):
        request, world, geometry = _entry("gap3", "sprint-R-p")
        state = request.entry_states[0]
        self.assertEqual(geometry.feature_distance(state), E.feature_distance(state, "gap3"))
        self.assertEqual(geometry.goal_distance(state), E.horizontal_goal_distance(state, "gap3"))

    def test_jump_only_when_every_branch_is_grounded(self):
        request, world, geometry = _entry("gap2", "walk-C-s")
        # reference line far beyond the gap: the player walks off the edge before the trigger can fire
        far = C.Geometry(30., geometry.goal_centre)
        node, reason = C.ground_phase(C.Tree(request, world, far), C.Params("W", -0.3, "WJ", "N", 0))
        self.assertIsNone(node)
        self.assertEqual(reason, C.LEFT_GROUND)
        # no trigger within 30 ticks on flat ground
        request, world, geometry = _entry("turn", "rest-C")
        node, reason = C.ground_phase(C.Tree(request, world, C.Geometry(60., geometry.goal_centre)),
                                      C.Params("W", -0.3, "WJ", "N", 0))
        self.assertEqual((node, reason), (None, C.NO_TRIGGER))
        # a found candidate: all branches are grounded just before the jump tick
        request, world, geometry = _entry("gap2", "walk-C-s")
        tree = C.Tree(request, world, geometry)
        found, why = C.evaluate(tree, C.Params("W", 0.15, "WJ", "W", 5))
        if found is None:                       # pick any passing parameter set instead
            point = O.enumerate_point(request, world, geometry, mode="collect")
            found, why = C.evaluate(tree, next(iter(point.passing)))
        node, jump_index = tree.root, [i for i, c in enumerate(found.inputs) if c.jump][0]
        self.assertEqual(len([c for c in found.inputs if c.jump]), 1)
        for command in found.inputs[:jump_index]:
            node = tree.advance(node, command)
        self.assertTrue(all(s.on_ground for s in node.states))

    def test_verify_accepts_mixed_fixture_candidate(self):
        from experiments.motion_navigation.trajectory_proto import m0_probe
        fixture = m0_probe.build_mixed_action_fixture()
        accepted, why, scan = C.verify(fixture.fixture.request, fixture.fixture.world, fixture.expected_inputs)
        self.assertTrue(accepted, why)

    def test_mixed_fixture_g6_expected_candidate_found(self):
        from experiments.motion_navigation.trajectory_proto import m0_probe
        fixture = m0_probe.build_mixed_action_fixture()
        geometry = C.Geometry(1.0, (0.5, 1.0, 3.341460582123732))
        result = O.enumerate_point(fixture.fixture.request, fixture.fixture.world, geometry, mode="feasible")
        self.assertTrue(result.feasible)
        self.assertEqual(result.first[1], fixture.expected_inputs)


class OracleTests(unittest.TestCase):
    def _brute_force(self, request, world, geometry):
        tree = C.Tree(request, world, geometry)
        grid = [C.Params(g, None, brake_b=b) for g in C.GROUND_GAITS for b in C.BRAKE_GRID]
        if geometry.feature_z is not None:
            grid += [C.Params(g, d, jg, ag, a) for g in C.GROUND_GAITS for d in C.TAKEOFF_GRID
                     for jg in C.JUMP_GAITS for ag in C.AIR_GAITS for a in C.AIR_TICKS]
        return {p for p in grid if C.evaluate(tree, p)[0] is not None}, len(grid)

    def test_oracle_collect_equals_brute_force_over_whole_grid(self):
        for template_id, bin_id in (("turn", "walk-C-s"), ("jumpup", "sprint-R-p")):
            request, world, geometry = _entry(template_id, bin_id)
            point = O.enumerate_point(request, world, geometry, mode="collect")
            expected, size = self._brute_force(request, world, geometry)
            self.assertEqual(set(point.passing), expected, template_id)
            self.assertGreater(len(expected), 0)
            if template_id == "jumpup":
                self.assertEqual(size, 2 * 37 * 2 * 3 * 17 + 2 * 21)

    def test_feasible_mode_agrees_with_collect_and_infeasible_gap5(self):
        request, world, geometry = _entry("jumpup", "rest-C")
        self.assertTrue(O.enumerate_point(request, world, geometry, mode="feasible").feasible)
        request, world, geometry = _entry("gap5", "rest-C")
        result = O.enumerate_point(request, world, geometry, mode="feasible", cap=200_000)
        self.assertFalse(result.feasible)
        self.assertFalse(result.over_cap)

    def test_cap_is_recorded_not_silently_dropped(self):
        request, world, geometry = _entry("jumpup", "rest-C")
        result = O.enumerate_point(request, world, geometry, mode="collect", cap=100)
        self.assertTrue(result.over_cap)
        self.assertGreater(result.steps, 100)
        self.assertGreater(len(result.passing), 0)


class TableTests(unittest.TestCase):
    def _mini(self):
        row = {"template": "turn", "bin": "rest-C", "status": "unsupported", "params": []}
        return {"schema": T.SCHEMA, "identity": T.current_identity(), "generation_rules": T.GENERATION_RULES,
                "rows": [row]}

    def _write(self, table):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, newline="\n")
        handle.write(T.serialize(table))
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_identity_matching_table_loads(self):
        loaded = T.load_table(self._write(self._mini()))
        self.assertIn(("turn", "rest-C"), loaded.rows)

    def test_identity_change_in_any_field_rejects_load(self):
        for key in T.current_identity():
            table = self._mini()
            value = table["identity"][key]
            table["identity"][key] = value + 1 if isinstance(value, int) else ("x" + str(value) if isinstance(value, str) else [0])
            with self.assertRaises(T.IdentityMismatch, msg=key):
                T.load_table(self._write(table))

    def test_current_code_changes_reject_a_previously_written_table(self):
        path = self._write(self._mini())
        T.load_table(path)
        changed = copy.deepcopy(E.TEMPLATES)
        changed["gap2"] = E.Template("gap2", (4, 6), None, (.3, 1., 6.02, .7, 1.05, 7.2), 0., 0., 4.)
        with mock.patch.object(E, "TEMPLATES", changed):             # template change
            with self.assertRaises(T.IdentityMismatch):
                T.load_table(path)
        with mock.patch.object(C, "TAKEOFF_GRID", C.TAKEOFF_GRID[:-1]):   # parameter grid change
            with self.assertRaises(T.IdentityMismatch):
                T.load_table(path)
        with mock.patch.object(T, "generator_hash", lambda: "0" * 64):    # generator change
            with self.assertRaises(T.IdentityMismatch):
                T.load_table(path)
        with mock.patch.object(E, "SAMPLES_PER_BIN", 199):                # sample definition change
            with self.assertRaises(T.IdentityMismatch):
                T.load_table(path)
        with mock.patch.object(T, "POOL_SIZE", 29):                       # pool size change
            with self.assertRaises(T.IdentityMismatch):
                T.load_table(path)

    def test_serialization_is_deterministic_lf_and_round_trips(self):
        table = self._mini()
        shuffled = {k: table[k] for k in reversed(list(table))}
        shuffled["identity"] = {k: table["identity"][k] for k in reversed(list(table["identity"]))}
        self.assertEqual(T.serialize(table), T.serialize(shuffled))
        text = T.serialize(table)
        self.assertNotIn("\r", text)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(json.loads(text), json.loads(json.dumps(table)))
        path = self._write(table)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), text.encode())

    def test_params_round_trip_and_validation(self):
        for params in (C.Params("S", 0.25, "SJ", "N", 7), C.Params("W", None, brake_b=0.3)):
            self.assertEqual(C.Params.from_dict(json.loads(json.dumps(params.to_dict()))), params)
        with self.assertRaises(ValueError):
            C.Params("W", 0.33, "WJ", "W", 1)           # off-grid trigger
        with self.assertRaises(ValueError):
            C.Params("W", None, "WJ", None, 0, 0.5)     # jump gait without jump

    def test_row_structure_and_size_bound_for_a_real_row(self):
        row, stats = T.build_row("turn", BINS["rest-C"])
        self.assertEqual(row["status"], "supported")
        entry = row["params"][0]
        self.assertIn("assigned", entry)
        for key in ("exit_speed_bps", "exit_x", "exit_z", "ticks", "risk"):
            self.assertIn(key, entry["assigned"])
        self.assertLess(len(json.dumps(row)) * 147, 1_000_000)

    def test_generated_table_size_bound_if_present(self):
        path = os.path.join(HERE, "table.json")
        if not os.path.exists(path):
            self.skipTest("no frozen table yet")
        self.assertLessEqual(os.path.getsize(path), 1_000_000)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__))
    # Reviewer fix: collect ids before running; Python 3.11 TestSuite drops tests after they run.
    ids = [test.id().split(".")[-1] for group in suite for test in group]
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = {str(t[0]) for t in result.failures + result.errors}
    names = {name: not any(name in f for f in failed) for name in ids}
    summary = {"ran": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "skipped": len(result.skipped), "per_test": names,
               "identity_tests_passed": all(v for k, v in names.items() if "identity" in k)}
    with open(os.path.join(HERE, "unit_tests.json"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, sort_keys=True, indent=1) + "\n")
    raise SystemExit(not result.wasSuccessful())
