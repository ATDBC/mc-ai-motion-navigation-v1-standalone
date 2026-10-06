from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
from time import perf_counter_ns

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mc2p.motion_nav.route_admission import AdmissionStatus, RouteAdmitter
from mc2p.motion_nav.route_validation import GroundCapabilityIdentity
from tests.motion_nav.test_b07_step_transition import frame
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from tests.motion_nav.test_d060_terminal_node_exact_proof import _world
from tests.motion_nav.test_d064_ground_direct_handoff import _request

OUT = ROOT / '.tmp' / 'd064-direct-performance-v2'
WARMUP = 100
SAMPLES = 1000
FILES = (
    'mc2p/motion_nav/navigation_session.py',
    'mc2p/motion_nav/planning_coordinator.py',
    'mc2p/motion_nav/route_admission.py',
    'mc2p/motion_nav/route_validation.py',
    'mc2p/motion_nav/support_surfaces.py',
    'tests/motion_nav/test_b07_step_transition.py',
    'tests/motion_nav/test_b07_surface_planning.py',
    'tests/motion_nav/test_d060_terminal_node_exact_proof.py',
    'tests/motion_nav/test_d064_ground_direct_handoff.py',
    'tests/motion_nav/test_r28_planning_retry_successor.py',
)

def run(*args: str) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()

def nearest(values: list[int], q: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]

def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

performance_path = OUT / 'performance.json'
if performance_path.exists():
    raise RuntimeError(f'refusing to overwrite {performance_path}')
command = os.environ['MC2P_D064_RECORDED_COMMAND']
source_commit = run('git', 'rev-parse', 'HEAD')
tracked_status = run('git', 'status', '--porcelain', '--untracked-files=no')
if tracked_status:
    raise RuntimeError(f'tracked source must be clean: {tracked_status}')
script_path = Path(__file__).resolve()
script_sha = sha256(script_path)
world = _world(6402, 16)
profile = ordinary_profile()
identity = GroundCapabilityIdentity.from_profile(profile)
request = _request(world, start_z=1, goal_z=7)
current = frame(world, 6402, (.5, -60.0, 1.5))
admitter = RouteAdmitter()

def sample() -> int:
    started = perf_counter_ns()
    result = admitter.admit_ground_direct(
        request, current, ground_profile=profile,
        capability_identity=identity,
    )
    duration = perf_counter_ns() - started
    if result.status is not AdmissionStatus.ACCEPTED:
        raise AssertionError(result)
    return duration

for _ in range(WARMUP):
    sample()
raw = [sample() for _ in range(SAMPLES)]
p95 = nearest(raw, .95)
maximum = max(raw)
payload = {
    'schema_version': 'mc2p.d064-direct-performance.v2',
    'source': {
        'commit': source_commit,
        'tracked_source_clean': True,
        'benchmark_script': {
            'path': str(script_path.relative_to(ROOT)).replace('\\', '/'),
            'sha256': script_sha,
            'tracked': False,
        },
        'file_git_hashes': {name: run('git', 'hash-object', name) for name in FILES},
    },
    'environment': {'python': sys.version, 'platform': platform.platform()},
    'command': command,
    'configuration': {'warmup': WARMUP, 'samples': SAMPLES},
    'raw_duration_ns': raw,
    'summary': {
        'p95_ns': p95,
        'p95_ms': p95 / 1_000_000,
        'max_ns': maximum,
        'max_ms': maximum / 1_000_000,
    },
    'gates': {
        'p95_le_2ms': p95 <= 2_000_000,
        'max_lt_8ms': maximum < 8_000_000,
    },
}
payload['passed'] = all(payload['gates'].values())
performance_path.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
    encoding='utf-8',
)
(OUT / 'COMMAND.txt').write_text(command + '\n', encoding='utf-8')
lines = []
for name in ('benchmark_direct.py', 'COMMAND.txt', 'performance.json'):
    digest = sha256(OUT / name)
    lines.append(f'{digest}  {name}')
(OUT / 'SHA256SUMS').write_text('\n'.join(lines) + '\n', encoding='ascii')
print(json.dumps({'output': str(OUT), 'source': source_commit, 'summary': payload['summary'], 'gates': payload['gates'], 'passed': payload['passed']}))
raise SystemExit(0 if payload['passed'] else 1)
