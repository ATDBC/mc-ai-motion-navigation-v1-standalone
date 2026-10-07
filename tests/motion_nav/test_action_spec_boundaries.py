"""Low-level action implementations cannot depend on their callers."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ActionSpecBoundaryTests(unittest.TestCase):
    def test_controlled_drop_has_no_upper_or_private_imports(self):
        tree = ast.parse((ROOT / 'mc2p/motion_nav/actions/controlled_drop.py').read_text())
        forbidden = {'route_admission', 'action_preconditions', 'navigation_session',
                     'navigation_owners', 'planning_coordinator'}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or '').split('.')[-1], forbidden)
                self.assertFalse(any(n.name.startswith('_') for n in node.names))

    def test_low_level_contracts_keep_public_facade_identity_and_id(self):
        from mc2p.motion_nav import action_preconditions as facade
        from mc2p.motion_nav import action_requirements as low
        for name in ('AcquisitionSpec', 'AcquisitionGrant', 'ActionPreconditionResult',
                     'ActionPreconditionReason', 'ActionPreconditionStatus'):
            self.assertIs(getattr(facade, name), getattr(low, name))
        route = SimpleNamespace(route_id='r', route_revision=2)
        self.assertEqual(low.acquisition_id(route, 3, (4, 5, 6)),
                         'landing-7c9ad6972d6be758692f')
        self.assertEqual(low.ActionPreconditionResult(low.ActionPreconditionStatus.READY,
            low.ActionPreconditionReason.READY).missing_cells, ())

    def test_landing_evidence_is_owned_by_low_level_module(self):
        from mc2p.motion_nav import landing_evidence as low
        from mc2p.motion_nav import route_admission as facade
        self.assertIs(low.direct_drop_visual_evidence_sufficient,
                      facade.direct_drop_visual_evidence_sufficient)
        self.assertEqual(low.direct_drop_visual_evidence_sufficient.__module__,
                         'mc2p.motion_nav.landing_evidence')
