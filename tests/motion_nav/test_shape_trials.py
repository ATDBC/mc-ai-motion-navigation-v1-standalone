from __future__ import annotations

import math
import unittest

from mc2p.motion_nav.evidence.shape_trials import (
    circle_metrics,
    circle_route_points,
    line_metrics,
    line_reference_yaw_degrees,
)


class ShapeTrialTests(unittest.TestCase):
    def test_line_reference_turns_once_each_way_over_equal_distances(self) -> None:
        self.assertEqual(line_reference_yaw_degrees(0), 0)
        self.assertEqual(line_reference_yaw_degrees(3), 45)
        self.assertEqual(line_reference_yaw_degrees(24), 360)
        self.assertEqual(line_reference_yaw_degrees(27), 315)
        self.assertEqual(line_reference_yaw_degrees(48), 0)
        self.assertEqual(line_reference_yaw_degrees(51), 0)

    def test_left_and_right_circle_routes_are_closed_and_tangent_to_positive_z(self) -> None:
        origin = (10.5, 1.0, -3.5)
        left = circle_route_points(origin, 4.0, "left")
        right = circle_route_points(origin, 4.0, "right")
        for route in (left, right):
            self.assertEqual(route[0], route[-1])
            self.assertLess(math.dist(route[0], route[1]), .35)
            self.assertGreater(route[1][2], route[0][2])
        self.assertLess(min(point[0] for point in left), origin[0] - 7.9)
        self.assertGreater(max(point[0] for point in right), origin[0] + 7.9)

    def test_continuous_metrics_do_not_treat_polygon_vertices_as_the_circle(self) -> None:
        origin = (0.0, 1.0, 0.0)
        radius = 4.0
        samples = [
            {"position": [radius * math.cos(angle) - radius, 1.0,
                          radius * math.sin(angle)], "t": index * .05}
            for index, angle in enumerate([i * math.tau / 80 for i in range(81)])
        ]
        metrics = circle_metrics(samples, origin, radius, "left")
        self.assertLess(metrics["radial_rmse_blocks"], 1e-9)
        self.assertLess(metrics["closure_error_blocks"], 1e-9)
        samples[20]["position"][2] += .5
        changed = circle_metrics(samples, origin, radius, "left")
        self.assertGreater(changed["radial_max_blocks"], .45)

    def test_line_metrics_report_lateral_drift_and_view_error_separately(self) -> None:
        samples = [
            {"position": [0.1 * i, 1.0, float(i)], "yaw": line_reference_yaw_degrees(i) + 2,
             "t": i * .05}
            for i in range(10)
        ]
        metrics = line_metrics(samples, (0.0, 1.0, 0.0))
        self.assertAlmostEqual(metrics["final_lateral_error_blocks"], .9)
        self.assertAlmostEqual(metrics["yaw_error_rms_degrees"], 2.0)


if __name__ == "__main__":
    unittest.main()
