import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class MotionNavigationPackageBoundaryTests(unittest.TestCase):
    def test_runtime_owned_drivers_do_not_advance_or_submit_outside_control_frame(self):
        required_paths = (
            "mc2p/skills/melee_strike_driver.py",
            "mc2p/skills/moving_melee_driver.py",
            "mc2p/skills/external_motion_recovery_driver.py",
        )
        optional_history_paths = ("mc2p/skills/point_goal_driver.py",)
        for relative in required_paths:
            self.assertTrue((ROOT / relative).is_file(), relative)
        forbidden = []
        for relative in required_paths + optional_history_paths:
            source = ROOT / relative
            # 主仓库还保留旧 point-goal 代码供历史参照；独立包只导出
            # 当前 Runtime 主线实际使用的驱动。共享测试只检查当前树中
            # 存在的文件，不能为了凑齐历史文件而重新带入旧射线依赖。
            if not source.is_file():
                continue
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                owner = node.func.value
                runtime_owned = (
                    isinstance(owner, ast.Attribute) and owner.attr == "runtime"
                )
                if runtime_owned and node.func.attr in {"step", "submit_ordered_intent"}:
                    forbidden.append((relative, node.lineno, node.func.attr))
        self.assertEqual(forbidden, [])

    def test_evidence_tools_have_one_canonical_implementation(self):
        from mc2p.motion_nav.evidence.ground_calibration import (
            calibrate_ground_motion as canonical_calibrate,
        )
        from mc2p.motion_nav.evidence.physics_validation import (
            compare_tick_rows as canonical_compare,
        )
        from mc2p.motion_nav.evidence.reference_baselines import (
            load_reference_catalog as canonical_load,
        )
        from mc2p.motion_nav.evidence.reference_evidence import (
            normalize_legacy_evidence as canonical_normalize,
        )
        from mc2p.motion_nav.evidence.shape_trials import (
            circle_route_points as canonical_circle,
        )
        from mc2p.motion_nav.ground_calibration import calibrate_ground_motion
        from mc2p.motion_nav.physics_validation import compare_tick_rows
        from mc2p.motion_nav.reference_baselines import load_reference_catalog
        from mc2p.motion_nav.reference_evidence import normalize_legacy_evidence
        from mc2p.motion_nav.shape_trials import circle_route_points

        self.assertIs(calibrate_ground_motion, canonical_calibrate)
        self.assertIs(compare_tick_rows, canonical_compare)
        self.assertIs(load_reference_catalog, canonical_load)
        self.assertIs(normalize_legacy_evidence, canonical_normalize)
        self.assertIs(circle_route_points, canonical_circle)
        self.assertEqual(
            canonical_calibrate.__module__,
            "mc2p.motion_nav.evidence.ground_calibration",
        )

    def test_replaced_air_coordinator_is_explicitly_legacy(self):
        from mc2p.motion_nav.air_confirmation import AirConfirmationService
        from mc2p.motion_nav.legacy.air_confirmation import (
            AirConfirmationService as LegacyAirConfirmationService,
        )

        self.assertIs(AirConfirmationService, LegacyAirConfirmationService)
        self.assertEqual(
            LegacyAirConfirmationService.__module__,
            "mc2p.motion_nav.legacy.air_confirmation",
        )

    def test_materialized_planners_have_a_reference_only_entry(self):
        from mc2p.motion_nav import known_map_planner
        from mc2p.motion_nav import planning_reference

        self.assertIs(planning_reference.build_walk_graph,
                      known_map_planner.build_walk_graph)
        self.assertIs(planning_reference.astar_plan,
                      known_map_planner.astar_plan)
        self.assertIs(planning_reference.build_surface_graph,
                      known_map_planner.build_surface_graph)
        self.assertIs(planning_reference.dijkstra_surface_reference,
                      known_map_planner.dijkstra_surface_reference)
        self.assertEqual(planning_reference.LIFECYCLE, "reference_only")


if __name__ == "__main__":
    unittest.main()
