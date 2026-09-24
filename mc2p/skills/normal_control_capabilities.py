"""Load the bounded J3 control proof; new navigation consumers keep their own sources."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType

from mc2p.contracts.action_v1 import MovementV1
from mc2p.runtime.segmented_trace import iter_segmented_jsonl

SCHEMA = 'mc2p.normal-control-capability.v2'
KINDS = ('side', 'back', 'yaw_sweep', 'pitch_sweep', 'release')
CELLS = frozenset((backend, seed) for backend in ('standalone', 'craftground')
                  for seed in (21001, 21002))
CONTEXT = {
    'perception_profile': 4, 'perception_mode': 'surface',
    'render_mode': 'structured_only', 'memory_radius_blocks': 32,
    'memory_shape': 'sphere', 'memory_retention_ns': 60_000_000_000,
    'freshness_ns': 500_000_000, 'control_profile': 'normal_decoupled_v1',
    'yaw_feedback_revision': 2,
}


def sha256(path: Path) -> str:
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inside(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('capability path escapes source root')
    return path


def read_json(path: Path):
    return json.loads(Path(path).read_text('utf-8'))


def _verify_complete_stream(directory: Path) -> None:
    # The reader verifies segment hashes and completion totals only on
    # exhaustion. Existing-file enumeration cannot detect a missing manifest.
    try:
        count = sum(1 for _ in iter_segmented_jsonl(directory))
        if count == 0:
            raise ValueError('empty stream')
    except (OSError, ValueError) as error:
        raise ValueError(f'control stream is incomplete or invalid: {directory}: {error}') from error


def intersect_controls(records: list[dict]) -> tuple[tuple[int, int, str], ...]:
    """Intersect complete stages within each probe, never multiply separate axes."""
    selected = {}
    for record in records:
        cell = (record.get('backend'), record.get('seed'))
        kind = record.get('probe_kind')
        if cell not in CELLS or kind not in KINDS:
            raise ValueError('undeclared control matrix cell')
        key = (kind, *cell)
        if key in selected:
            raise ValueError('duplicate control matrix cell')
        selected[key] = record
    allowed = set()
    for kind in KINDS:
        if any((kind, *cell) not in selected for cell in CELLS):
            continue
        sets = []
        for cell in CELLS:
            record = selected[kind, *cell]
            if (record.get('engineering_status') != 'valid'
                    or record.get('capability_outcome') != 'passed'):
                sets.append(set())
                continue
            combinations = set()
            for item in record['metrics']['verified_combinations']:
                f, s, axes, stage = (item[k] for k in ('forward', 'strafe', 'look_axes', 'stage'))
                if (type(f) is not int or type(s) is not int or f not in (-1, 0, 1)
                        or s not in (-1, 0, 1) or axes not in ('fixed', 'yaw', 'pitch')
                        or stage not in ('moving', 'stationary_calibration', 'repeated_single_tick')
                        or (stage == 'stationary_calibration' and (f or s))):
                    raise ValueError('invalid measured control combination')
                # Keep stage in the intersection, so stationary and moving evidence
                # cannot be spliced together across backend/seed cells.
                combinations.add((f, s, axes, stage))
            sets.append(combinations)
        allowed.update(item[:3] for item in set.intersection(*sets))
    return tuple(sorted(allowed))


@dataclass(frozen=True)
class ControlCapabilities:
    allowed_controls: tuple[tuple[int, int, str], ...]
    source_fingerprints: tuple[tuple[str, str], ...]
    source_root: Path
    measured_look_degrees_per_second: float | None = None
    measured_speed_blocks_per_second: float | None = None

    def __post_init__(self):
        object.__setattr__(self, 'allowed_controls', tuple(tuple(x) for x in self.allowed_controls))
        object.__setattr__(self, 'source_fingerprints', tuple(tuple(x) for x in self.source_fingerprints))
        object.__setattr__(self, 'source_root', Path(self.source_root).resolve())

    @property
    def measurements(self):
        return MappingProxyType({
            'normal_speed_blocks_per_second': self.measured_speed_blocks_per_second,
            'look_rate_degrees_per_second': self.measured_look_degrees_per_second,
            'sample_delivery_ns': None,
        })

    def allows(self, movement: MovementV1, look_axes: str) -> bool:
        if type(movement) is not MovementV1 or movement.jump or movement.sprint or movement.sneak:
            return False
        key = (movement.forward, movement.strafe, look_axes)
        return key == (0, 0, 'fixed') or key in self.allowed_controls

    def verify_current_sources(self, root: Path | None = None) -> None:
        base = self.source_root if root is None else Path(root).resolve()
        for name, digest in self.source_fingerprints:
            path = inside(base, name)
            if not path.is_file() or sha256(path) != digest:
                raise ValueError('control source differs: ' + name)


def load_control_capabilities(path: Path, root: Path) -> ControlCapabilities:
    """Verify sealed proof bytes and derived permissions before constructing the gate.

    The builder replays complete real stages. Loading verifies their sealed bytes,
    original results, complete matrix, parameter binding and immutable archives.
    Current body/controller files are checked separately at driver startup.
    """
    from scripts.normal_control_probes import CONTROL_PARAMETERS, CONTROL_COMPATIBILITY_PATHS, _archive

    root = Path(root).resolve()
    doc = read_json(path)
    if (doc.get('schema_version') != SCHEMA or doc.get('context') != CONTEXT
            or doc.get('parameters') != CONTROL_PARAMETERS):
        raise ValueError('control capability schema or context differs')
    sealed = doc.get('evidence_fingerprints', {})
    if not sealed:
        raise ValueError('missing sealed control evidence')
    for name, digest in sealed.items():
        target = inside(root, name)
        if not target.is_file() or sha256(target) != digest:
            raise ValueError('control evidence hash differs: ' + name)
    records = []
    common_body = None
    yaw_manifests = []
    for row in doc['results']:
        run = inside(root, row['directory'])
        result_path = run / 'evidence-result.json'
        manifest_path = run / 'evidence/run-manifest.json'
        for target in (result_path, manifest_path):
            if target.relative_to(root).as_posix() not in sealed:
                raise ValueError('unsealed control result or manifest')
        result = read_json(result_path)
        manifest = read_json(manifest_path)
        if (row.get('result_sha256') != sealed[result_path.relative_to(root).as_posix()]
                or row.get('run_manifest_sha256') != sealed[manifest_path.relative_to(root).as_posix()]
                or row.get('source_tree_sha256') != manifest['source_archive']['tree_sha256']):
            raise ValueError('control result or manifest hash claim differs')
        summary_path = inside(root, row['source_summary'])
        if summary_path.relative_to(root).as_posix() not in sealed:
            raise ValueError('unsealed source summary')
        matching = [item for item in read_json(summary_path)['rows']
                    if Path(item['directory']).resolve() == run]
        if (len(matching) != 1 or matching[0]['outcome'] != 'passed'
                or matching[0]['engineering'] != 'valid'
                or matching[0]['metrics'] != result['metrics']
                or matching[0]['surface'].get('passed') is not True):
            raise ValueError('original source summary differs')
        evidence = run / 'evidence'
        for stream in ('control-requests', 'runtime-trace/trace', manifest['control_events_path']):
            _verify_complete_stream(inside(evidence, stream))
        required_files = list(evidence.glob('*.json')) + list(run.glob('*.json'))
        for subdir in ('source-archive', 'control-requests', 'runtime-trace',
                       manifest['control_events_path']):
            directory = inside(evidence, subdir)
            members = [p for p in directory.rglob('*') if p.is_file()]
            if not members:
                raise ValueError('unsealed or missing control stage stream')
            required_files.extend(members)
        if any(p.relative_to(root).as_posix() not in sealed for p in required_files):
            raise ValueError('unsealed control stage evidence')
        if (result['engineering_status'] != 'valid' or result['capability_outcome'] != 'passed'
                or result.get('errors') != [] or not all(result['checks'].values())
                or result.get('parent_ports_released') is not True
                or any(result[k] != row[k] for k in ('backend', 'seed', 'probe_kind'))
                or any(manifest[k] != row[k] for k in ('backend', 'seed', 'probe_kind'))
                or manifest['classification'] != ('development' if row['seed'] == 21001 else 'held_out')
                or manifest['control_parameters'] != CONTROL_PARAMETERS
                or result['metrics'] != row['metrics']):
            raise ValueError('control result, stage or identity differs')
        _archive(run / 'evidence', manifest)
        body = manifest['control_compatibility_fingerprints']
        if set(body) != set(CONTROL_COMPATIBILITY_PATHS):
            raise ValueError('control body closure differs')
        if common_body is not None and body != common_body:
            raise ValueError('mixed body fingerprints')
        common_body = body
        if row['probe_kind'] == 'yaw_sweep':
            yaw_manifests.append(manifest)
            if result['checks'].get('feedback_selected_and_bound') is not True:
                raise ValueError('missing yaw feedback proof')
        records.append(result)
    required = {(kind, *cell) for kind in KINDS for cell in CELLS}
    if {(r['probe_kind'], r['backend'], r['seed']) for r in records} != required:
        raise ValueError('incomplete control proof matrix')
    allowed = intersect_controls(records)
    if [list(item) for item in allowed] != doc['allowed_controls']:
        raise ValueError('allowed controls differ from measured intersection')
    dependencies = dict(common_body)
    for manifest in yaw_manifests:
        for name, digest in manifest['source_fingerprints'].items():
            if (name == 'mc2p/skills/normal_yaw_feedback.py'
                    or name.startswith('deployment/surface-depth-diagnostic/')
                    or name.startswith('artifacts/surface-cache/')
                    or name == 'scripts/surface_depth_probe/native.py'):
                if name in dependencies and dependencies[name] != digest:
                    raise ValueError('mixed current control dependencies')
                dependencies[name] = digest
    if dependencies != doc['compatibility_fingerprints']:
        raise ValueError('control dependency closure differs')
    look_rate = max(r['metrics']['max_actual_look_rate_degrees_per_second']
                    for r in records if r['seed'] == 21001)
    if doc['measurements'] != {
        'normal_speed_blocks_per_second': None,
        'look_rate_degrees_per_second': look_rate, 'sample_delivery_ns': None,
        'calibration_seed': 21001,
        'speed_limitation': 'single-tick bounded probes do not calibrate sustained speed',
    }:
        raise ValueError('development measurements differ')
    return ControlCapabilities(allowed, tuple(sorted(dependencies.items())), root, look_rate)
