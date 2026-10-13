"""E15：D102 生产准备链对 jump_gap_1:A3 的各段成本。

调用项目 07fa1d7 的生产函数：prepare_fixed_motion（worker 侧）、seal_bundle、
receiver 的 _validate_for_registration 及其组成部分、validate_sealed_snapshot，
以及 fallback／closeout 现场调用的 verified_ground_rollout。夹具沿用
tests/motion_nav/test_d102_prepared_proof.py 的 sealed_fixture／fixed_request。
两种快照：夹具全窗口（6,496 格）和只含证明依赖包围盒外扩 1 格的局部快照。
只读、只计时，不改项目文件，不用 profiler 计时。Linux 墙钟只看比例；
同时测 scan_commitment，用 D101 M0 的 Windows 实测（初始证明决定 P50 48.35 ms）
给出一个粗略平台比例。
运行：PYTHONPATH=<项目根>:<项目根>/tests/motion_nav python e15_pipeline_cost.py
"""
import hashlib, json, pickle, statistics, time
from pathlib import Path
from types import SimpleNamespace

from experiments.motion_navigation.trajectory_proto import m0_probe
from experiments.motion_navigation.trajectory_proto.commitment import ScanStatus, scan_commitment
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence
from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.motion_activation import require_immutable_graph
from mc2p.motion_nav.physics_types import TickInput
from mc2p.motion_nav.prepared_motion import PreparedCurrentFacts
from mc2p.motion_nav import prepared_motion_proof as P
from mc2p.motion_nav import prepared_motion_validation as V
from mc2p.motion_nav.world_snapshot import validate_sealed_snapshot
from mc2p.motion_nav.safe_ground_control import verified_ground_rollout
from test_d102_prepared_proof import sealed_fixture, fixed_request

ROOT = Path(m0_probe.__file__).resolve().parents[3]


def timed(fn, n):
    out, value = [], None
    for _ in range(n):
        t = time.perf_counter_ns(); value = fn(); out.append((time.perf_counter_ns() - t) / 1e6)
    return statistics.median(out), max(out), value


fixture = m0_probe._fixture_for("jump_gap_1", "A3")
frozen = json.loads((ROOT / "experiments/motion_navigation/trajectory_proto/data/d097a-set-a-sample.json").read_text())
inputs = tuple(TickInput(*row) for row in frozen["inputs"]["jump_gap_1:A3"])
evidence = _boundary_evidence(fixture.request, inputs)
med, _, reference = timed(lambda: scan_commitment(fixture.request, inputs, fixture.world, evidence), 10)
assert reference.status is ScanStatus.VERIFIED_CANDIDATE
ratio = 48.3461 / med
print(f"scan_commitment (D101 M0 initial proof, two branches, every boundary): "
      f"{reference.counts.physics_steps} steps, Linux median {med:.1f} ms; "
      f"D101 M0 Windows P50 48.35 ms -> Windows/Linux ~ {ratio:.2f}")

deps = [p for p, _ in reference.proof.dependency_facts]
lo = [min(p[i] for p in deps) - 1 for i in range(3)]
hi = [max(p[i] for p in deps) + 1 for i in range(3)]
full_cells = list(fixture.world._world._snapshot_facts)
outside = [p for p in full_cells if not all(lo[i] <= p[i] <= hi[i] for i in range(3))]
snapshots = {"full fixture window": sealed_fixture(fixture),
             "local (dependency bbox +1)": sealed_fixture(fixture, omissions=outside)}

for label, snapshot in snapshots.items():
    print(f"\n== snapshot: {label}: {len(snapshot.facts)} cells, {len(snapshot.canonical_bytes)} canonical bytes")
    request = fixed_request(fixture, inputs, reference, snapshot)
    m, x, _ = timed(lambda: validate_sealed_snapshot(snapshot), 5)
    print(f"   validate_sealed_snapshot              median {m:8.1f} ms")
    m, x, result = timed(lambda: P.prepare_fixed_motion(request, snapshot), 5)
    assert result.bundle is not None, result.status
    print(f"   worker prepare_fixed_motion (total)   median {m:8.1f} ms  ({result.counts.physics_steps} physics steps)")
    bundle = result.bundle
    m, x, message = timed(lambda: P.seal_bundle(bundle), 5)
    print(f"   worker seal_bundle                    median {m:8.1f} ms  (message {len(message.payload)} bytes)")
    current = PreparedCurrentFacts(
        request.async_identity.scope, request.stream, request.stream.owner_id, request.world_session_id,
        request.ruleset, bundle.proof.dependency_versions, request.ledger_id, snapshot.identity,
        request.entry_state, request.damage_remaining_points, request.entry_state.movement_tick_id,
        request.requested_window)
    m, x, validated = timed(lambda: V._validate_for_registration(message, request, snapshot, current), 5)
    print(f"   receiver _validate_for_registration   median {m:8.1f} ms  -> {validated.admission.status.value}")
    decoded = pickle.loads(message.payload)
    for name, fn in (("sha256(payload)", lambda: hashlib.sha256(message.payload).hexdigest()),
                     ("pickle.loads", lambda: pickle.loads(message.payload)),
                     ("require_immutable_graph(bundle)", lambda: require_immutable_graph(decoded)),
                     ("_validate_values (re-run constructors)", lambda: V._validate_values(decoded)),
                     ("canonical_motion_digest(proof)", lambda: P.canonical_motion_digest(decoded.proof)),
                     ("_parent_mismatch (identity checks)", lambda: V._parent_mismatch(decoded, request))):
        m, x, _ = timed(fn, 5)
        print(f"      {name:38s} median {m:8.2f} ms")

proof = bundle.proof
main_states = sum(len(b.trajectory) for b in proof.branches)
tail_states = sum(len(t.trajectory) for b in proof.branches for t in b.safety_tails)
print(f"\nproof content: {len(proof.branches)} branches, main-trajectory states {main_states}, "
      f"{sum(len(b.safety_tails) for b in proof.branches)} safety tails holding {tail_states} states; "
      f"pickled main {len(pickle.dumps(tuple(b.trajectory for b in proof.branches)))} bytes, "
      f"tails {len(pickle.dumps(tuple(b.safety_tails for b in proof.branches)))} bytes")
for b in proof.branches:
    lengths = [len(t.trajectory) for t in b.safety_tails]
    key = sorted({0, b.last_abandonable_index, b.commitment_index, len(b.inputs)})
    print(f"   {b.start_branch.value}: tail states per boundary {lengths}; "
          f"tails only at {key} -> {sum(lengths[i] for i in key)} states")

world = fixture.world._world
anchor = fixture.request.anchor_state
frame = SimpleNamespace(session=anchor.session, body=SimpleNamespace(position=anchor.position), world=world)
print()
for label, kwargs in (("closeout/fallback (0 + 8 ticks)", dict(control_ticks=0, tail_ticks=8, minimum_support=.01)),
                      ("neutral check (1 + 0)", dict(control_ticks=1, tail_ticks=0, minimum_support=1e-4)),
                      ("settle (1 + 8)", dict(control_ticks=1, tail_ticks=8, minimum_support=1e-4))):
    m, x, out = timed(lambda: verified_ground_rollout(frame, anchor, MovementV1(), **kwargs), 30)
    print(f"verified_ground_rollout {label:32s} Linux median {m:.2f} ms max {x:.2f} ms "
          f"(Windows ~{m * ratio:.2f} ms)  result={'state' if out else None}")
