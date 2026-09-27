import unittest
from tests.observation_v3_fixtures import valid_payload_value,block_value,encoded
from mc2p.backends.client_observation_payload_v3 import decode_client_observation_payload_v3
class SurfaceObservationTests(unittest.TestCase):
 def value(self):
  v=valid_payload_value();p=v['perception']['value'];p.update(sensor_profile_revision=4,ray_columns=0,ray_rows=0);p['blocks']=[block_value(sources=('surface_depth',))];return v
 def test_surface_profile_decodes(self):
  p=decode_client_observation_payload_v3(encoded(self.value())).perception.value
  self.assertEqual(p.sensor_profile_revision,4);self.assertEqual(p.blocks[0].sources,('surface_depth',))
  self.assertEqual(p.visibility_rules_id,'surface_visibility_1_21_v1')
  self.assertEqual(p.entity_visibility_near_model,'surface_bbox_exact_16')
  self.assertEqual(p.entity_visibility_far_model,'surface_rules_five_point_16_32')
 def test_surface_profile_requires_the_frozen_visibility_rules(self):
  for mutation in ('missing','wrong','missing_near','missing_far'):
   v=self.value();p=v['perception']['value']
   if mutation=='missing':p.pop('visibility_rules_id')
   elif mutation=='missing_near':p.pop('entity_visibility_near_model')
   elif mutation=='missing_far':p.pop('entity_visibility_far_model')
   else:p['visibility_rules_id']='surface_visibility_future'
   with self.subTest(mutation=mutation),self.assertRaises(ValueError):
    decode_client_observation_payload_v3(encoded(v))
 def test_profiles_cannot_mislabel_sources(self):
  for revision,cols,rows,source in [(3,159,9,'surface_depth'),(4,0,0,'first_hit_ray'),(4,159,9,'surface_depth'),(5,0,0,'surface_depth')]:
   v=self.value();v['perception']['value'].update(sensor_profile_revision=revision,ray_columns=cols,ray_rows=rows,blocks=[block_value(sources=(source,))])
   with self.assertRaises(ValueError):decode_client_observation_payload_v3(encoded(v))
 def test_surface_keeps_contact_and_large_visible_set(self):
  v=self.value();p=v['perception']['value'];p['blocks']=[block_value(position=(i,63,0),sources=('surface_depth',)) for i in range(1500)]
  self.assertEqual(len(decode_client_observation_payload_v3(encoded(v)).perception.value.blocks),1500)
  p['blocks']=[block_value(sources=('body_contact','surface_depth'))];self.assertEqual(len(decode_client_observation_payload_v3(encoded(v)).perception.value.blocks),1)
if __name__=='__main__':unittest.main()
