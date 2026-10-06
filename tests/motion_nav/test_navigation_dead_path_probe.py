"""A swallowed assertion must still invalidate a dead-path proof."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from scripts import navigation_dead_path_probe as probe


class DeadPathProbeTests(unittest.TestCase):
    def test_caught_entry_assertion_cannot_be_reported_as_zero_calls(self):
        class NavigationSession:
            def _cell_fact_id(self): pass
        def swallowed(*args, **kwargs):
            try:
                NavigationSession()._cell_fact_id()
            except AssertionError:
                pass
            return {'passed': True}
        target = 'navigation_session.NavigationSession._cell_fact_id'
        with (patch.object(probe.importlib, 'import_module', return_value=
                           SimpleNamespace(NavigationSession=NavigationSession)),
              patch.object(probe, 'execute_case', swallowed)):
            with self.assertRaises(AssertionError):
                probe._execute((target, 'faults', ('case', 'faults', 'fake')))
        self.assertEqual(probe._dead_calls, 1)


if __name__ == '__main__':
    unittest.main()
