"""E19：T1 首决定 23.7 ms 到底花在哪里。

1. 读三份 Windows 正式证据（D101 M0、D104 无效 K2 诊断、D105 T1 M0）的首决定耗时和物理步；
2. 在 Linux 上用同一项目函数重跑 D101 旧扫描器和 D105 方案 A 的 `_run_one`，得到首决定耗时，
   用来求 Windows/Linux 比例；
3. 不插桩地拆开一次 D105 扫描：主轨迹＋尾迹推演、整份证明的规范 JSON＋SHA-256 摘要、
   `validate_sound_sparse_proof`（其中又算一次整份摘要）；旧扫描器同样拆分；
4. 几种“只改证明身份的计算方式、不改证明内容”的假设变体，估算首决定耗时：
   V1 生成后只算一次整份摘要（不在同一调用里复算）；V2 摘要只覆盖紧凑证明（不含每个边界的逐 tick 尾迹）；
   V3 控制线程不算内容摘要（交给 worker 或首 tick 之后）。
5. 方案 B 的上限：用上一轮 E17 的保守停车包络（`sparse_scan(use_certificate=True)`）在同一工作量上
   数物理步、测推演耗时，再分别加上现状和 V1–V3 的摘要成本，看方案 B 单独能否过 15 ms。
只调用项目函数并计时，不改项目文件。运行：PYTHONPATH=<项目根> python e19_t1_cost_split.py
"""
import gzip, importlib.util, json, math, statistics, sys, time
from dataclasses import replace
from pathlib import Path

import hashlib
from dataclasses import fields, is_dataclass
from enum import Enum

from experiments.motion_navigation.trajectory_proto import d101_continuous_probe as P
from experiments.motion_navigation.trajectory_proto import d105_continuous_probe as Q
from experiments.motion_navigation.trajectory_proto import sound_sparse_proof as SSP
from experiments.motion_navigation.trajectory_proto import commitment as C
from experiments.motion_navigation.trajectory_proto.contracts import SearchBudget
from experiments.motion_navigation.trajectory_proto.d101_input_stream import StreamStartBranch
from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics, _canonical, trajectory_digest
from experiments.motion_navigation.trajectory_proto.scenarios import representative_fixture

ROOT = Path(C.__file__).resolve().parents[3]
EVID = ROOT / "evidence/motion_navigation"


_FIELD_NAMES = {}


