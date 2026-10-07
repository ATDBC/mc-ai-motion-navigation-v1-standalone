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
# These existing enums express lifecycle, facts, ownership or generic policy.
# Unknown closed classifications are always reported for review, including
# classifications renamed without a Controller/Action prefix.
NON_IMPLEMENTATION_ENUMS = set('''
ActionPreconditionStatus ActionPreconditionReason ActionRouteState
ActiveRouteValidationDisposition ActiveRouteValidationReason AdmissionReason AdmissionStatus
AirMotionState AsyncAdmissionDisposition AsyncWorkKind BodySelectionKind BodyControlPhase CalculationStatus
CellKnowledge ComputationInvalidationCause DependencyOwnerKind ExternalMotionSource
FieldStatusV0 FixedRouteState GapPreparationStatus GoalPlanningPolicy GoalReachPolicy
GoalSupport GroundHandoffDisposition GroundTraversalStatus GroundRouteGuardPhase HandoffDestination HandoffDisposition
InformationOutcome InputApplicationStatus InputResponsibilityDisposition InputResponsibilityStatus
InteractionKind JumpUpState LandingEdgeProbeState LocalAttemptVerdict ModeReadiness
MotionCandidateStatus MotionJobOperation MotionResidualStatus MotionTickPhase MovementMode
NavigationSessionEvent NavigationSessionState NavigationTransitionAction ObservedGoalStatus
PlacementState PlanningAttemptPermitKind PlanningFactRequirementKind PlanningRetryTrigger
PlanningStatus PlanningSubmissionStatus PlanningUpdateKind ProbeHandoffAction ProbeOutcomeKind
ProgressKind ProjectionStatus QueryStatus RecoveryBudgetKind RecoveryLimitStatus RecoveryRequestStatus
ResourceStatus RetryCause RiskActionState RiskCommitKind RiskReleaseEvidence RiskReservationStatus
RiskSubmissionStatus RolloutOutputMode SessionEventPolicy SnapshotBuildStatus SolveStatus StateBuildStatus
StepState StopCause SurfacePlanningStatus SurfaceSearchNeed TaskDemandState TraitStatus
VerifiedMotionExecutorState WaitVerdict WalkValidationQueryKind WorkCheck _BasisChange
RuntimeStateV1 RuntimeNavigationDriverState MovingMeleePhase MeleeStrikeOutcome KnownWorldFollowState
AttackEvidenceGrade TargetPositionSource AttackAttemptOutcome RecoveryDirective KnownWorldFollowStatus
AttackAttemptPhase ExternalMotionReentryStatus EngagementEventKind AttackTaskOutcome PlacementFailureKind
_ReleaseResult _ApproachExit _ApproachHandoff
'''.split())
CLASSIFICATION_NOTES = {
    'PlanningFrontierKind': '规划信息职责，不选择动作或求解实现',
    'ActionPriorityV0': '输入仲裁优先级，不选择运动控制器',
    'FixedMeleePhase': '战斗生命周期，不选择导航动作实现',
    'ComparisonOperatorV0': '成功条件运算符，不选择求解策略',
    'MovementMode': '通用地面模式与物理条件，不是控制器家族',
    'QueryStatus': '求解可行性、信息缺口与不支持结果',
    'BodyControlPhase': '通用身体责任状态',
    'BodyCommitment': '通用身体承诺能力，单列capability',
    'mc2p.motion_nav.physics_types.JAVA_1_21_RULESET': '冻结物理规则协议身份',
    'math.pi': '标准数学数值常量',
    'math.inf': '标准数学数值常量',
}
NON_IMPLEMENTATION_ENUMS.update({'PlanningFrontierKind', 'ActionPriorityV0',
                                'FixedMeleePhase', 'ComparisonOperatorV0'})
NEUTRAL_VALUE_SYMBOLS = {'mc2p.motion_nav.physics_types.JAVA_1_21_RULESET', 'math.pi', 'math.inf'}


