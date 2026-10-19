"""Check the F2 ruler itself; future production diagnostics remain explicitly RED."""
from dataclasses import asdict, replace
import json
import unittest

from scripts.f2_ground_route_evidence import MANIFEST, digest, gate_violations, measure_frame, run_route


def actual_error_copy(name):
    """Generate real calculator transitions, then run the shared measurement entry."""
    from mc2p.contracts.action_v1 import MovementV1
    from mc2p.motion_nav.online_motion import project_movement_command
    from mc2p.motion_nav.physics_1_21 import step
    from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET
    from tests.sim.backend import CalculatorBackend
    from tests.sim.product_cases import f2_ground_route_scene
    family = {"head_on_mid_route_wall": "head_wall", "unsafe_contact": "hazard_wall"}.get(name, "offset")
    manifest = json.loads(MANIFEST.read_text("utf-8"))
    case = next(c for c in manifest["tasks"] if c["id"] == f"f2/{family}/south/normal")
    scene = f2_ground_route_scene(case)
    backend = CalculatorBackend([0], scene, tuple(case["start"]))
    before = backend.state
    movement = MovementV1(forward=1)
    terminal = name == "terminal_sneak"
    phase = "settling" if terminal or name == "neutral_tail_outside_corridor" else "active"
    if name == "head_on_mid_route_wall":
        before = replace(before, position=(.7, 64., 2.7), velocity_blocks_per_tick=(0., -.0784, .18))
    elif name == "unsafe_contact":
        before = replace(before, position=(.7, 64., 2.65), velocity_blocks_per_tick=(0., -.0784, .18))
    elif name in {"outside_corridor", "neutral_tail_outside_corridor"}:
        before = replace(before, position=(1.08, 64., 2.), velocity_blocks_per_tick=(.18, -.0784, 0.))
        if name == "neutral_tail_outside_corridor":
            movement = MovementV1()
    elif name in {"undeclared_sneak", "terminal_sneak"}:
        before = replace(before, position=(.7, 64., 6.5 if terminal else 2.),
                         sneaking=True, pose="crouching", body_height=1.5)
        movement = MovementV1(sneak=True)
    if name == "unsafe_contact":
        # The calculator rejects magma; an observed erroneous contact is a raw
        # state copy, not a claim that hazardous movement is supported.
        after = replace(before, position=(.7, 64., 2.7), horizontal_collision=True,
                        velocity_blocks_per_tick=(0., -.0784, 0.))
    else:
        projected = project_movement_command(before, movement)
        result = step(before, projected.tick_input, backend.world, JAVA_1_21_RULESET)
        assert result.status is CalculationStatus.OK
        after = result.next_state
    diagnostics = dict(full_candidates=4 if name == "full_candidate_limit" else None,
                       physics_steps=None)
    measured = measure_frame(before, after, movement, case, scene,
                             tick=1, responsibility=phase, terminal=terminal, **diagnostics)
    return dict(id=name, case_id=case["id"], before=before.to_mapping(),
                after=after.to_mapping(), applied=asdict(movement),
                state_source="injected_observation_error_copy" if name == "unsafe_contact" else "physics_1_21.step",
                responsibility=phase, terminal=terminal, diagnostics=diagnostics,
                diagnostics_source="synthetic_future_schema" if name == "full_candidate_limit" else "missing_RED",
                measured=measured, expected_violation="outside_corridor" if name == "neutral_tail_outside_corridor" else name)


def safe_frames():
    return [dict(tick=1, moving=True, progress=1., progress_delta=.1,
                 cross_track=.1, contact=False, unsafe_contact=False,
                 in_completion_region=False, sneak=False, terminal=False,
                 full_candidates=0, physics_steps=0, route_responsibility="active"),
            dict(tick=2, moving=False, progress=1.1, progress_delta=.1,
                 cross_track=.1, contact=False, unsafe_contact=False,
                 in_completion_region=True, sneak=False, terminal=True,
                 full_candidates=0, physics_steps=0, route_responsibility="settling")]


