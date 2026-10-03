"""R28-1 public Runtime boundaries before moving stop destinations."""
from dataclasses import replace
import unittest

from tests.motion_nav.test_action_continuity_formal import _gap_case
from tests.sim.backend import Perturbations, SLAB_ID
from tests.sim.runner import Event, _goal, run
from tests.sim.scenarios import SCENARIOS


class SharedRecoveryFormalTests(unittest.TestCase):
    def test_whole_floor_lowering_releases_without_claiming_old_goal_reached(self):
        base = next(s for s in SCENARIOS if s.name == "flat_walk")
        def lower(context):
            edits = {position: SLAB_ID for position in context.backend.scene.solids
                     if position[1] == 63}
            context.backend.perturbations.world_edits[context.backend.movement_tick + 1] = edits
        result = run(replace(base, name="r28-whole-floor-lowered", max_ticks=180,
            perturbations=Perturbations(),
            events=[Event("lower-entire-floor", lambda c: c.tick >= 10, lower)]))
        self.assertFalse(result.violations, result.violations)
        self.assertEqual(result.outcome, "failed", result.reason)
        self.assertLess(result.ticks, 100)
        self.assertTrue(any(row["position"][1] < 63.8 for row in result.trace))
        self.assertFalse(result.trace[-1]["source_bound"])
        self.assertAlmostEqual(result.trace[-1]["position"][1], 63.5)

    def test_expired_planning_work_is_retired_at_every_observed_boundary(self):
        base = next(s for s in SCENARIOS if s.name == "flat_walk")
        edits = {tick: {(0, 63, 5): "minecraft:stone" if tick % 2 else "minecraft:grass_block"}
                 for tick in range(3, 180)}
        result = run(replace(base, name="r28-every-tick-material-change", max_ticks=180,
                             perturbations=Perturbations(world_edits=edits)))
        self.assertFalse(result.violations, result.violations)
        self.assertEqual(result.outcome, "failed", result.reason)
        self.assertFalse(result.trace[-1]["source_bound"])
        self.assertTrue(all(not row["planning_work_owned"] or row["planning_work_identity_valid"]
                            for row in result.trace))

    def test_airborne_cancel_cannot_be_replaced_by_a_new_goal_revision(self):
        refused = []
        def cancel_and_revise(context):
            context.session.cancel("r28-cancel-in-air")
            refused.append(not context.driver.replace_goal(
                "goal", 2, _goal((.55, 64., 7.5)), context.clock[0],
            ))
        case = replace(_gap_case(), max_ticks=160,
                       events=[Event("cancel-in-air", lambda c: not c.backend.state.on_ground,
                                     cancel_and_revise)])
        result = run(case)
        self.assertEqual(refused, [True])
        self.assertEqual(result.outcome, "cancelled", result.reason)
        self.assertFalse(result.violations, result.violations)
        self.assertTrue(result.trace[-1]["on_ground"])
        self.assertFalse(result.trace[-1]["source_bound"])

    def test_airborne_revision_preserves_body_until_landing(self):
        revised = []
        def revise(context):
            context.goal_position = (.55, 64., 7.5)
            context.goal_state = _goal(context.goal_position, context.risk_policy_id)
            context.driver.replace_goal("goal", 2, context.goal_state, context.clock[0])
            revised.append(context.backend.movement_tick)
        result = run(replace(_gap_case(), max_ticks=160,
            events=[Event("revise-in-air", lambda c: not c.backend.state.on_ground, revise)]))
        self.assertTrue(revised)
        self.assertIn(result.outcome, {"success", "failed"}, result.reason)
        self.assertFalse(result.violations, result.violations)
        self.assertEqual(result.damage, 0.)
        self.assertFalse(result.trace[-1]["source_bound"])


if __name__ == "__main__":
    unittest.main()