def _symbols(node, aliases, declaration_nodes=()):
    result = set()
    for item in ast.walk(node):
        if id(item) in declaration_nodes:
            continue
        if isinstance(item, ast.Name):
            name = aliases.get(item.id, item.id).rsplit('.', 1)[-1]
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
    return any((isinstance(item, ast.Attribute) and item.attr in {'state', '_state'}
                and isinstance(item.value, ast.Name) and item.value.id == 'self')
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


def _qualified_name(node, aliases):
    """Resolve names and module attributes; no calls or general dataflow."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        owner = _qualified_name(node.value, aliases)
        return None if owner is None else owner + '.' + node.attr
    return None


def _source_segment(lines, node):
    """Reuse UTF-8 lines; AST columns are byte offsets, including Chinese text."""
    start, end = node.lineno - 1, node.end_lineno - 1
    if start == end:
        return lines[start][node.col_offset:node.end_col_offset].decode('utf-8')
    return b''.join([lines[start][node.col_offset:], *lines[start + 1:end],
                     lines[end][:node.end_col_offset]]).decode('utf-8')


def _local_enums(tree, aliases, nodes=None):
    enums = {}
    for node in tree.body:
        if (isinstance(node, ast.ClassDef)
                and any((_qualified_name(base, aliases) or '').rsplit('.', 1)[-1]
                        in {'Enum', 'StrEnum', 'IntEnum', 'Flag', 'IntFlag'}
                        for base in node.bases)):
            enums[node.name] = {target.id for statement in node.body
                for target in (statement.targets if isinstance(statement, ast.Assign)
                               else (statement.target,) if isinstance(statement, ast.AnnAssign) else ())
                if isinstance(target, ast.Name)}
    return enums


class _EnumModules:
    """Cached declaration lookup, without importing or executing repository code."""
    def __init__(self, root):
        self.root = root
        self.cache = {}
        self.resolved = {}
        self.resolving = set()

    def module(self, name):
        if name not in self.cache:
            path = self.root.joinpath(*name.split('.')).with_suffix('.py')
            if not path.is_file():
                path = self.root.joinpath(*name.split('.'), '__init__.py')
            if not path.is_file():
                self.cache[name] = None
            else:
                tree = ast.parse(path.read_text(encoding='utf-8-sig'))
                nodes = list(ast.walk(tree))
                import_context = name + '.__init__' if path.name == '__init__.py' else name
                aliases = _import_aliases(tree, import_context, nodes)
                bindings = _import_aliases(tree, import_context, tree.body)
                imports = bindings.copy()
                symbols = {n.name: 'class' for n in tree.body if isinstance(n, ast.ClassDef)}
                symbols.update({n.name: 'function' for n in tree.body
                                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))})
                expressions = defaultdict(list)
                for n in tree.body:
                    targets = (n.targets if isinstance(n, ast.Assign) else
                               (n.target,) if isinstance(n, ast.AnnAssign) else ())
                    symbols.update({t.id: ('value' if isinstance(n.value, ast.Constant) else 'unresolved')
                                    for t in targets if isinstance(t, ast.Name)})
                    if len(targets) == 1 and isinstance(targets[0], ast.Name):
                        expressions[targets[0].id].append(n.value)
                        value = _qualified_name(n.value, imports)
                        if value is not None:
                            first = value.split('.')[0]
                            if first not in {v.split('.')[0] for v in imports.values()}:
                                value = name + '.' + value
                            bindings[targets[0].id] = value
                self.cache[name] = (_local_enums(tree, aliases, nodes), aliases, symbols, tree, nodes, bindings, expressions)
                for identity in NEUTRAL_VALUE_SYMBOLS:
                    if identity.rsplit('.', 1)[0] == name:
                        bindings[identity.rsplit('.', 1)[1]] = identity
        return self.cache[name]

    def resolve(self, qualified, visited=()):
        """Resolve a module symbol or a finite Name/Attribute alias chain."""
        if qualified in visited or qualified in self.resolving:
            return ('unresolved', qualified.rsplit('.', 1)[-1])
        if qualified in self.resolved:
            return self.resolved[qualified]
        self.resolving.add(qualified)
        try:
            result = self._resolve(qualified, (*visited, qualified))
        finally:
            self.resolving.remove(qualified)
        self.resolved[qualified] = result
        return result

    def _resolve(self, qualified, visited):
        for identity in NEUTRAL_VALUE_SYMBOLS:
            if qualified == identity or qualified.startswith(identity + '.'):
                return ('neutral_value', identity)
        parts = qualified.split('.')
        if self.module(qualified) is not None:
            return ('module', qualified)
        for length in range(len(parts) - 1, 0, -1):
            data = self.module('.'.join(parts[:length]))
            if data is None:
                continue
            enums, _, symbols, _, _, bindings, expressions = data
            symbol = parts[length]
            identity = '.'.join(parts[:length + 1])
            if identity in NEUTRAL_VALUE_SYMBOLS:
                return ('neutral_value', identity)
            if symbol in enums:
                if len(parts) == length + 1:
                    return ('enum_type', symbol)
                if len(parts) == length + 2 and parts[-1] in enums[symbol]:
                    return ('enum_member', symbol)
                return ('unresolved', symbol)
            if symbol in expressions and len(expressions[symbol]) > 1:
                context = {key: '.'.join(parts[:length]) + '.' + key for key in symbols}
                context.update(bindings)
                sources = set().union(*(_derived_sources(value, context, self) for value in expressions[symbol]))
                if sources:
                    return ('derived', (frozenset(sources), frozenset({'multiple_assignment'})))
                return ('value', symbol)
            if symbol in bindings:
                return self.resolve('.'.join([bindings[symbol], *parts[length + 1:]]), visited)
            if symbol in expressions:
                context = {name: '.'.join(parts[:length]) + '.' + name for name in symbols}
                context.update(bindings)
                sources = set().union(*(_derived_sources(value, context, self) for value in expressions[symbol]))
                if sources:
                    kinds = {type(value).__name__ for value in expressions[symbol] if value is not None}
                    if len(expressions[symbol]) > 1:
                        kinds.add('multiple_assignment')
                    return ('derived', (frozenset(sources), frozenset(kinds)))
                return ('value', symbol)
            return (symbols.get(symbol, 'unresolved'), symbol)
        return ('unresolved', parts[-2] if len(parts) > 1 else parts[-1])


def _import_aliases(tree, module, nodes=None):
    aliases = {}
    for node in ast.walk(tree) if nodes is None else nodes:
        if isinstance(node, ast.ImportFrom):
            prefix = ('.'.join(module.split('.')[:-node.level]) if node.level else '')
            imported_module = '.'.join(part for part in (prefix, node.module) if part)
            for item in node.names:
                aliases[item.asname or item.name] = imported_module + '.' + item.name
        elif isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split('.')[0]] = (
                    item.name if item.asname else item.name.split('.')[0])
    return aliases


def _enum_references(node, aliases, local_enums, modules, import_roots):
    names = set()
    for item in ast.walk(node):
        name = _qualified_name(item, aliases)
        if name is None or '.' not in name:
            continue
        owner, member = name.rsplit('.', 1)
        enum_name = owner.rsplit('.', 1)[-1]
        declared = enum_name in local_enums and member in local_enums[enum_name]
        # Known capability/neutral enums are explicit, never inferred by case.
        imported = enum_name in NON_IMPLEMENTATION_ENUMS or enum_name in {
            'ControllerFamily', 'MotionSolveKind', 'BodyCommitment'}
        resolved = (modules.resolve(name) if not declared and not imported
                    and name.split('.')[0] in import_roots else None)
        if declared or imported:
            names.add(enum_name)
        elif resolved and resolved[0] == 'enum_member':
            names.add(resolved[1])
    return names


def _closed_attribute_references(node, aliases, imported_names, bound_names, class_names, modules):
    """Conservatively expose attribute values in closed equality/identity tests."""
    values = []
    if isinstance(node, ast.Compare):
        values = [value for operation, value in zip(node.ops, node.comparators)
                  if isinstance(operation, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot))]
    elif isinstance(node, ast.MatchValue):
        values = [node.value]
    names = set()
    neutral_values = set()
    for value in values:
        if not (isinstance(value, ast.Attribute) or isinstance(value, ast.Name)
                and value.id in aliases):
            continue
        # Attribute access through a bound value (frame.position, request.id)
        # is not a class/module selector. Imported names and class declarations
        # remain visible; unresolved free symbols are conservatively reviewed.
        qualified = _qualified_name(value, aliases)
        canonical_root = (qualified or '').split('.')[0]
        if canonical_root in bound_names and canonical_root not in class_names and canonical_root not in imported_names:
            continue
        if qualified and '.' in qualified:
            owner = qualified.rsplit('.', 1)[0]
            kind, original = modules.resolve(qualified)
            if kind == 'enum_member':
                names.add(original)
            elif kind == 'neutral_value':
                neutral_values.add(original)
            elif kind == 'unresolved' or kind == 'module' or kind == 'class' and isinstance(value, ast.Attribute):
                names.add(original if kind != 'module' else owner.rsplit('.', 1)[-1])
    return names, neutral_values


def _derived_sources(value, aliases, modules, unknown=None, roots=None):
    if value is None:
        return set()
    unknown = {} if unknown is None else unknown
    sources = set()
    roots = {value.split('.')[0] for value in aliases.values()} if roots is None else roots
    parents = {id(child): parent for parent in ast.walk(value) for child in ast.iter_child_nodes(parent)}
    for item in ast.walk(value):
        if isinstance(item, ast.Name) and item.id in unknown:
            sources.update(unknown[item.id])
        qualified = _qualified_name(item, aliases)
        if qualified and qualified.split('.')[0] in roots:
            kind, identity = modules.resolve(qualified)
            if kind in {'enum_type', 'enum_member', 'neutral_value'}:
                sources.add(identity)
            elif kind == 'derived':
                sources.update(identity[0])
            elif kind in {'module', 'unresolved'}:
                parent = parents.get(id(item))
                if not (isinstance(parent, ast.Attribute) and parent.value is item
                        or isinstance(parent, ast.Call) and parent.func is item):
                    sources.add('UNRESOLVED_MODULE:' + qualified)
    return sources


def _function_scopes(tree, module, global_aliases, modules):
    """Independent function tables; no path, closure or container evaluation."""
    functions = {}
    enclosing = {}
    selectors = (ast.Compare, ast.Dict, ast.MatchValue, ast.MatchClass)
    scopes = {}
    def visit(node, owner=None, conditional=False):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            enclosing[node] = owner
            owner = node
            conditional = False
            functions[node] = []
        if owner is not None:
            functions[owner].append((node, conditional))
        branch = conditional or isinstance(node, (ast.If, ast.For, ast.AsyncFor,
            ast.While, ast.Try, ast.Match))
        for child in ast.iter_child_nodes(node):
            visit(child, owner, branch)
    visit(tree)
    module_classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    for function, entries in functions.items():
        nodes = [n for n, _ in entries]
        assignments = defaultdict(list)
        for node, conditional in entries:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
                if len(targets) == 1 and isinstance(targets[0], ast.Name):
                    assignments[targets[0].id].append((node.value, conditional))
        args = {n.arg for n in ast.walk(function.args) if isinstance(n, ast.arg)}
        local_bound = args | {n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        bound = set(local_bound)
        outer = enclosing[function]
        while outer is not None:
            bound.update(n.arg for n in ast.walk(outer.args) if isinstance(n, ast.arg))
            bound.update(n.id for n, _ in functions[outer] if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
            outer = enclosing[outer]
        aliases = {name: value for name, value in global_aliases.items() if name not in bound}
        aliases.update(_import_aliases(tree, module, nodes))
        aliases.update({name: module + '.' + name for name in module_classes
                        if name not in bound and name not in aliases})
        imported = set(aliases)
        roots = {value.split('.')[0] for value in aliases.values()}
        unknown = {}
        expression_kinds = {}
        pending = set(assignments)

        def derived(value):
            return _derived_sources(value, aliases, modules, unknown, roots)

        def mark(name, sources, records):
            unknown[name] = sources
            expression_kinds[name] = sorted({type(value).__name__ for value, _ in records if value is not None}
                | ({'multiple_assignment'} if len(records) > 1 else set())
                | ({'control_flow'} if any(c for _, c in records) else set()))

        for _ in range(len(pending) + 1):
            changed = False
            for name in sorted(pending):
                records = assignments[name]
                value, conditional = records[0]
                if len(records) == 1 and not conditional and isinstance(value, (ast.Name, ast.Attribute)):
                    qualified = _qualified_name(value, aliases)
                    if qualified is None:
                        if derived(value):
                            mark(name, derived(value), records)
                            pending.remove(name)
                            changed = True
                        continue
                    base = qualified.split('.')[0]
                    if base in pending:
                        continue
                    if derived(value) and any(isinstance(n, ast.Name) and n.id in unknown
                                              for n in ast.walk(value)):
                        mark(name, derived(value), records)
                    else:
                        aliases[name] = qualified
                    pending.remove(name)
                    changed = True
                elif any(derived(v) for v, _ in records):
                    mark(name, set().union(*(derived(v) for v, _ in records)), records)
                    pending.remove(name)
                    changed = True
            if not changed:
                break
        # Unresolved local cycles with an identifiable enum source remain visible.
        for name in pending:
            if any(derived(v) for v, _ in assignments[name]):
                mark(name, set().union(*(derived(v) for v, _ in assignments[name])), assignments[name])
        context = (aliases, imported, bound, module_classes - bound, roots, unknown, expression_kinds)
        for node in nodes:
            if isinstance(node, selectors):
                scopes[id(node)] = context
    return scopes


def measure(root: Path) -> dict:
    package = root / 'mc2p/motion_nav'
    branches, unclassified, references, declarations = [], [], defaultdict(set), []
    modules, driver_strings, display_strings, session = {}, {}, {}, {}
    driver_writers, driver_flow_sites = {}, {}
    proxies, capabilities, declaration_checks, neutral_value_checks, derived_checks = [], [], [], [], []
    enum_modules = _EnumModules(root)
    paths = sorted(package.rglob('*.py')) + sorted((root / 'mc2p/skills').glob('*driver.py'))
    for path in paths:
        relative = (path.relative_to(package).as_posix() if path.is_relative_to(package)
                    else '../skills/' + path.name)
        source = path.read_text(encoding='utf-8-sig')
        source_lines = source.encode('utf-8').splitlines(keepends=True)
        module = '.'.join(path.relative_to(root).with_suffix('').parts)
        local_enums, initial_aliases, module_symbols, tree, nodes, bindings, expressions = enum_modules.module(module)
        aliases = {name: module + '.' + name for name in module_symbols}
        aliases.update(bindings)
        aliases.update({name: module + '.' + name for name, values in expressions.items() if len(values) > 1})
        imported_names = set(initial_aliases)
        import_roots = {value.split('.')[0] for value in aliases.values()} | {module.split('.')[0]}
        bound_names = {node.id for node in nodes if isinstance(node, ast.Name)
                       and isinstance(node.ctx, ast.Store)} | {
                       node.arg for node in nodes if isinstance(node, ast.arg)}
        class_names = {node.name for node in nodes if isinstance(node, ast.ClassDef)}
        scopes = _function_scopes(tree, module, aliases, enum_modules)
        declaration_nodes = set()
        for node in nodes:
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
                            'imports': sorted({node.module for node in nodes
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
            sites = []
            displays = set()
            for node in nodes:
                if isinstance(node, ast.Dict):
                    displays.update(n.value for n in ast.walk(node)
                                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                                    and n.value in {'ready', 'running', 'failed', 'cancelled',
                                                    'stopping', 'success', 'interaction_required'})
                if isinstance(node, ast.Compare) and _state_reference(node):
                    flow.update(_string_values(node))
                    sites.extend({'line': node.lineno, 'value': value}
                                 for value in sorted(_string_values(node)))
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        flow.update(_written_state_values(target, node.value))
                        sites.extend({'line': node.lineno, 'value': value}
                                     for value in sorted(_written_state_values(target, node.value)))
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    flow.update(_written_state_values(node.target, node.value))
            driver_strings[relative] = sorted(flow)
            driver_flow_sites[relative] = sites
            driver_writers[relative] = sorted({method.name
                for owner in tree.body if isinstance(owner, ast.ClassDef)
                for method in owner.body if isinstance(method, ast.FunctionDef)
                for node in ast.walk(method) if isinstance(node, ast.Attribute)
                and node.attr in {'state', '_state'} and isinstance(node.ctx, ast.Store)
                and isinstance(node.value, ast.Name) and node.value.id == 'self'})
            display_strings[relative] = sorted(displays)
        if relative in IMPLEMENTATIONS:
            continue
        for symbol in _symbols(tree, aliases, declaration_nodes):
            references[symbol].add(relative)
        for node in nodes:
            active_aliases, active_imported, active_bound, active_classes, active_roots, unknown, expression_kinds = scopes.get(
                id(node), (aliases, imported_names, bound_names, class_names, import_roots, {}, {}))
            syntax, symbols = None, set()
            if isinstance(node, ast.Compare):
                syntax, symbols = 'comparison', _symbols(node, active_aliases)
            elif isinstance(node, ast.Dict):
                syntax = 'mapping'
                symbols = set().union(*(_symbols(key, active_aliases) for key in node.keys if key))
            elif isinstance(node, ast.MatchClass):
                syntax, symbols = 'match', _symbols(node.cls, active_aliases)
            elif isinstance(node, ast.MatchValue):
                syntax, symbols = 'match_value', _symbols(node.value, active_aliases)
            # Solver internals implement their own strategies. The ruler
            # measures selection in shared callers, not those algorithms.
            if syntax and relative != 'motion_solver.py':
                selection = (node.keys if isinstance(node, ast.Dict) else (node,))
                enum_names = set().union(*(_enum_references(part, active_aliases, local_enums, enum_modules, active_roots)
                                          for part in selection if part is not None))
                closed, neutral_values = _closed_attribute_references(node, active_aliases, active_imported,
                    active_bound, active_classes, enum_modules)
                enum_names.update(closed)
                selected_nodes = [n for part in selection if part is not None for n in ast.walk(part)]
                used_derived = {n.id for n in selected_nodes if isinstance(n, ast.Name) and n.id in unknown}
                imported_derived = []
                for item in selected_nodes:
                    qualified = _qualified_name(item, active_aliases)
                    if qualified and qualified.split('.')[0] in active_roots:
                        kind, detail = enum_modules.resolve(qualified)
                        if kind == 'derived':
                            imported_derived.append(detail)
                if used_derived or imported_derived:
                    sources = set().union(*(unknown[name] for name in used_derived),
                                          *(detail[0] for detail in imported_derived))
                    safe = all(identity in NON_IMPLEMENTATION_ENUMS or identity == 'BodyCommitment'
                               or identity in NEUTRAL_VALUE_SYMBOLS for identity in sources)
                    unsafe_sources = sources - NON_IMPLEMENTATION_ENUMS - NEUTRAL_VALUE_SYMBOLS - {'BodyCommitment'}
                    constraint = (isinstance(node, ast.Compare) and all(isinstance(operation,
                        (ast.Lt, ast.LtE, ast.Gt, ast.GtE)) for operation in node.ops)
                        and (safe or unsafe_sources == {'MotionSolveKind'}
                             and any(isinstance(value, ast.Attribute) for value in node.comparators)))
                    enum_names.update(identity for identity in sources if identity in NON_IMPLEMENTATION_ENUMS
                                      or identity == 'BodyCommitment')
                    if safe:
                        neutral_values.update(sources & NEUTRAL_VALUE_SYMBOLS)
                    elif not constraint:
                        enum_names.add('UNKNOWN_ENUM_DERIVED')
                    derived_checks.append({'file': relative, 'line': node.lineno, 'syntax': syntax,
                        'sources': sorted(sources), 'expression_kinds': sorted(set().union(*(
                            set(expression_kinds[name]) for name in used_derived),
                            *(detail[1] for detail in imported_derived))), 'safe_only': safe,
                        'comparison_kind': 'derived_constraint' if constraint else 'closed_value_selection',
                        'classification_role': 'declaration_validation' if relative == 'actions/registry.py' else 'runtime_review',
                        'source': _source_segment(source_lines, node)})
                neutral_value_checks.extend({'file': relative, 'line': node.lineno, 'syntax': syntax,
                    'classification': identity, 'source': _source_segment(source_lines, node)}
                    for identity in sorted(neutral_values))
                for name in sorted(enum_names):
                    known = name in {'ControllerFamily', 'MotionSolveKind', 'BodyCommitment'}
                    suspicious = name not in NON_IMPLEMENTATION_ENUMS
                    if not known and not suspicious:
                        continue
                    row = {'file': relative, 'line': node.lineno, 'syntax': syntax,
                           'classification': name,
                           'source': _source_segment(source_lines, node)}
                    destination = (declaration_checks if relative == 'actions/registry.py'
                                   else capabilities if name == 'BodyCommitment' else proxies)
                    destination.append(row)
                    if not known and relative != 'actions/registry.py':
                        unclassified.append(row)
            if not symbols:
                continue
            row = {'file': relative, 'line': node.lineno, 'syntax': syntax,
                   'actions': sorted(symbols), 'category': CATEGORIES.get(relative),
                   'source': _source_segment(source_lines, node)}
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
    return {'schema_version': 'navigation-design-metrics-v3',
            'excluded_concrete_implementations': sorted(IMPLEMENTATIONS),
            'action_branch_count': len(branches), 'branches_by_action': dict(sorted(counts.items())),
            'action_branches': branches, 'unclassified': unclassified,
            'direct_action_dispatch': branches,
            'proxy_implementation_dispatch': proxies,
            'capability_checks': capabilities,
            'neutral_value_checks': neutral_value_checks,
            'derived_checks': derived_checks,
            'classification_notes': CLASSIFICATION_NOTES,
            'declaration_validation_checks': declaration_checks,
            'action_declarations': declarations,
            'action_files': {k: sorted(v) for k, v in sorted(references.items())},
            'session': session, 'driver_flow_strings': driver_strings,
            'driver_flow_string_sites': driver_flow_sites,
            'driver_state_writers': driver_writers,
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
