"""Recovery fixtures keep the frozen v9 target and world inputs intact."""
from dataclasses import replace
import unittest

from scripts.f2_ground_route_runtime import (
    frozen_plan, fixture, _original_goal, _survey_exterior, _fixture_view_has_support,
)
from scripts.f2_ground_route_evidence import digest
from tests.sim.f2r_cases import goal_for
from tests.sim.f2s_cases import support_region_cases
from mc2p.motion_nav.world_model import Aabb


class RecoveryFabricFixtureTests(unittest.TestCase):
    def test_bridge_camera_preflight_rejects_old_unsupported_views_and_accepts_new(self):
        for row in frozen_plan(f2rec=True):
            if row['family']!='f2rec_bridge_head':
                continue
            solids,_,_ = fixture(row)
            self.assertFalse(_fixture_view_has_support(solids,row,-2.5,.5))
            self.assertFalse(_fixture_view_has_support(solids,row,3.5,9.5))
            for x,z,_,_ in _survey_exterior(row):
                self.assertTrue(_fixture_view_has_support(solids,row,x,z))
            for x,z in ((.5,.5),(.5,9.5),(.5,8.5)):
                self.assertTrue(_fixture_view_has_support(solids,row,x,z))

    def test_support_matrix_preserves_all_24_frozen_world_and_goal_inputs(self):
        rows = frozen_plan(f2rec=True)
        self.assertEqual(len(rows), 24)
        self.assertEqual(len({row['id'] for row in rows}), 24)
        for row in rows:
            with self.subTest(case=row['id']):
                case = next(case for case in support_region_cases() if case['id']==row['frozen_v9_id'])
                solids,start,target = fixture(row)
                self.assertNotEqual(case['family'], 'column_top')
                self.assertEqual(row['frozen_v9_input_sha256'], digest(case))
                self.assertEqual(solids, {(x,y+36,z):material for x,y,z,material in case['solids']})
                self.assertEqual(start, (case['start'][0],case['start'][1]+36,case['start'][2]))
                before = goal_for(tuple(case['goal']), case['target'])
                bounds = before.region.as_tuple()
                translated = Aabb(bounds[0],bounds[1]+36,bounds[2],bounds[3],bounds[4]+36,bounds[5])
                self.assertEqual(_original_goal(row,target), replace(before, region=translated))
                self.assertEqual(row['goal_bounds'], list(translated.as_tuple()))

    def test_each_family_covers_all_directions_and_actual_late_first_requirement(self):
        rows = frozen_plan(f2rec=True)
        for family,target in {(row['family'],row['target_kind']) for row in rows}:
            self.assertEqual({(row['direction'],row['condition']) for row in rows
                if (row['family'],row['target_kind']) == (family,target)},
                {(direction,condition) for direction in range(4)
                 for condition in ('normal','late_first')})

    def test_full_recovery_plan_has_43_groups_and_46_actual_trials(self):
        from scripts.f2rec_r4_fabric import plan
        frozen = plan()
        self.assertEqual(frozen['groups'],43)
        self.assertEqual(frozen['actual_trials'],46)
        self.assertEqual(len(frozen['ordinary_actor_reference']),6)
        for family in ('offset_mid','player_wall_head','player_ledge_2'):
            actor = next(row for row in frozen['ordinary_actor_reference']
                         if row['id']=='f2-'+family+'-0-normal')
            reference = next(row for row in frozen['ordinary_actor_reference']
                             if row['id']=='f2-reference-'+family)
            for key in ('scene_sha256','start_position','goal_bounds'):
                self.assertEqual(actor[key],reference[key])


if __name__=='__main__':
    unittest.main()
