"""The structure ruler must see dispatch hidden behind syntax or aliases."""
import tempfile
from pathlib import Path
import unittest

from scripts.navigation_design_metrics import measure


class NavigationDesignMetricsTests(unittest.TestCase):
    def report(self, files):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, source in files.items():
                path = root / 'mc2p/motion_nav' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source, encoding='utf-8')
            return measure(root)

    def test_dispatch_alias_mapping_match_and_string_are_visible(self):
        report = self.report({'navigation_session.py': '''
from x import ControlledDropSegment as Drop
Alias = Drop
def select(action):
    if type(action) is Alias: return 1
    mapping = {Drop: 2}
    if type(action).__name__ == "ControlledDropSegment": return 3
    match action:
        case Drop(): return 4
'''})
        self.assertEqual(report['action_branch_count'], 4)
        self.assertEqual(report['branches_by_action']['ControlledDropSegment'], 4)
        self.assertEqual({row['syntax'] for row in report['action_branches']},
                         {'comparison', 'mapping', 'match'})

    def test_only_specific_implementations_are_excluded(self):
        source = 'if type(action) is ControlledDropSegment: pass\n'
        report = self.report({'actions/controlled_drop.py': source,
                              'actions/registry.py': source,
                              'actions/existing.py': source,
                              'mystery.py': source})
        self.assertEqual(report['action_branch_count'], 3)
        self.assertEqual(len(report['unclassified']), 3)
        self.assertEqual(report['action_files']['ControlledDropSegment'],
                         ['actions/existing.py', 'actions/registry.py', 'mystery.py'])

    def test_session_fields_long_methods_and_driver_flow_strings(self):
        report = self.report({'navigation_session.py': '''
class NavigationSession:
    def __init__(self): self._owner = None
    def propose(self): self._owner = 1
''', 'known_world_navigation_driver.py': '''
class Driver:
    def advance(self):
        if self.state == "ready": self.state = "failed"
    def report(self): return {"ready": "正在运行"}
'''})
        self.assertEqual(report['session']['methods'], 2)
        self.assertEqual(report['session']['fields'], ['_owner'])
        self.assertEqual(report['driver_flow_strings']['known_world_navigation_driver.py'],
                         ['failed', 'ready'])

    def test_unknown_segment_is_unclassified_instead_of_silently_ignored(self):
        report = self.report({'mystery.py': 'if type(action) is NewSegment: pass\n'})
        self.assertEqual(report['action_branch_count'], 1)
        self.assertEqual(len(report['unclassified']), 1)

    def test_session_methods_and_owned_fields_exclude_port_and_forwarded_properties(self):
        report = self.report({'navigation_session.py': '''
class NavigationSessionPort:
    def foreign(self): pass
class NavigationSession:
    def __init__(self):
        self._stored = None
        self.owner = None
    @property
    def owner(self): return self._stored
    @owner.setter
    def owner(self, value): self._stored = value
    def propose(self):
        def local(): pass
        self.owner = 1
'''})
        self.assertEqual(report['session']['methods'], 2)
        self.assertEqual(report['session']['fields'], ['_stored'])
        self.assertEqual(report['session']['assigned_properties'], ['owner'])
        self.assertEqual(report['session']['property_accessors'], 2)

    def test_driver_state_literals_are_not_limited_to_a_frozen_vocabulary(self):
        report = self.report({'navigation_driver.py': '''
class Driver:
    def advance(self):
        if self.state == "braking": self.state = "stopped"
        self.state, self.reason = "draining", "unrelated_reason"
    def report(self): return {"ready": "正在运行"}
'''})
        self.assertEqual(report['driver_flow_strings']['navigation_driver.py'],
                         ['braking', 'draining', 'stopped'])

    def test_route_type_declarations_are_separate_from_runtime_dispatch(self):
        report = self.report({'action_route.py': '''
class ControlledDropSegment: pass
RouteAction = WalkSegment | ControlledDropSegment
''', 'route_admission.py': 'def edge(value): return ControlledDropSegment(value)\n'})
        self.assertEqual(report['action_files']['ControlledDropSegment'], ['route_admission.py'])
        self.assertEqual(len(report['action_declarations']), 2)
        self.assertEqual(report['action_branch_count'], 0)


if __name__ == '__main__':
    unittest.main()
