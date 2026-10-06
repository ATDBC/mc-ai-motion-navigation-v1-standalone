"""AST structure report. Only an individual action implementation is excluded."""
from __future__ import annotations
import argparse
import ast
from collections import Counter, defaultdict
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = {'WalkSegment', 'StepSegment', 'JumpUpSegment', 'JumpGapSegment',
           'ControlledDropSegment'}
IMPLEMENTATIONS = {'actions/controlled_drop.py'}
CATEGORIES = {
    'navigation_session.py': 'session coordination',
    'action_route.py': 'route contract',
    'action_route_executor.py': 'execution and body safety',
    'action_preconditions.py': 'entry and observations',
    'route_admission.py': 'admission and planning edge conversion',
    'motion_coordination.py': 'solve request and motion coordination',
    'route_body_controller.py': 'body ownership and walk dispatch',
}
COORDINATORS = ('navigation_session.py', 'motion_coordination.py',
                'planning_coordinator.py', 'execution_supervisor.py',
                'navigation_handoff.py', 'navigation_lifecycle.py',
                'information_acquisition.py')


def _symbols(node, aliases, declaration_nodes=()):
    result = set()
    for item in ast.walk(node):
        if id(item) in declaration_nodes:
            continue
        if isinstance(item, ast.Name):
            name = aliases.get(item.id, item.id)
        elif isinstance(item, ast.Attribute):
            name = item.attr
        elif isinstance(item, ast.Constant) and isinstance(item.value, str):
            name = item.value
        else:
            continue
        if name in ACTIONS or name.endswith('Segment'):
            result.add(name)
    return result


def _state_reference(node):
    return any((isinstance(item, ast.Attribute) and item.attr in {'state', '_state'})
               or (isinstance(item, ast.Name) and item.id in {'state', '_state'})
               for item in ast.walk(node))


def _string_values(node):
    return {item.value for item in ast.walk(node)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)}


def _written_state_values(target, value):
    if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
        return set().union(*(_written_state_values(t, v)
                             for t, v in zip(target.elts, value.elts)))
    if _state_reference(target) and not isinstance(value, ast.Dict):
        return _string_values(value)
    return set()