class F2GroundRouteGateTests(unittest.TestCase):
    def test_actual_dangerous_body_contact_is_extracted_and_detected(self):
        copy = actual_error_copy("unsafe_contact")
        self.assertIn("unsafe_contact", gate_violations([copy["measured"]], corridor=.45))

    def test_actual_cross_track_escape_is_extracted_and_detected(self):
        copy = actual_error_copy("outside_corridor")
        self.assertGreater(copy["measured"]["cross_track"], .45)
        self.assertIn("outside_corridor", gate_violations([copy["measured"]], corridor=.45))

    def test_neutral_inertial_stop_tail_escape_is_extracted_and_detected(self):
        copy = actual_error_copy("neutral_tail_outside_corridor")
        self.assertEqual(copy["applied"]["forward"], 0)
        self.assertAlmostEqual(copy["before"]["position"][0] - .7, .38)
        self.assertGreater(copy["after"]["position"][0] - .7, .45)
        self.assertIn("outside_corridor", gate_violations([copy["measured"]], corridor=.45))

    def test_neutral_input_does_not_disable_active_corridor_check(self):
        frames = safe_frames()
        frames[1].update(cross_track=.6, route_responsibility="settling")
        self.assertIn("outside_corridor", gate_violations(frames, corridor=.45))

    def test_initial_outside_corridor_is_rejected_without_execution_escape(self):
        manifest = json.loads(MANIFEST.read_text("utf-8"))
        case = next(c for c in manifest["tasks"] if c["id"] == "f2/outside_corridor/south/normal")
        result = run_route(case)
        self.assertFalse(result["success"])
        self.assertNotIn("outside_corridor", result["gate_violations"])

    def test_observed_sneaking_is_detected_after_neutral_request(self):
        from mc2p.contracts.action_v1 import MovementV1
        from mc2p.motion_nav.physics_types import PhysicsState
        from tests.sim.product_cases import f2_ground_route_scene
        copy = actual_error_copy("terminal_sneak")
        manifest = json.loads(MANIFEST.read_text("utf-8"))
        case = next(c for c in manifest["tasks"] if c["id"] == copy["case_id"])
        row = measure_frame(PhysicsState.from_mapping(copy["before"]), PhysicsState.from_mapping(copy["after"]),
                            MovementV1(), case, f2_ground_route_scene(case), tick=1,
                            responsibility="settling", terminal=True)
        self.assertIn("terminal_sneak", gate_violations([row], corridor=.45))

    def test_archived_raw_state_copies_are_remeasured(self):
        from mc2p.contracts.action_v1 import MovementV1
        from mc2p.motion_nav.physics_types import PhysicsState
        from scripts.f2_ground_route_evidence import ROOT
        from tests.sim.product_cases import f2_ground_route_scene
        archive = json.loads((ROOT / "evidence/motion_navigation/F2-ground-route-v1/baseline/measurement-v2/error-copies.json").read_text("utf-8"))
        manifest = json.loads(MANIFEST.read_text("utf-8"))
        cases = {c["id"]: c for c in manifest["tasks"]}
        for copy in archive["cases"]:
            with self.subTest(copy=copy["id"]):
                case = cases[copy["case_id"]]
                row = measure_frame(PhysicsState.from_mapping(copy["before"]), PhysicsState.from_mapping(copy["after"]),
                                    MovementV1(**copy["applied"]), case, f2_ground_route_scene(case), tick=1,
                                    responsibility=copy["responsibility"], terminal=copy["terminal"], **copy["diagnostics"])
                self.assertEqual(row, copy["measured"])
                self.assertIn(copy["expected_violation"], gate_violations([row], corridor=archive["corridor"]))

    def test_safe_wall_tangent_and_declared_sneak_are_allowed(self):
        frames = safe_frames()
        frames[0].update(contact=True, sneak=True)
        self.assertEqual(gate_violations(frames, corridor=.45, intervals=((.5, 1.05),)), [])

    def test_six_error_copies_are_detected(self):
        for expected in ("head_on_mid_route_wall", "unsafe_contact", "outside_corridor",
                         "undeclared_sneak", "terminal_sneak", "full_candidate_limit"):
            with self.subTest(expected=expected):
                copy = actual_error_copy(expected)
                self.assertIn(expected, gate_violations([copy["measured"]], corridor=.45))

    def test_sneak_outside_declared_interval_is_detected(self):
        frames = safe_frames()
        frames[0]["sneak"] = True
        self.assertIn("undeclared_sneak", gate_violations(frames, corridor=.45, intervals=((2., 3.),)))

    def test_terminal_wall_contact_requires_completion_region(self):
        frames = safe_frames()
        frames[0].update(contact=True, progress_delta=0, in_completion_region=True)
        self.assertNotIn("head_on_mid_route_wall", gate_violations(frames, corridor=.45))

    def test_missing_future_diagnostics_are_red_instead_of_zero(self):
        frames = safe_frames()
        frames[0].update(full_candidates=None, physics_steps=None)
        self.assertIn("RED:Task2:full_candidates", gate_violations(frames, corridor=.45))
        self.assertIn("RED:Task2:physics_steps", gate_violations(frames, corridor=.45))

    def test_frozen_inputs_keep_player_ids_seed_187_and_all_route_conditions(self):
        manifest = json.loads(MANIFEST.read_text("utf-8"))
        self.assertEqual(digest(manifest), "b37a3f1ec43993cd14850c6525dd1b76549b21c078a1496ad4d0d7ed8e3bfbcf",
                         "Later tasks must not rewrite the F2-0 frozen input manifest")
        tasks = manifest["tasks"]
        self.assertEqual(len({c["id"] for c in tasks}), len(tasks))
        self.assertEqual(len([c for c in tasks if c["family"] == "player"]), 400)
        groups = manifest["product_reference"]["groups"]
        for group in groups:
            self.assertIn(f"{group['id']}-000187", {c["id"] for c in tasks})
        for family in ("offset", "diagonal", "centre_to_offset", "tangent", "corner", "small_height", "declared_edge"):
            cases = [c for c in tasks if c["family"] == family]
            self.assertEqual(len(cases), 8)
            self.assertEqual({c["direction"] for c in cases}, {"south", "east", "north", "west"})
            self.assertEqual({tuple(c["late_ticks"]) for c in cases}, {(), (1,)})

    def test_offset_component_records_real_progress_without_inventing_diagnostics(self):
        manifest = json.loads(MANIFEST.read_text("utf-8"))
        case = next(c for c in manifest["tasks"] if c["id"] == "f2/offset/south/normal")
        result = run_route(case)
        self.assertTrue(result["success"])
        self.assertGreater(result["progress_blocks"], 5.7)
        self.assertEqual(result["input_sha256"], digest(case))
        self.assertEqual(result["full_candidates_max"], 0)
        self.assertEqual(result["physics_steps_total"], 0)
        self.assertEqual(result["diagnostics_status"]["full_candidates"], "production")
        self.assertEqual(result["diagnostics_status"]["physics_steps"], "production")
        self.assertNotIn("outside_corridor", result["gate_violations"])

    def test_frozen_formal_goal_boxes_equal_existing_goal_contract(self):
        from tests.sim.product_cases import product_scenario
        from tests.sim.runner import _goal
        manifest = json.loads(MANIFEST.read_text("utf-8"))
        for case in manifest["tasks"]:
            if case["kind"] != "formal_chain":
                continue
            group = next(g for g in manifest["product_reference"]["groups"] if g["id"] == case["group"])
            scenario, parameters = product_scenario(manifest["product_reference"], group, case["seed"])
            self.assertEqual(list(_goal(scenario.goal).region.as_tuple()), case["goal_box"])
            self.assertEqual(digest(parameters), case["parameters_sha256"])


if __name__ == "__main__":
    unittest.main()