def fast_canonical(value):
    """与项目 `_canonical` 输出相同，只是按类型缓存字段表，少做 is_dataclass／fields 反射。"""
    kind = type(value)
    names = _FIELD_NAMES.get(kind)
    if names is None:
        names = False if isinstance(value, Enum) else (
            tuple(f.name for f in fields(value)) if is_dataclass(value) else ())
        _FIELD_NAMES[kind] = names
    if names is False:
        return value.value
    if names:
        return {name: fast_canonical(getattr(value, name)) for name in names}
    if kind is tuple or kind is list:
        return [fast_canonical(item) for item in value]
    if kind is set or kind is frozenset:
        return sorted((fast_canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    return value


def fast_digest(value):
    payload = json.dumps(fast_canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def nearest(values, p):
    s = sorted(values)
    return s[math.ceil(p / 100 * len(s)) - 1]


def windows_firsts(path):
    rows = [json.loads(line) for line in gzip.open(path, "rt")]
    first = [r for r in rows if r["full_proof_calls"] == 1]
    other = [r for r in rows if r["full_proof_calls"] != 1]
    return ([r["duration_ns"] / 1e6 for r in first], [r["physics_steps"] for r in first],
            [r["duration_ns"] / 1e6 for r in other], len(rows))


def ms(fn, n):
    out = []
    for _ in range(n):
        t = time.perf_counter_ns(); fn(); out.append((time.perf_counter_ns() - t) / 1e6)
    return statistics.median(out)


def setup():
    """Rebuild the exact request/evidence _run_one hands to the scanner (serial fixed)."""
    fixture = representative_fixture("jump_gap_1")
    inputs = P.frozen_inputs()
    first_sequence = 999 * P._SEQUENCE_STRIDE
    preludes = tuple(tuple(replace(f, control_sequence=first_sequence - 1, evidence_id="e19-wait")
                           for f in prelude) for prelude in fixture.request.branch_preludes)
    request = replace(fixture.request, request_id="e19-request", input_ledger_id="e19-ledger",
                      branch_preludes=preludes, first_candidate_control_sequence=first_sequence,
                      budget=SearchBudget(10000, 100000, 40, 2))
    evidence = tuple(tuple(replace(row, irrevocable_inputs=(), applications=()) for row in rows)
                     for rows in P._boundary_evidence(request, inputs))
    return fixture, inputs, request, evidence


def main():
    print("== 1. Windows 正式证据：首决定（含完整证明）")
    win = {}
    for name, rel in (("D101 M0 旧扫描器", "continuous-motion-d101-v1/m0/decisions.jsonl.gz"),
                      ("D104 K2 无效候选诊断", "key-boundary-motion-proof-d104-v1/diagnostic-k2-invalidated/decisions.jsonl.gz"),
                      ("D105 T1 方案 A", "sound-motion-proof-d105-v1/t1/m0/decisions.jsonl.gz")):
        f, steps, o, n = windows_firsts(EVID / rel)
        win[name] = statistics.median(f)
        print(f"  {name:20s} 决定 {n}，首决定 {len(f)} 个：中位 {statistics.median(f):.2f} ms，"
              f"最小 {min(f):.2f}，最大 {max(f):.2f}；物理步中位 {statistics.median(steps)}；"
              f"非首决定最大 {max(o):.3f} ms；全体 P99 {nearest(f + o, 99):.4f} ms")

    print("\n== 2. Linux 重跑同一 _run_one（每支 30 轮）")
    lin = {}
    for name, fn in (("D101 M0 旧扫描器", P.run_single_jump_probe), ("D105 T1 方案 A", Q.run_sound_single_jump_probe)):
        firsts = []
        for branch in StreamStartBranch:
            firsts += [r.decisions[0].duration_ns / 1e6 for r in fn(P.SingleJumpProbeSpec(branch, 30))]
        lin[name] = statistics.median(firsts)
        print(f"  {name:20s} 首决定中位 {lin[name]:.2f} ms；Windows/Linux = {win[name] / lin[name]:.3f}")
    ratio = win["D105 T1 方案 A"] / lin["D105 T1 方案 A"]

    print("\n== 3. 不插桩拆分一次扫描（Linux，交错测 40 轮，分解取最小值）")
    fixture, inputs, request, evidence = setup()

    def d105_scan():
        return SSP.scan_sound_sparse_commitment(request, inputs, fixture.world, evidence,
                                                counter=CountedPhysics(request.budget))

    def old_scan():
        return C.scan_commitment(request, inputs, fixture.world, evidence,
                                 counter=CountedPhysics(request.budget))

    new = d105_scan(); old = old_scan()
    assert new.status.name == "VERIFIED_CANDIDATE" and old.status.name == "VERIFIED_CANDIDATE"
    new_proof, old_proof = new.proof, old.proof
    real_digest = SSP.trajectory_digest
    real_validate = SSP.validate_sound_sparse_proof

    def proof_digest_stub(value):
        # 只把“整份证明”的摘要换成常数；紧凑尾迹摘要仍用真实函数。
        if type(value) is SSP.SoundSparseProof:
            return new_proof.trajectory_hash
        return real_digest(value)

    def core_only():
        SSP.trajectory_digest = proof_digest_stub
        SSP.validate_sound_sparse_proof = lambda proof: None
        try:
            return d105_scan()
        finally:
            SSP.trajectory_digest, SSP.validate_sound_sparse_proof = real_digest, real_validate

    def validate_struct():
        SSP.trajectory_digest = proof_digest_stub
        try:
            real_validate(new_proof)
        finally:
            SSP.trajectory_digest = real_digest

    compact = replace(new_proof, trajectory_hash="", branches=tuple(
        replace(b, boundary_decisions=tuple(replace(d, tail=None) for d in b.boundary_decisions))
        for b in new_proof.branches))
    jobs = {
        "d105_scan": d105_scan,
        "d105_core": core_only,
        "proof_digest": lambda: real_digest(replace(new_proof, trajectory_hash="")),
        "validate_full": lambda: real_validate(new_proof),
        "validate_struct": validate_struct,
        "compact_digest": lambda: real_digest(compact),
        "fast_digest": lambda: fast_digest(replace(new_proof, trajectory_hash="")),
        "canonical_only": lambda: _canonical(replace(new_proof, trajectory_hash="")),
        "old_scan": old_scan,
        "old_digest": lambda: trajectory_digest(replace(old_proof, trajectory_hash="")),
        "request_digest": lambda: trajectory_digest(request),
    }
    samples = {k: [] for k in jobs}
    for _ in range(40):
        for k, fn in jobs.items():
            t = time.perf_counter_ns(); fn(); samples[k].append((time.perf_counter_ns() - t) / 1e6)
    # 共享云主机抖动较大：分解用 40 轮最小值（最接近纯计算成本），同时列出中位数。
    m = {k: min(v) for k, v in samples.items()}
    med = {k: statistics.median(v) for k, v in samples.items()}
    print("  组件（最小值／中位数，ms）：" + "，".join(f"{k} {m[k]:.2f}/{med[k]:.2f}" for k in jobs))
    assert core_only().status.name == "VERIFIED_CANDIDATE"
    assert fast_digest(replace(new_proof, trajectory_hash="")) == new_proof.trajectory_hash
    size = lambda v: len(json.dumps(_canonical(v), sort_keys=True, separators=(",", ":")))
    print(f"  D105 扫描总计 {m['d105_scan']:.2f} ms；物理步 {new.counts.physics_steps}")
    print(f"    推演＋组装（不含整份证明摘要和复验） {m['d105_core']:.2f} ms")
    print(f"    整份证明摘要（规范 JSON {size(replace(new_proof, trajectory_hash='')):,} 字节＋SHA-256）一次 {m['proof_digest']:.2f} ms；扫描里算两次")
    print(f"    validate_sound_sparse_proof 全部 {m['validate_full']:.2f} ms，其中结构与紧凑尾迹检查 {m['validate_struct']:.2f} ms")
    share = (m['d105_scan'] - m['d105_core']) / m['d105_scan']
    print(f"    摘要与复验合计约占扫描 {share:.0%}；推演每物理步约 {m['d105_core'] / new.counts.physics_steps * 1000:.0f} µs")
    print(f"    其中 Python 递归规范化 `_canonical` 占 {m['canonical_only']:.2f} ms；"
          f"按类型缓存字段表、输出逐字节相同的规范化后整份摘要 {m['fast_digest']:.2f} ms（摘要值已核对相同）")
    print(f"  旧扫描总计 {m['old_scan']:.2f} ms；物理步 {old.counts.physics_steps}；整份证明摘要（{size(replace(old_proof, trajectory_hash='')):,} 字节）{m['old_digest']:.2f} ms，"
          f"推演＋组装约 {m['old_scan'] - m['old_digest']:.2f} ms（每步约 {(m['old_scan'] - m['old_digest']) / old.counts.physics_steps * 1000:.0f} µs）")
    print(f"  紧凑证明（去掉边界决定里的逐 tick 尾迹）摘要 {m['compact_digest']:.2f} ms（{size(compact):,} 字节）")
    print(f"  _run_one 中 request 摘要 {m['request_digest']:.2f} ms")

    print("\n== 4. 假设变体的首决定估计（只换身份摘要的计算方式，不改证明内容）")
    base = m["d105_core"] + m["validate_struct"]
    current = m["d105_scan"]
    variants = (
        ("现状：整份摘要算两次", current),
        ("V1 只算一次整份摘要", base + m["proof_digest"]),
        ("V1′ 只算一次，同值快速规范化", base + m["fast_digest"]),
        ("V2 只摘要紧凑证明", base + m["compact_digest"]),
        ("V3 控制线程不算内容摘要", base),
    )
    windows_now = win["D105 T1 方案 A"]
    for name, linux in variants:
        print(f"  {name:22s} Linux 扫描 {linux:6.2f} ms，占现状 {linux / current:5.1%} → Windows 首决定约 {windows_now * linux / current:5.2f} ms")
    print(f"  （按“各部分在两平台同比例缩放”换算，以 Windows 实测首决定中位 {windows_now:.2f} ms 为基准；"
          f"首决定里扫描以外的开销 <1 ms，P99 由 204 个首决定决定，几乎等于首决定中位）")

    print("\n== 5. 方案 B（E17 保守停车包络）在同一工作量上的上限")
    spec = importlib.util.spec_from_file_location(
        "e17", Path(__file__).resolve().parents[1] / "2026-10-13-d104-k1-review" / "e17_sound_sparse_scan.py")
    e17 = importlib.util.module_from_spec(spec); sys.modules["e17"] = e17; spec.loader.exec_module(e17)
    rows = {}
    for name, cert in (("A", False), ("B", True)):
        intervals, counter, stats = e17.sparse_scan(request, inputs, fixture.world, evidence, use_certificate=cert)
        t = min(ms(lambda: e17.sparse_scan(request, inputs, fixture.world, evidence, use_certificate=cert), 1)
                for _ in range(30))
        rows[name] = t
        print(f"  E17 模式 {name}：物理步 {counter.physics_steps}，推演 {t:.2f} ms，风险区间 {intervals}，统计 {stats}")
    scale = windows_now / current
    core_b = m["d105_core"] * rows["B"] / rows["A"]
    for name, digest in (("现状两次整份摘要", 2 * m["proof_digest"]), ("V1 一次整份摘要", m["proof_digest"]),
                         ("V2 紧凑摘要", m["compact_digest"]), ("V3 不在控制线程摘要", 0.0)):
        linux = core_b + m["validate_struct"] + digest
        print(f"  方案 B＋{name:10s}：Linux 约 {linux:6.2f} ms → Windows 首决定约 {linux * scale:5.2f} ms")
    print(f"  （方案 B 推演按 E17 的 B/A 耗时比 {rows['B'] / rows['A']:.2f} 缩放 D105 推演＋组装；"
          f"B 被包络证明的边界不保存可执行尾迹，真实实现还要补这部分，所以这是下界）")

if __name__ == "__main__":
    main()
