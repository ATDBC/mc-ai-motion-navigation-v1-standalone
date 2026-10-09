"""The structure ruler must see dispatch hidden behind syntax or aliases."""
import tempfile
from pathlib import Path
import unittest

from scripts.navigation_design_metrics import NON_IMPLEMENTATION_ENUMS, measure


class NavigationDesignMetricsTests(unittest.TestCase):
    def test_handoff_disposition_is_internal_lifecycle(self):
        self.assertIn('HandoffDisposition', NON_IMPLEMENTATION_ENUMS)
        root = Path(__file__).resolve().parents[2]
        report = measure(root)
        self.assertEqual(report['unclassified'], [])
        self.assertEqual(len(report['capability_checks']), 2)
        self.assertEqual(len(report['proxy_implementation_dispatch']), 1)
        self.assertEqual(len(report['direct_action_dispatch']), 42)
        for rows in (report['proxy_implementation_dispatch'], report['direct_action_dispatch']):
            self.assertFalse(any(row.get('classification') == 'HandoffDisposition' for row in rows))

    def test_reassigned_module_name_uses_aggregate_before_last_binding(self):
        declarations = '''
from enum import StrEnum
class dispatch_mode(StrEnum):
    hover = "hover"
class BodyCommitment(StrEnum):
    TRANSITION = "transition"
class QueryStatus(StrEnum):
    READY = "ready"
'''
        for assignments, proxy, derived in (
            ('selected=dispatch_mode.hover\nselected=BodyCommitment.TRANSITION\n', 1, 1),
            ('selected=BodyCommitment.TRANSITION\nselected=dispatch_mode.hover\n', 1, 1),
            ('selected=BodyCommitment.TRANSITION\nselected=QueryStatus.READY\n', 0, 1),
            ('selected=BodyCommitment.TRANSITION\n', 0, 0),
        ):
            with self.subTest(assignments=assignments):
                report = self.report({'action_route_executor.py': declarations + assignments + 'if mode is selected: create()\n'})
                self.assertEqual(len(report['proxy_implementation_dispatch']), proxy)
                self.assertEqual(len(report['unclassified']), proxy)
                self.assertEqual(len(report['capability_checks']), 1)
                self.assertEqual(len(report['derived_checks']), derived)

    def test_simple_module_links_do_not_erase_reassignment_aggregate(self):
        declarations = '''
from enum import StrEnum
class dispatch_mode(StrEnum):
    hover = "hover"
class BodyCommitment(StrEnum):
    TRANSITION = "transition"
'''
        for assignments, proxy, derived in (
            ('selected=dispatch_mode.hover\nselected=BodyCommitment.TRANSITION\n', 2, 2),
            ('selected=BodyCommitment.TRANSITION\nselected=dispatch_mode.hover\n', 2, 2),
            ('selected=BodyCommitment.TRANSITION\n', 0, 0),
        ):
            with self.subTest(assignments=assignments):
                source = declarations + assignments + 'copy=selected\nlater: object=copy\nif mode is later: create()\n'
                report = self.report({'kinds.py': source,
                    'action_route_executor.py': 'from .kinds import later\nif mode is later: create()\n'})
                self.assertEqual(len(report['proxy_implementation_dispatch']), proxy)
                self.assertEqual(len(report['unclassified']), proxy)
                self.assertEqual(len(report['capability_checks']), 2)
                self.assertEqual(len(report['derived_checks']), derived)

    def test_module_dynamic_sources_survive_direct_use_and_reexports(self):
        report = self.report({'kinds.py': '''
from enum import StrEnum
class dispatch_mode(StrEnum):
    hover = "hover"
class BodyCommitment(StrEnum):
    TRANSITION = "transition"
selected = getattr(dispatch_mode, "hover")
container: object = [dispatch_mode.hover][0]
hold = getattr(BodyCommitment, "TRANSITION")
ordinary = build_number()
if mode is selected: create()
''', 'facade.py': 'from .kinds import selected as Export, container as Member, hold as Hold, ordinary as Count\n',
            'action_route_executor.py': '''
from .facade import Export, Member, Hold, Count
if mode is Export: create()
if mode is Member: create()
if hold is Hold: protect()
if count == Count: pass
'''})
        self.assertEqual(len(report['proxy_implementation_dispatch']), 3)
        self.assertEqual(len(report['unclassified']), 3)
        self.assertEqual(len(report['capability_checks']), 1)
        self.assertEqual(len(report['derived_checks']), 4)

    def test_package_reexport_and_reassigned_module_symbol_keep_sources(self):
        report = self.report({'package/kinds.py': 'from enum import StrEnum\nclass choice(StrEnum):\n    hover="hover"\n',
            'package/__init__.py': 'from .kinds import choice\nselected = choice.hover\nselected = 1\n',
            'action_route_executor.py': 'from .package import choice as Mode, selected\nif mode is Mode.hover: create()\nif mode is selected: create()\n'})
        self.assertEqual(len(report['proxy_implementation_dispatch']), 2)
        self.assertEqual(len(report['unclassified']), 2)
        self.assertEqual(len(report['derived_checks']), 1)

    def test_source_detail_preserves_utf8_ast_byte_offsets(self):
        source = '说明 = "测试"; result = mode is NewChoice.hover\n'
        report = self.report({'action_route_executor.py': source})
        self.assertEqual(report['unclassified'][0]['source'], 'mode is NewChoice.hover')

    def test_dynamic_sources_keep_safe_unknown_and_mixed_classifications(self):
        report = self.report({'kinds.py': '''
from enum import StrEnum
class dispatch_mode(StrEnum):
    hover = "hover"
class BodyCommitment(StrEnum):
    TRANSITION = "transition"
class QueryStatus(StrEnum):
    READY = "ready"
''', 'action_route_executor.py': '''
from .kinds import dispatch_mode, BodyCommitment, QueryStatus
import missing as unknown_module
def choose(mode):
    safe = [BodyCommitment.TRANSITION, QueryStatus.READY][0]
    unknown = getattr(dispatch_mode, "hover")
    mixed = [dispatch_mode.hover, BodyCommitment.TRANSITION][0]
    opaque = getattr(unknown_module, "hover")
    if mode is safe: wait()
    if mode is unknown: create()
    if mode is mixed: create()
    if mode is opaque: create()
    if mode > unknown: create()
'''})
        self.assertEqual(len(report['proxy_implementation_dispatch']), 4)
        self.assertEqual(len(report['unclassified']), 4)
        self.assertEqual(len(report['capability_checks']), 2)
        self.assertEqual(len(report['derived_checks']), 5)
        self.assertEqual([r['safe_only'] for r in report['derived_checks']], [True, False, False, False, False])

    def test_repository_derived_details_are_deterministic_and_constraints_are_visible(self):
        root = Path(__file__).resolve().parents[2]
        first, second = measure(root), measure(root)
        self.assertEqual(first['derived_checks'], second['derived_checks'])
        self.assertEqual(first['unclassified'], [])
        self.assertEqual(len(first['capability_checks']), 2)
        constraints = [r for r in first['derived_checks'] if 'MotionSolveKind' in r['sources']
                       and r['classification_role'] != 'declaration_validation']
        self.assertTrue(constraints)
        self.assertTrue(all(r['comparison_kind'] == 'derived_constraint' for r in constraints))
        self.assertEqual(len(first['proxy_implementation_dispatch']), 1)

    def test_function_aliases_and_dynamic_enum_sources_cannot_disappear(self):
        for statement, comparison in (
            ('Alias = dispatch_mode', 'Alias.hover'),
            ('Alias: object = dispatch_mode\n    member: object = Alias.hover', 'member'),
            ('Alias = getattr(dispatch_mode, "hover")', 'Alias'),
            ('Alias = [dispatch_mode.hover][0]', 'Alias'),
        ):
            with self.subTest(statement=statement):
                source = 'from .kinds import dispatch_mode\ndef choose(mode):\n    ' + statement + '\n    if mode is ' + comparison + ': create()\n'
                report = self.report({'kinds.py': 'from enum import StrEnum\nclass dispatch_mode(StrEnum):\n    hover="hover"\n',
                    'action_route_executor.py': source})
                self.assertEqual(len(report['proxy_implementation_dispatch']), 1)
                self.assertEqual(len(report['unclassified']), 1)

    def test_function_scopes_do_not_share_local_aliases(self):
        report = self.report({'kinds.py': 'from enum import StrEnum\nclass dispatch_mode(StrEnum):\n    hover="hover"\n',
            'action_route_executor.py': '''
from .kinds import dispatch_mode
def first(mode):
    Alias = dispatch_mode
    if mode is Alias.hover: create()
def second(mode, frame, request):
    Alias = request
    ordinary = frame.sequence_id
    if ordinary == Alias.sequence_id: pass
def third(mode):
    Alias = dispatch_mode
    Alias = 1
    if mode is Alias: create()
def fourth(mode, condition):
    if condition:
        Alias = dispatch_mode
    if mode is Alias: create()
'''})
        self.assertEqual(len(report['proxy_implementation_dispatch']), 3)
        self.assertEqual(len(report['unclassified']), 3)
        self.assertEqual({r['line'] for r in report['unclassified']}, {5, 13, 17})

    def test_module_type_and_member_reexports_preserve_enum_identity(self):
        report = self.report({'kinds.py': '''
from enum import StrEnum
class dispatch_mode(StrEnum):
    hover = "hover"
Alias = dispatch_mode
Later: object = Alias
hover = Later.hover
''', 'facade.py': '''
from .kinds import Later as Choice, hover as selected
Export = Choice
''', 'action_route_executor.py': '''
from .kinds import Alias, hover
from .facade import Export as Renamed, selected
if mode is Alias.hover: create()
if mode is hover: create()
if mode is Renamed.hover: create()
if mode is selected: create()
'''})
        self.assertEqual(len(report['proxy_implementation_dispatch']), 4)
        self.assertEqual(len(report['unclassified']), 4)
        self.assertEqual({r['classification'] for r in report['unclassified']}, {'dispatch_mode'})

    def test_module_reexports_preserve_capability_and_neutral_classification(self):
        report = self.report({'contracts.py': '''
from enum import StrEnum
class BodyCommitment(StrEnum):
    TRANSITION = "transition"
class _ReleaseResult(StrEnum):
    PENDING = "pending"
''', 'facade.py': '''
from .contracts import BodyCommitment as Choice, _ReleaseResult as Result
Hold = Choice.TRANSITION
Wait = Result.PENDING
''', 'action_route_executor.py': '''
from .facade import Choice, Result, Hold, Wait
if hold is Choice.TRANSITION: protect()
if outcome is Result.PENDING: wait()
if hold is Hold: protect()
if outcome is Wait: wait()
'''})
        self.assertEqual(len(report['capability_checks']), 2)
        self.assertEqual(report['proxy_implementation_dispatch'], [])
        self.assertEqual(report['unclassified'], [])

    def test_cyclic_module_aliases_are_visible_without_recursion_failure(self):
        report = self.report({'kinds.py': 'first = second\nsecond: object = first\n',
            'action_route_executor.py': 'from .kinds import first as Choice\nif mode is Choice.hover: create()\n'})
        self.assertEqual(len(report['proxy_implementation_dispatch']), 1)
        self.assertEqual(len(report['unclassified']), 1)

    def test_unresolved_module_symbols_and_cross_module_cycles_are_visible(self):
        report = self.report({'kinds.py': 'from .facade import Choice\nimport missing as factory\nDynamic = getattr(factory, "enum")\n',
            'facade.py': 'from .kinds import Choice\n', 'action_route_executor.py': '''
from .kinds import Dynamic, Choice
if mode is Dynamic.hover: create()
if mode is Choice: create()
'''})
        self.assertEqual(len(report['proxy_implementation_dispatch']), 2)
        self.assertEqual(len(report['unclassified']), 2)

    def test_protocol_identity_value_survives_alias_without_hiding_unknowns(self):
        report = self.report({'physics_types.py': 'JAVA_1_21_RULESET = build_rules()\n',
            'kinds.py': 'from enum import StrEnum\nclass Unknown(StrEnum):\n    ruleset_id = "unknown"\n',
            'facade.py': 'from .physics_types import JAVA_1_21_RULESET as Rules\nfrom .kinds import Unknown\nAlias = Rules\nOther = getattr(Unknown, "ruleset_id")\nContainer = [Unknown.ruleset_id]\n',
            'action_route_executor.py': '''
from .facade import Alias as Renamed, Other, Container
if identity != Renamed.ruleset_id: reject()
if identity != Other.ruleset_id: reject()
if identity is Container: reject()
'''})
        self.assertEqual(len(report['neutral_value_checks']), 1)
        self.assertEqual(report['neutral_value_checks'][0]['classification'],
                         'mc2p.motion_nav.physics_types.JAVA_1_21_RULESET')
        self.assertEqual(len(report['proxy_implementation_dispatch']), 2)
        self.assertEqual(len(report['unclassified']), 2)

    def test_imported_closed_enum_comparison_does_not_depend_on_case(self):
        report = self.report({'kinds.py': 'from enum import StrEnum\nclass dispatch_mode(StrEnum):\n    hover = "hover"\n',
            'action_route_executor.py': 'from .kinds import dispatch_mode\nif mode is dispatch_mode.hover: create()\n'})
        self.assertEqual(len(report['proxy_implementation_dispatch']), 1)
        self.assertEqual(len(report['unclassified']), 1)

    def test_imported_enum_aliases_and_non_selector_values(self):
        report = self.report({'kinds.py': '''
from enum import Enum as enum_base
class dispatch_mode(enum_base):
    hover = 1
limit = 42
''', 'action_route_executor.py': '''
from .kinds import dispatch_mode as Mode
import mc2p.motion_nav.kinds as kinds
family = kinds.dispatch_mode
member = family.hover
if mode is Mode.hover: create()
if mode is member: create()
def validate(frame, request):
    if frame.sequence_id == request.sequence_id: pass
from .kinds import limit
reserved = limit
if value != reserved: pass
''', 'body.py': '''
from mc2p.motion_nav.body_control import BodyCommitment as Commitment
from missing import _ReleaseResult as Release
if hold is Commitment.TRANSITION: wait()
if result is Release.PENDING: wait()
'''})
        self.assertEqual(len(report['proxy_implementation_dispatch']), 2)
        self.assertEqual(len(report['unclassified']), 2)
        self.assertEqual(len(report['capability_checks']), 1)

    def test_local_enum_members_and_simple_aliases_ignore_case(self):
        for source, unknown in (
            ('from enum import StrEnum\nclass dispatch_mode(StrEnum):\n    HOVER = "hover"\nif mode is dispatch_mode.HOVER: create()\n', True),
            ('from enum import StrEnum\nclass DispatchMode(StrEnum):\n    hover = "hover"\nif mode is DispatchMode.hover: create()\n', True),
            ('import kinds\nfamily = kinds.ControllerFamily\nif mode is family.AIR: create()\n', False),
            ('from enum import Enum as E\nclass mode(E):\n    hover = 1\nFirst = mode\nSecond = First\nselected = Second.hover\nif value is selected: create()\n', True),
        ):
            with self.subTest(source=source):
                report = self.report({'action_route_executor.py': source})
                self.assertEqual(len(report['proxy_implementation_dispatch']), 1)
                self.assertEqual(len(report['unclassified']), int(unknown))

    def test_known_private_handoff_results_are_not_action_dispatch(self):
        report = self.report({'action_route_executor.py': '''
if release is _ReleaseResult.PENDING: wait()
if handoff is _ApproachHandoff.PENDING: wait()
if exit_status is _ApproachExit.RUNNING: wait()
'''})
        self.assertEqual(report['proxy_implementation_dispatch'], [])
        self.assertEqual(report['unclassified'], [])

    def test_proxy_dispatch_survives_rename_attribute_chain_and_match_value(self):
        for source in (
            'if mode is DispatchMode.AIR: create()\n',
            'class _DispatchMode(StrEnum):\n    HOVER = "hover"\nif mode is _DispatchMode.HOVER: create()\n',
            'if family is kinds.ControllerFamily.AIR: create()\n',
            'match spec.controller_family:\n    case ControllerFamily.AIR: create()\n',
            'match mode:\n    case DispatchMode.HOVER: create()\n',
        ):
            with self.subTest(source=source):
                report = self.report({'action_route_executor.py': source})
                self.assertEqual(len(report['proxy_implementation_dispatch']), 1)
                self.assertEqual(len(report['unclassified']), int('DispatchMode' in source))

    def test_registry_completeness_check_is_separate_from_runtime_selection(self):
        report = self.report({'actions/registry.py': '''
if spec.controller_family is ControllerFamily.AIR and not callable(spec.factory): reject()
'''})
        self.assertEqual(report['proxy_implementation_dispatch'], [])
        self.assertEqual(len(report['declaration_validation_checks']), 1)

    def test_renamed_controller_classification_is_not_silently_ignored(self):
        report = self.report({'action_route_executor.py': '''
if spec.controller is NewControllerKind.HOVER: create()
'''})
        self.assertEqual(len(report['proxy_implementation_dispatch']), 1)
        self.assertEqual(len(report['unclassified']), 1)

    def test_proxy_dispatch_and_capabilities_are_separate(self):
        report = self.report({'action_route_executor.py': '''
from x import ControllerFamily as Family, MotionSolveKind as Solve
if spec.controller_family is Family.AIR: create()
if kind is Solve.JUMP_GAP: choose_policy()
if spec.body_commitment is BodyCommitment.TRANSITION: hold()
'''})
        self.assertEqual(report['schema_version'], 'navigation-design-metrics-v3')
        self.assertEqual(report['action_branch_count'], 0)
        self.assertEqual(len(report['proxy_implementation_dispatch']), 2)
        self.assertEqual(len(report['capability_checks']), 1)
        self.assertEqual(report['unclassified'], [])

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

    def test_generic_body_phase_alias_is_neutral(self):
        report = self.report({'route_body_controller.py': '''
phase = BodyControlPhase.RETAIN
if phase is None: pass
if phase is BodyControlPhase.QUIESCENT: pass
'''})
        self.assertEqual(report['proxy_implementation_dispatch'], [])
        self.assertEqual(report['unclassified'], [])

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
