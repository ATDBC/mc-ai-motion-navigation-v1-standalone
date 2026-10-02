import unittest
from dataclasses import replace
from unittest.mock import patch

from scripts.navigation_migration_evidence import observe
from tests.sim.migration_faults import FAULT_CASES


class MigrationFaultTests(unittest.TestCase):
    def test_external_impulse_delivery_is_independent_of_diagnostic_names(self):
        from mc2p.motion_nav.navigation_session import NavigationSession
        from tests.sim.backend import Perturbations
        from tests.sim.runner import run
        from tests.sim.scenarios import SCENARIOS
        base = next(s for s in SCENARIOS if s.name == "flat_walk")
        case = replace(base, perturbations=Perturbations(impulses={14: (0.,0.,.01)}))
        reference = run(case)
        getter = NavigationSession.diagnostics.fget
        with patch.object(NavigationSession, "diagnostics", property(
                lambda session: replace(getter(session), controller_ids=()))):
            changed_names = run(case)
        self.assertTrue(reference.applied_perturbations)
        self.assertEqual(reference.applied_perturbations, changed_names.applied_perturbations)

    def test_public_faults_enter_the_migration_function_and_release_body(self):
        expected = ("planning_coordinator._retry_or_fail",
                    "navigation_session.handle_internal_contract_failure",
                    "navigation_session._resolve_pending_retry",
                    "navigation_session._wait_for_active_terminal")
        for name, function in zip(FAULT_CASES, expected):
            with self.subTest(name=name):
                record = observe(("faults/" + name, "faults", name))
                self.assertIsNone(record["exception"])
                self.assertTrue(record["passed"], record)
                self.assertIn(function, record["functions_entered"])