def measure(root: Path) -> dict:
    package = root / 'mc2p/motion_nav'
    branches, unclassified, references, declarations = [], [], defaultdict(set), []
    modules, driver_strings, display_strings, session = {}, {}, {}, {}
    paths = sorted(package.rglob('*.py')) + sorted((root / 'mc2p/skills').glob('*driver.py'))
    for path in paths:
        relative = (path.relative_to(package).as_posix() if path.is_relative_to(package)
                    else '../skills/' + path.name)
        source = path.read_text(encoding='utf-8-sig')
        tree = ast.parse(source)
        aliases = {}
        declaration_nodes = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for item in node.names:
                    aliases[item.asname or item.name] = item.name
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        aliases[target.id] = aliases.get(node.value.id, node.value.id)
            if isinstance(node, ast.ClassDef) and node.name in ACTIONS:
                declarations.append({'file': relative, 'line': node.lineno,
                                     'kind': 'segment_record', 'actions': [node.name]})
            elif (relative == 'action_route.py' and isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == 'RouteAction'
                          for target in node.targets)):
                declarations.append({'file': relative, 'line': node.lineno,
                                     'kind': 'route_type_union',
                                     'actions': sorted(_symbols(node.value, aliases))})
                declaration_nodes.update(id(item) for item in ast.walk(node.value))
        modules[relative] = {'lines': len(source.splitlines()),
                            'imports': sorted({node.module for node in ast.walk(tree)
                                               if isinstance(node, ast.ImportFrom)
                                               and node.module})}
        if relative == 'navigation_session.py':
            owner = next((node for node in tree.body if isinstance(node, ast.ClassDef)
                          and node.name == 'NavigationSession'), None)
            functions = ([node for node in owner.body if isinstance(node, ast.FunctionDef)]
                         if owner is not None else [])
            accessors = [node for node in functions if any(
                (isinstance(d, ast.Name) and d.id == 'property')
                or (isinstance(d, ast.Attribute) and d.attr in {'setter', 'getter', 'deleter'})
                for d in node.decorator_list)]
            property_names = {node.name for node in accessors}
            methods = [node for node in functions if node not in accessors]
            assigned = {node.attr for node in ast.walk(owner or ast.Module(body=[], type_ignores=[]))
                             if isinstance(node, ast.Attribute)
                             and isinstance(node.value, ast.Name)
                             and node.value.id == 'self'
                             and isinstance(node.ctx, ast.Store)}
            fields = sorted(assigned - property_names)
            session = {'lines': len(source.splitlines()), 'methods': len(methods),
                       'fields': fields, 'field_count': len(fields),
                       'assigned_properties': sorted(assigned & property_names),
                       'property_accessors': len(accessors),
                       'long_methods': [{'name': n.name, 'line': n.lineno,
                                         'lines': n.end_lineno - n.lineno + 1}
                                        for n in methods if n.end_lineno-n.lineno >= 49]}
        if 'driver' in relative:
            flow = set()
            displays = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    displays.update(n.value for n in ast.walk(node)
                                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                                    and n.value in {'ready', 'running', 'failed', 'cancelled',
                                                    'stopping', 'success', 'interaction_required'})
                if isinstance(node, ast.Compare) and _state_reference(node):
                    flow.update(_string_values(node))
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        flow.update(_written_state_values(target, node.value))
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    flow.update(_written_state_values(node.target, node.value))
            driver_strings[relative] = sorted(flow)
            display_strings[relative] = sorted(displays)
        if relative in IMPLEMENTATIONS:
            continue
        for symbol in _symbols(tree, aliases, declaration_nodes):
            references[symbol].add(relative)
        for node in ast.walk(tree):
            syntax, symbols = None, set()
            if isinstance(node, ast.Compare):
                syntax, symbols = 'comparison', _symbols(node, aliases)
            elif isinstance(node, ast.Dict):
                syntax = 'mapping'
                symbols = set().union(*(_symbols(key, aliases) for key in node.keys if key))
            elif isinstance(node, ast.MatchClass):
                syntax, symbols = 'match', _symbols(node.cls, aliases)
            if not symbols:
                continue
            row = {'file': relative, 'line': node.lineno, 'syntax': syntax,
                   'actions': sorted(symbols), 'category': CATEGORIES.get(relative),
                   'source': ast.get_source_segment(source, node)}
            branches.append(row)
            if row['category'] is None or symbols - ACTIONS:
                unclassified.append(row)
    counts = Counter(action for row in branches for action in row['actions'])
    lifecycle = {}
    for name in ('planning_coordinator.py', 'motion_coordination.py'):
        path = package / name
        if path.exists():
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            lifecycle[name] = [{'name': n.name, 'line': n.lineno}
                               for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                               and any(word in n.name for word in
                                       ('submit', 'poll', 'accept', 'cancel', 'retire',
                                        'deadline', 'identity', 'revalidate'))]
    return {'schema_version': 'navigation-design-metrics-v2',
            'excluded_concrete_implementations': sorted(IMPLEMENTATIONS),
            'action_branch_count': len(branches), 'branches_by_action': dict(sorted(counts.items())),
            'action_branches': branches, 'unclassified': unclassified,
            'action_declarations': declarations,
            'action_files': {k: sorted(v) for k, v in sorted(references.items())},
            'session': session, 'driver_flow_strings': driver_strings,
            'driver_report_mapping_strings': display_strings,
            'lifecycle_review_sites': lifecycle,
            'coordination_modules': {k: v for k, v in modules.items() if k in COORDINATORS},
            'production_lines': sum(item['lines'] for name, item in modules.items()
                                    if not name.startswith('../'))}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = measure(args.root)
    result = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result, encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('action_branch_count', 'branches_by_action',
                                           'production_lines')}, ensure_ascii=False))
    print('session:', report['session']['lines'], 'unclassified:', len(report['unclassified']))


if __name__ == '__main__':
    main()
