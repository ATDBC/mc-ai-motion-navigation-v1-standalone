"""Audit typed navigation states through simple references in the whole repository."""
import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


def _name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return (_name(node.value) or '') + '.' + node.attr
    return None


def audit(path):
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    references = set()
    typed_attributes = set()
    assignments = {}
    factories = set()
    # approach_driver is the public field declared as RuntimeNavigationDriver
    # by both melee consumers. Its callers do not import the navigation class.
    def navigation(node):
        return (_name(node) in references
                or isinstance(node, ast.Attribute) and node.attr in typed_attributes
                or isinstance(node, ast.Attribute) and node.attr == 'approach_driver'
                or isinstance(node, ast.Call) and ((_name(node.func) or '').split('.')[-1]
                    in {'RuntimeNavigationDriver', *factories}))

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and any(
                isinstance(n, ast.Return) and isinstance(n.value, ast.Call)
                and (_name(n.value.func) or '').endswith('RuntimeNavigationDriver')
                for n in ast.walk(node)):
            factories.add(node.name)
        if isinstance(node, ast.AnnAssign) and any(isinstance(n, ast.Name)
                and n.id == 'RuntimeNavigationDriver' for n in ast.walk(node.annotation)):
            references.add(_name(node.target))
            if isinstance(node.target, ast.Attribute):
                typed_attributes.add(node.target.attr)
            elif isinstance(node.target, ast.Name):
                typed_attributes.add(node.target.id)
        if isinstance(node, ast.arg) and node.annotation is not None and any(
                isinstance(n, ast.Name) and n.id == 'RuntimeNavigationDriver'
                for n in ast.walk(node.annotation)):
            references.add(node.arg)
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                assignments[_name(target)] = node.value
    for _ in range(3):
        for target, value in assignments.items():
            if navigation(value):
                references.add(target)

    def state(node):
        return isinstance(node, ast.Attribute) and node.attr == 'state' and navigation(node.value)

    def typed(node, seen=()):
        name = _name(node)
        if name and 'RuntimeNavigationDriverState.' in name:
            return True
        if isinstance(node, ast.Call) and (_name(node.func) or '').endswith('RuntimeNavigationDriverState'):
            return True
        if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
            return all(typed(n, seen) for n in node.elts)
        if isinstance(node, ast.IfExp):
            return typed(node.body, seen) and typed(node.orelse, seen)
        if isinstance(node, ast.GeneratorExp):
            return typed(node.elt, seen)
        if isinstance(node, ast.Call) and (_name(node.func) or '') == 'tuple':
            return all(typed(n, seen) for n in node.args)
        if name in assignments and name not in seen:
            return typed(assignments[name], (*seen, name))
        return isinstance(node, ast.Constant) and node.value is None

    issues = []
    for node in ast.walk(tree):
        if state(node) and isinstance(node.ctx, ast.Store):
            issues.append((node.lineno, 'state assignment'))
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(state(n) for n in operands):
                for operand in operands:
                    if not state(operand) and not typed(operand):
                        issues.append((node.lineno, 'untyped comparison'))
        if isinstance(node, ast.Dict):
            issues.extend((n.lineno, 'raw state in JSON') for n in node.values if state(n))
        if isinstance(node, ast.keyword) and state(node.value):
            issues.append((node.value.lineno, 'raw state keyword'))
        if isinstance(node, ast.FormattedValue) and state(node.value):
            issues.append((node.value.lineno, 'raw state in log'))
    return issues


class RuntimeNavigationStateConsumerTests(unittest.TestCase):
    def test_whole_repository_consumers_use_enum_and_string_boundaries(self):
        failures = {}
        for directory in ('mc2p', 'scripts', 'tests'):
            for path in sorted((ROOT / directory).rglob('*.py')):
                if path.name in {Path(__file__).name, 'test_runtime_navigation_driver_state.py'}:
                    continue  # The Driver contract test intentionally verifies forbidden assignment.
                issues = audit(path)
                if issues:
                    failures[path.relative_to(ROOT).as_posix()] = issues
        self.assertEqual(failures, {})

    def test_audit_detects_alias_comparison_and_output(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'probe.py'
            path.write_text('driver = RuntimeNavigationDriver(runtime, session)\n'
                'alias = driver\nexpected = "success"\nif alias.state == expected: pass\n'
                'row = {"state": alias.state}\nalias.state = "failed"\n')
            self.assertEqual({reason for _, reason in audit(path)},
                             {'untyped comparison', 'raw state in JSON', 'state assignment'})
