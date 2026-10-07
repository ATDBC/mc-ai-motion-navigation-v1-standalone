"""Driver state stays typed and has one writer, while Session keeps lifecycle."""
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriverState
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.navigation_session import NavigationSessionState
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver


class RuntimeNavigationDriverStateTests(unittest.TestCase):
    def test_formal_runtime_scripts_compare_driver_states_as_enums(self):
        for name in ('r27_successor_gap_runtime.py', 'r28_product_fabric_runtime.py'):
            tree = ast.parse((Path('scripts') / name).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Compare) and any(isinstance(n, ast.Attribute)
                    and n.attr == 'state' and isinstance(n.value, ast.Name)
                    and n.value.id == 'driver' for n in ast.walk(node)):
                    self.assertFalse(any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                                         for n in ast.walk(node)), name)

    def test_state_has_only_constructor_and_transition_writer(self):
        tree = ast.parse(Path('mc2p/skills/navigation_session_driver.py').read_text())
        owner = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                     and n.name == 'RuntimeNavigationDriver')
        writers = {method.name for method in owner.body if isinstance(method, ast.FunctionDef)
                   for node in ast.walk(method) if isinstance(node, ast.Attribute)
                   and isinstance(node.value, ast.Name) and node.value.id == 'self'
                   and node.attr in {'state', '_state'} and isinstance(node.ctx, ast.Store)}
        self.assertEqual(writers, {'__init__', '_set_state'})
        for node in ast.walk(owner):
            if isinstance(node, ast.Compare) and any(isinstance(n, ast.Attribute)
                and n.attr in {'state', '_state'} and isinstance(n.value, ast.Name)
                and n.value.id == 'self' for n in ast.walk(node)):
                self.assertFalse(any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                                     for n in ast.walk(node)))

    def test_session_mapping_and_reason_do_not_infer_state(self):
        from mc2p.skills.navigation_session_driver import RuntimeNavigationDriverState as State
        driver = RuntimeNavigationDriver.__new__(RuntimeNavigationDriver)
        mapping = {NavigationSessionState.CANCELLING: State.STOPPING,
            NavigationSessionState.COMPLETE: State.SUCCESS,
            NavigationSessionState.CANCELLED: State.CANCELLED,
            NavigationSessionState.FAILED: State.FAILED,
            NavigationSessionState.CLOSED: State.FAILED,
            NavigationSessionState.REQUIRES_INTERACTION: State.INTERACTION_REQUIRED}
        for session_state in NavigationSessionState:
            driver.session = SimpleNamespace(report=SimpleNamespace(state=session_state, reason='failed'))
            driver._sync_report()
            self.assertIs(driver.state, mapping.get(session_state, State.RUNNING))
            self.assertEqual(driver.reason, 'failed')
        for state in State:
            driver._set_state(state, 'success')
            self.assertIs(driver.state, state)
            self.assertIs(type(driver.state.value), str)
        with self.assertRaises(ContractViolation): driver._set_state('running', 'bad')
        with self.assertRaises(AttributeError): driver.state = State.READY
