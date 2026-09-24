"""Frozen knowledge identity and strict admission for current navigation evidence."""
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from scripts.navigation_motion_evidence import restore_snapshot

NAVIGATION_CONTRACT = (
    ('observation_schema_version','mc2p.observation.v3'),
    ('knowledge_model','block_state_v1'),
    ('field_profile','navigation_v1'),
)


def require_navigation_contract(value):
    if type(value) is not dict or any(type(value.get(key)) is not str or value[key]!=expected
                                     for key,expected in NAVIGATION_CONTRACT):
        raise ValueError('navigation observation/knowledge/profile contract mismatch')


def require_navigation_snapshot_v3(raw):
    if type(raw) is not dict or raw.get('schema_version')!='mc2p.observation.v3':
        raise ValueError('current navigation requires an actual V3 observation')
    obs=restore_snapshot(raw)
    if (type(obs) is not ObservationSnapshotV3 or obs.privileged_fields_present
            or obs.field_profile!='navigation_v1' or obs.targeting.reason_code!='not_requested'):
        raise ValueError('current navigation requires unprivileged navigation_v1 evidence')
    return obs
