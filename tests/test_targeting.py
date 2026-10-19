"""Current targeting is a task query, not block discovery or remembered aim."""
from dataclasses import replace
import unittest

from mc2p.contracts.common import ContractViolation, FieldStatusV0
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v3 import TargetingStateV3
from tests.observation_v3_fixtures import valid_snapshot_v3
from tests.follow_v3_fixtures import observed_block
from tests.follow_v3_fixtures import follow_snapshot
from tests.follow_fixtures import player_value


NOW = 101_000_000
BLOCK = (0, 63, 0)


def targeted_snapshot(**kwargs):
    block=observed_block(BLOCK)
    obs=valid_snapshot_v3(profile='interaction_v1',blocks=(block,),**kwargs)
    return replace(obs,perception=replace(obs.perception,value=replace(obs.perception.value,
        blocks=(replace(block,sources=('current_target',)),))),
        targeting=replace(obs.targeting,value=TargetingStateV3(
        'block',BLOCK,None,'up',Vec3V0(.5,64,.5),1.62)))


def targeted_entity_snapshot(*, track_id="entity-zombie-1", relative=(0, 0, 2.5),
                             hurt=0, cooldown=1.0, targeted=True, sequence=1,
                             received=100_000_000):
    entity = player_value(track_id, relative, entity_type="minecraft:zombie")
    entity["hurt_animation_ticks"] = hurt
    obs = follow_snapshot(
        profile="interaction_v1", entities=[entity], sequence=sequence,
        received=received, self_changes={"attack_cooldown": cooldown},
    )
    targeting = TargetingStateV3(
        "entity", None, track_id, None,
        Vec3V0(obs.self_state.value.position.x + relative[0],
               obs.self_state.value.position.y + 1.0,
               obs.self_state.value.position.z + relative[2]),
        float((relative[0] ** 2 + relative[2] ** 2) ** .5),
    ) if targeted else TargetingStateV3("miss", None, None, None, None, None)
    return replace(obs, targeting=replace(obs.targeting, value=targeting))


