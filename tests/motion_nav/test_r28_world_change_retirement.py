"""World changes must reach both the game model and the formal observer."""
from dataclasses import replace
import math
import unittest

from tests.sim.backend import Perturbations, SLAB_ID
from tests.sim.runner import Event, _goal, run
from tests.sim.scenarios import SCENARIOS


def lower_support(context):
    x, y, z = context.backend.state.position
    edits = {(xx, math.floor(y)-1, zz): SLAB_ID
             for xx in range(math.floor(x)-1, math.floor(x)+2)
             for zz in range(math.floor(z)-1, math.floor(z)+2)}
    context.backend.perturbations.world_edits[context.backend.movement_tick+1] = edits


class WorldChangeRetirementTests(unittest.TestCase):
    def test_sensor_only_world_edit_cannot_be_mistaken_for_a_real_fall(self):
        base = next(s for s in SCENARIOS if s.name == 'flat_walk')
        def corrupt(context):
            context.backend.scene.solids[(0,63,0)] = SLAB_ID
        with self.assertRaisesRegex(RuntimeError, 'collision world'):
            run(replace(base, events=[Event('bad-edit',lambda c:c.tick>=6,corrupt)]))

    def test_real_support_lowering_has_bounded_release(self):
        base = next(s for s in SCENARIOS if s.name == 'flat_walk')
        for at in (6,10,14):
            for mode in ('normal','late','cancel','revision'):
                with self.subTest(at=at,mode=mode):
                    events = [Event('lower-support',lambda c,at=at:c.tick>=at,lower_support)]
                    if mode == 'cancel':
                        events.append(Event('cancel',lambda c,at=at:c.tick>=at+2,
                                            lambda c:c.session.cancel('test_cancel')))
                    if mode == 'revision':
                        def revise(c):
                            c.goal_position = (.5,64.,6.5)
                            c.goal_state = _goal(c.goal_position,c.risk_policy_id)
                            c.driver.replace_goal('goal',2,c.goal_state,c.clock[0])
                        events.append(Event('revision',lambda c,at=at:c.tick>=at+2,revise))
                    delays = frozenset(range(1,180,5)) if mode == 'late' else frozenset()
                    result = run(replace(base, name=f'lower-{at}-{mode}',max_ticks=180,
                                         perturbations=Perturbations(late_ticks=delays),events=events))
                    self.assertIn(result.outcome, {'success','failed','cancelled'},result.reason)
                    self.assertFalse(result.violations,result.violations)
                    self.assertLess(result.ticks,100)
                    self.assertTrue(any(row['position'][1] < 63.8 for row in result.trace))
                    self.assertFalse(result.trace[-1]['source_bound'])


if __name__ == '__main__':
    unittest.main()
