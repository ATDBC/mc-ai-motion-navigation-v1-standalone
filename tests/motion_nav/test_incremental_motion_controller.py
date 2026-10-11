import unittest
import json
from pathlib import Path

from experiments.motion_navigation.trajectory_proto import m0_probe


class IncrementalMotionControllerTests(unittest.TestCase):
    def test_frozen_mixed_candidate_is_reproduced(self):
        from experiments.motion_navigation.trajectory_proto.template_controller import (
            ControllerParams,
            ControllerTree,
            TemplateGeometry,
            evaluate,
        )

        fixture = m0_probe.build_mixed_action_fixture()
        geometry = TemplateGeometry(1.0, (0.5, 1.0, 3.341460582123732))
        params = ControllerParams("W", 0.15, "SJ", "W", 5)
        node, reason = evaluate(ControllerTree(fixture.fixture.request, fixture.fixture.world, geometry), params)
        self.assertEqual(reason, "ok")
        self.assertEqual(node.inputs, fixture.expected_inputs)

    def test_one_request_has_one_budget_owner(self):
        from experiments.motion_navigation.trajectory_proto.contracts import SearchBudget
        from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
        from experiments.motion_navigation.trajectory_proto.template_controller import (
            ControllerTree,
            TemplateGeometry,
        )

        fixture = m0_probe.build_mixed_action_fixture().fixture
        wrong = CountedPhysics(SearchBudget(8, 80, 40, 2))
        with self.assertRaisesRegex(ValueError, "request budget"):
            ControllerTree(fixture.request, fixture.world, TemplateGeometry(1.0, (0.5, 1.0, 3.34)), wrong)

        from experiments.motion_navigation.trajectory_proto.frozen_templates import verify_candidate
        with self.assertRaisesRegex(ValueError, "request budget"):
            verify_candidate(fixture.request, fixture.world,
                             m0_probe.build_mixed_action_fixture().expected_inputs, wrong)

    def test_frozen_third_party_controller_samples_match_exactly(self):
        from experiments.motion_navigation.trajectory_proto.frozen_templates import EntrySpec, make_request
        from experiments.motion_navigation.trajectory_proto.template_controller import (
            ControllerParams,
            ControllerTree,
            TemplateGeometry,
            evaluate,
        )

        path = (Path(__file__).resolve().parents[2]
                / "experiments/motion_navigation/trajectory_proto/data/d097a-accepted-sample.json")
        rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
        for row in rows:
            with self.subTest(template=row["template_id"], entry_bin=row["bin"]):
                spec = EntrySpec(row["template_id"], row["gait"], row["ticks"],
                                 row["offset"], row["heading_deg"])
                request, world = make_request(spec)
                node, reason = evaluate(
                    ControllerTree(request, world,
                                   TemplateGeometry.for_template(row["template_id"])),
                    ControllerParams.from_dict(row["params"]),
                )
                expected = tuple(type(request.stop_input)(*values) for values in row["inputs"])
                self.assertEqual(reason, "ok")
                self.assertEqual(node.inputs, expected)

    def test_module_does_not_import_review_code(self):
        from pathlib import Path
        from experiments.motion_navigation.trajectory_proto import template_controller

        source = Path(template_controller.__file__).read_text(encoding="utf-8")
        self.assertNotIn("reviews", source)
        self.assertNotIn(".tmp", source)


if __name__ == "__main__":
    unittest.main()
