"""Bounded permission for using one engaged target outside direct vision."""
from dataclasses import replace
import unittest

from mc2p.contracts.common import FieldStatusV0
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v2 import ObservationGroupV2, VisibleEntityV2
from mc2p.contracts.observation_v3 import TrackedEntityStateV3
from mc2p.skills.engagement_memory import (
    EngagementEventKind, EngagementStateV1, TargetPositionSource,
    advance_engagement, event_from_observation, resolve_target_position,
)
from mc2p.skills.fixed_melee import CombatTargetV1
from tests.observation_v3_fixtures import valid_snapshot_v3


NOW = 200_000_000


def target(revision=1, *, episode="combat-ep", goal="zombie-goal"):
    return CombatTargetV1("combat-task", goal, revision, episode, "entity-world-7")


def observation(sequence, *, visible, tracked=True, dead=False,
                received_ns=NOW, client_completed_ns=None):
    obs = valid_snapshot_v3(sequence=sequence,
        request_start_ns=received_ns - 1_000_000, received_at_ns=received_ns)
    obs = replace(obs, episode_id="combat-ep")
    if client_completed_ns is not None:
        obs = replace(obs, client_sample=replace(
            obs.client_sample, completed_at_monotonic_ns=client_completed_ns,
            started_at_monotonic_ns=client_completed_ns - 10,
        ))
    shown = VisibleEntityV2(
        "entity-world-7", "minecraft:zombie", None,
        Vec3V0(2, 0, 1), Vec3V0(.1, 0, 0), 15, 0,
        Vec3V0(.6, 1.95, .6), "standing", True, (), 0,
    )
    perception_value = replace(
        obs.perception.value, visible_entities=(shown,) if visible else (),
    )
    tick = obs.world_time_ticks.value
    entity = TrackedEntityStateV3(
        "entity-world-7", "minecraft:zombie",
        Vec3V0(2.1, 0, 1), Vec3V0(.1, 0, 0), 15, 0,
        Vec3V0(.6, 1.95, .6), "standing", True, True, dead,
        0 if dead else 18, 20,
    )
    tracked_group = (ObservationGroupV2.valid(tick, "client_registered_entity", entity)
                     if tracked else ObservationGroupV2.missing(
                         tick, "client_registered_entity", "entity_unavailable"))
    return replace(obs,
        perception=ObservationGroupV2.valid(
            tick, "client_perception_filtered", perception_value),
        tracked_entity=tracked_group,
    )


class EngagementMemoryTests(unittest.TestCase):
    def test_hidden_query_requires_confirmed_retaliation_grant(self):
        identity = target()
        visible = observation(3, visible=True)
        seen = advance_engagement(
            EngagementStateV1.for_target(identity),
            event_from_observation(identity, visible),
        )
        self.assertEqual(
            resolve_target_position(seen, visible, identity, NOW).source,
            TargetPositionSource.VISION,
        )
        hidden = observation(4, visible=False, received_ns=NOW + 10_000_000)
        hidden_state = advance_engagement(
            seen, event_from_observation(identity, hidden),
        )
        self.assertIsNone(resolve_target_position(
            hidden_state, hidden, identity, NOW + 10_000_000,
        ))
        engaged = advance_engagement(
            hidden_state,
            event_from_observation(
                identity, hidden, kind=EngagementEventKind.CONFIRMED_HIT,
            ),
        )
        fact = resolve_target_position(
            engaged, hidden, identity, NOW + 10_000_000,
        )
        self.assertEqual(fact.source, TargetPositionSource.ENGAGEMENT)
        self.assertEqual((fact.health_points, fact.is_dead), (18, False))

    def test_query_never_grants_permission_and_current_vision_wins(self):
        identity = target()
        visible = observation(1, visible=True)
        state = advance_engagement(
            EngagementStateV1.for_target(identity),
            event_from_observation(identity, visible),
        )
        fact = resolve_target_position(state, visible, identity, NOW)
        self.assertEqual(fact.source, TargetPositionSource.VISION)
        self.assertEqual(fact.relative_position, Vec3V0(2, 0, 1))
        self.assertFalse(state.engagement_granted)

    def test_identity_cancel_unload_and_death_have_stable_revocations(self):
        identity = target()
        first = observation(1, visible=True)
        base = advance_engagement(
            EngagementStateV1.for_target(identity),
            event_from_observation(identity, first),
        )
        cases = (
            (event_from_observation(target(revision=2), observation(2, visible=True)),
             "target_identity_changed"),
            (event_from_observation(identity, observation(2, visible=False, tracked=False)),
             "target_unavailable"),
            (event_from_observation(identity, observation(2, visible=False, dead=True)),
             "target_dead"),
            (replace(event_from_observation(identity, observation(2, visible=False)),
                     kind=EngagementEventKind.CANCELLED), "task_cancelled"),
        )
        for event, reason in cases:
            with self.subTest(reason=reason):
                revoked = advance_engagement(base, event)
                self.assertFalse(revoked.active)
                self.assertEqual(revoked.revocation_reason, reason)

    def test_regression_gap_and_expiry_never_drive_navigation(self):
        identity = target()
        first_obs = observation(5, visible=True, client_completed_ns=5000)
        first = advance_engagement(
            EngagementStateV1.for_target(identity),
            event_from_observation(identity, first_obs),
        )
        for next_obs, reason in (
            (observation(4, visible=False, client_completed_ns=6000), "observation_sequence_regressed"),
            (observation(7, visible=False, client_completed_ns=7000), "observation_gap"),
            (observation(6, visible=False, client_completed_ns=4000), "client_sample_time_regressed"),
        ):
            with self.subTest(reason=reason):
                state = advance_engagement(first, event_from_observation(identity, next_obs))
                self.assertEqual(state.revocation_reason, reason)
        changed_world = replace(
            event_from_observation(identity, observation(6, visible=False, client_completed_ns=6000)),
            observation_episode_id="other-episode",
        )
        self.assertEqual(
            advance_engagement(first, changed_world).revocation_reason,
            "world_session_changed",
        )
        receive_regression = replace(
            event_from_observation(identity, observation(6, visible=False, client_completed_ns=6000)),
            received_at_monotonic_ns=first.last_received_at_monotonic_ns - 1,
        )
        self.assertEqual(
            advance_engagement(first, receive_regression).revocation_reason,
            "controller_receive_time_regressed",
        )
        second_obs = observation(6, visible=False, client_completed_ns=6000)
        engaged = advance_engagement(first, event_from_observation(
            identity, second_obs, kind=EngagementEventKind.CONFIRMED_HIT,
        ))
        self.assertIsNone(resolve_target_position(
            engaged, second_obs, identity, second_obs.received_at_monotonic_ns + 500_000_001,
        ))
        self.assertIsNone(resolve_target_position(
            engaged, second_obs, target(goal="other-goal"),
            second_obs.received_at_monotonic_ns,
        ))


if __name__ == "__main__":
    unittest.main()