class TargetingTests(unittest.TestCase):
    def check(self,obs,**kwargs):
        from mc2p.skills.targeting import confirmed_block_target
        args=dict(block_position=BLOCK,face='up',now_ns=NOW,controller_clock_id='controller-test')
        args.update(kwargs)
        return confirmed_block_target(obs,**args)

    def test_only_current_interaction_block_and_optional_exact_face_confirm(self):
        obs=targeted_snapshot()
        self.assertTrue(self.check(obs))
        self.assertTrue(self.check(obs,face=None))
        self.assertFalse(self.check(obs,face='north'))
        self.assertFalse(self.check(obs,block_position=(1,63,0)))
        for other in (valid_snapshot_v3(),valid_snapshot_v3(profile='interaction_v1'),
                      replace(valid_snapshot_v3(profile='interaction_v1'),targeting=replace(obs.targeting,
                          status=FieldStatusV0.MISSING,value=None,reason_code='world_unavailable'))):
            self.assertFalse(self.check(other))

    def test_only_fresh_visible_exact_entity_target_confirms(self):
        from mc2p.skills.targeting import confirmed_entity_target
        obs = targeted_entity_snapshot()
        args = dict(entity_ref="entity-zombie-1", now_ns=NOW,
                    controller_clock_id="controller-test")
        self.assertTrue(confirmed_entity_target(obs, **args))
        self.assertFalse(confirmed_entity_target(obs, **(args | {"entity_ref": "entity-other"})))
        self.assertFalse(confirmed_entity_target(
            follow_snapshot(entities=[player_value("entity-zombie-1")]), **args))
        self.assertFalse(confirmed_entity_target(
            replace(obs, targeting=replace(obs.targeting, status=FieldStatusV0.MISSING,
                    value=None, reason_code="target_query_unavailable")), **args))
        self.assertFalse(confirmed_entity_target(
            replace(obs, privileged_fields_present=("world_map",)), **args))
        self.assertFalse(confirmed_entity_target(
            obs, **(args | {"controller_clock_id": "other"})))
        self.assertFalse(confirmed_entity_target(
            obs, **(args | {"now_ns": 600_000_001})))

    def test_clock_freshness_and_privilege_fail_closed_without_renewal(self):
        obs=targeted_snapshot()
        self.assertTrue(self.check(obs,now_ns=600_000_000))
        self.assertFalse(self.check(obs,now_ns=600_000_001))
        self.assertFalse(self.check(obs,now_ns=NOW-1))
        self.assertFalse(self.check(obs,controller_clock_id='other'))
        self.assertFalse(self.check(replace(obs,privileged_fields_present=('world_map',))))

    def test_entity_and_unavailable_targeting_never_confirm_a_block(self):
        from tests.follow_v3_fixtures import follow_snapshot
        obs=follow_snapshot(profile='interaction_v1')
        entity=replace(obs,targeting=replace(obs.targeting,value=TargetingStateV3(
            'entity',None,'player-1',None,Vec3V0(0,65,2),4.)))
        self.assertFalse(self.check(entity))
        for status in (FieldStatusV0.MISSING,FieldStatusV0.UNSUPPORTED):
            unavailable=replace(obs,targeting=replace(obs.targeting,status=status,
                value=None,reason_code='target_query_unavailable'))
            self.assertFalse(self.check(unavailable))

    def test_wrong_snapshot_and_malformed_query_are_rejected(self):
        from tests.observation_v2_fixtures import valid_snapshot_v2
        with self.assertRaises(ContractViolation): self.check(valid_snapshot_v2())
        for kwargs in ({'block_position':(True,63,0)},{'face':'top'},{'now_ns':True},
                       {'freshness_ns':-1},{'controller_clock_id':''}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ContractViolation):
                self.check(targeted_snapshot(),**kwargs)

    def test_navigation_replaces_targeting_without_putting_it_in_terrain_history(self):
        from mc2p.skills.navigation_memory import NavigationMemory
        memory=NavigationMemory('scope')
        first=memory.observe(targeted_snapshot(),now_ns=NOW,controller_clock_id='controller-test',scope_id='scope')
        self.assertEqual(first.latest.targeting.value.block_position,BLOCK)
        nav=valid_snapshot_v3(sequence=2,request_start_ns=200_000_000,received_at_ns=201_000_000)
        second=memory.observe(nav,now_ns=201_000_000,controller_clock_id='controller-test',scope_id='scope')
        self.assertEqual(second.latest.field_profile,'navigation_v1')
        self.assertEqual(second.latest.targeting.reason_code,'not_requested')
        self.assertIsNone(second.latest.targeting.value)
        self.assertFalse(hasattr(second.terrain[0],'targeting'))

    def test_unavailable_projection_retains_actual_request_profile_without_confirming(self):
        from tests.observation_v3_fixtures import valid_payload_value,encoded
        from mc2p.backends.client_observation_payload_v3 import decode_client_observation_payload_v3,snapshot_v3_from_payload
        from mc2p.skills.navigation_evidence import project_navigation_evidence
        value=valid_payload_value('interaction_v1')
        value['self_state'].update(status='missing',value=None,reason_code='player_unavailable')
        obs=snapshot_v3_from_payload(decode_client_observation_payload_v3(encoded(value)),
            episode_id='test',request_sequence_id=None,request_started_at_monotonic_ns=100_000_000,
            received_at_monotonic_ns=NOW,controller_clock_id='controller-test',source_backend='fixture')
        result=project_navigation_evidence(obs,now_ns=NOW,controller_clock_id='controller-test')
        self.assertFalse(result.available)
        self.assertEqual(result.field_profile,'interaction_v1')
        self.assertEqual(result.targeting,obs.targeting)
        self.assertFalse(self.check(obs))


if __name__=='__main__': unittest.main()
