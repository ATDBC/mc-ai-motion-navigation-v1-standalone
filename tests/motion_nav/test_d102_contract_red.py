"""Five current contracts; explicit G0 recording retains its legacy RED probe."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests.motion_nav.d102_contract_cases import assert_contract, probe_current_contract, write_contract_evidence


class ContractRedTests(unittest.TestCase):
    def _check(self, method):
        facts = probe_current_contract(method)
        with TemporaryDirectory(prefix="d102-red-") as folder:
            write_contract_evidence(self.id(), facts, output=Path(folder))
            assert_contract(self, facts)

    def test_parent_inputs_are_independent(self):
        self._check("test_parent_inputs_are_independent")

    def test_snapshot_graph_is_immutable(self):
        self._check("test_snapshot_graph_is_immutable")

    def test_control_drain_is_not_receiver(self):
        self._check("test_control_drain_is_not_receiver")

    def test_job_retirement_preserves_execution_refs(self):
        self._check("test_job_retirement_preserves_execution_refs")

    def test_overdue_keeps_registered_tail(self):
        self._check("test_overdue_keeps_registered_tail")


if __name__ == "__main__":
    unittest.main()
