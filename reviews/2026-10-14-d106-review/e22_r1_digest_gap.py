"""E22：R1 只算一次摘要后，Windows 首决定 16.46 ms，为什么没到上一轮估计的 V1′ 13.8 ms。

在 98a0542（D106）检出上：
1. 读 D105 T1 与 D106 R1 两份 Windows 证据的首决定分布；
2. 用同一份 D105 证明对象（同一请求、同一夹具），比较三种规范化实现的整份摘要耗时：
   a. D105 原 `_canonical`（每个值调 fields()）；
   b. D106 现行 `_canonical`（`lru_cache` 缓存字段名，但每个值仍先调 isinstance(Enum)、is_dataclass，再进 lru_cache）；
   c. 上一轮 E19 的按类型分派（一次 dict 查表决定 Enum／dataclass／序列／标量）。
   三者摘要逐字节相同（脚本断言）。
3. 拆分 D106 扫描：推演＋组装、一次整份摘要、结构检查；按 Windows 实测首决定换算各变体。
只调用项目函数并计时，不改项目文件。运行：PYTHONPATH=<D106 检出> python e22_r1_digest_gap.py
"""
import gzip, hashlib, json, math, statistics, time
from dataclasses import fields, is_dataclass, replace
from enum import Enum
from pathlib import Path

from experiments.motion_navigation.trajectory_proto import d101_continuous_probe as P
from experiments.motion_navigation.trajectory_proto import d105_continuous_probe as Q
from experiments.motion_navigation.trajectory_proto import physics as PH
from experiments.motion_navigation.trajectory_proto import sound_sparse_proof as SSP
from experiments.motion_navigation.trajectory_proto.contracts import SearchBudget
from experiments.motion_navigation.trajectory_proto.d101_input_stream import StreamStartBranch
from experiments.motion_navigation.trajectory_proto.scenarios import representative_fixture

ROOT = Path(PH.__file__).resolve().parents[3]
EVID = ROOT / "evidence/motion_navigation"


def d105_canonical(value):
    """eec26af 的原实现，逐字复制用于对照。"""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: d105_canonical(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (tuple, list)):
        return [d105_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((d105_canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    return value


_KIND = {}


def dispatch_canonical(value):
    """E19 的按类型分派：每个值只做一次 dict 查表。"""
    kind = type(value)
    names = _KIND.get(kind)
    if names is None:
        names = False if isinstance(value, Enum) else (
            tuple(f.name for f in fields(value)) if is_dataclass(value) else ())
        _KIND[kind] = names
    if names is False:
        return value.value
    if names:
        return {name: dispatch_canonical(getattr(value, name)) for name in names}
    if kind is tuple or kind is list:
        return [dispatch_canonical(item) for item in value]
    if kind is set or kind is frozenset:
        return sorted((dispatch_canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    return value


def digest_with(canonical, value):
    payload = json.dumps(canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def firsts(rel):
    rows = [json.loads(line) for line in gzip.open(EVID / rel, "rt")]
    first = sorted(r["duration_ns"] / 1e6 for r in rows if r["full_proof_calls"] == 1)
    every = sorted(r["duration_ns"] / 1e6 for r in rows)
    return first, every[math.ceil(.99 * len(every)) - 1]


def timed(fn, n=40):
    out = []
    for _ in range(n):
        t = time.perf_counter_ns(); fn(); out.append((time.perf_counter_ns() - t) / 1e6)
    return min(out), statistics.median(out)


def main():
    print("== 1. Windows 首决定")
    win = {}
    for name, rel in (("D105 T1", "sound-motion-proof-d105-v1/t1/m0/decisions.jsonl.gz"),
                      ("D106 R1", "sound-motion-proof-d106-v1/r1/m0/decisions.jsonl.gz")):
        f, p99 = firsts(rel)
        win[name] = statistics.median(f)
        print(f"  {name}: 首决定中位 {statistics.median(f):.2f} ms，最小 {f[0]:.2f}，第 2–5 大 {[round(x, 2) for x in f[-5:-1]]}，"
              f"最大 {f[-1]:.2f}；超过 15 ms 的首决定 {sum(x > 15 for x in f)}/204；P99 {p99:.4f}")
    print(f"  降幅 {win['D105 T1'] - win['D106 R1']:.2f} ms")

    fixture = representative_fixture("jump_gap_1")
    inputs = P.frozen_inputs()
    first_sequence = 999 * P._SEQUENCE_STRIDE
    preludes = tuple(tuple(replace(f, control_sequence=first_sequence - 1, evidence_id="e22-wait")
                           for f in prelude) for prelude in fixture.request.branch_preludes)
    request = replace(fixture.request, request_id="e22-request", input_ledger_id="e22-ledger",
                      branch_preludes=preludes, first_candidate_control_sequence=first_sequence,
                      budget=SearchBudget(10000, 100000, 40, 2))
    evidence = tuple(tuple(replace(row, irrevocable_inputs=(), applications=()) for row in rows)
                     for rows in P._boundary_evidence(request, inputs))

    def scan():
        return SSP.scan_sound_sparse_commitment(request, inputs, fixture.world, evidence,
                                                counter=PH.CountedPhysics(request.budget))

    proof = scan().proof
    bare = replace(proof, trajectory_hash="")
    expected = proof.trajectory_hash
    assert digest_with(d105_canonical, bare) == expected
    assert digest_with(PH._canonical, bare) == expected
    assert digest_with(dispatch_canonical, bare) == expected
    assert PH.trajectory_digest(bare) == expected

    real_digest = SSP.trajectory_digest

    def stub(value):
        return expected if type(value) is SSP.SoundSparseProof else real_digest(value)

    def core():
        SSP.trajectory_digest = stub
        try:
            return scan()
        finally:
            SSP.trajectory_digest = real_digest

    jobs = {
        "scan": scan,
        "core": core,
        "digest_d105": lambda: digest_with(d105_canonical, bare),
        "digest_d106": lambda: PH.trajectory_digest(bare),
        "digest_dispatch": lambda: digest_with(dispatch_canonical, bare),
        "canon_d106": lambda: PH._canonical(bare),
        "canon_dispatch": lambda: dispatch_canonical(bare),
        "structure": lambda: SSP._validate_sound_sparse_proof_structure(proof),
    }
    samples = {k: [] for k in jobs}
    for _ in range(40):
        for k, fn in jobs.items():
            t = time.perf_counter_ns(); fn(); samples[k].append((time.perf_counter_ns() - t) / 1e6)
    m = {k: min(v) for k, v in samples.items()}
    print("\n== 2. 同一证明的整份摘要（Linux，40 轮交错，最小值／中位数 ms；三种实现摘要逐字节相同）")
    for k in jobs:
        print(f"  {k:16s} {m[k]:6.2f} / {statistics.median(samples[k]):6.2f}")
    print(f"  D106 的 lru_cache 版本比 D105 原版快 {1 - m['digest_d106'] / m['digest_d105']:.0%}；"
          f"按类型分派版本快 {1 - m['digest_dispatch'] / m['digest_d105']:.0%}")
    print(f"  D106 扫描：总计 {m['scan']:.2f}，推演＋组装 {m['core']:.2f}，一次摘要 {m['digest_d106']:.2f}，结构检查 {m['structure']:.2f}")

    print("\n== 3. 以 D106 Windows 首决定中位为基准换算（各部分按同比例缩放）")
    # 基准用分项之和（推演＋组装、一次摘要、结构检查），避免扫描总时长单次最小值的抖动。
    current = m["core"] + m["digest_d106"] + m["structure"]
    scale = win["D106 R1"] / current
    rows = (
        ("现状 D106（一次摘要，lru_cache 规范化）", current),
        ("一次摘要，按类型分派规范化", m["core"] + m["digest_dispatch"] + m["structure"]),
        ("控制线程不算内容摘要（V3）", m["core"] + m["structure"]),
    )
    for name, linux in rows:
        print(f"  {name:28s} Linux {linux:6.2f} ms → Windows 首决定约 {linux * scale:5.2f} ms")
    print(f"  （换算系数 {scale:.3f} = Windows 首决定中位 / Linux 分项之和；首决定里扫描以外的开销 <1 ms）")

    print("\n== 4. 上一轮 E19 估计与实测对照")
    print("  E19 估计：V1 16.89 ms，V1′ 13.83 ms；R1 实测 16.46 ms。")
    print("  R1 的实现等于 V1 加上一个几乎无效的缓存：缓存省掉了 fields()，但每个值仍先做 isinstance(Enum)、"
          "is_dataclass（内部 isinstance(type)+hasattr）和一次 lru_cache 调用，热点没有消失。")


if __name__ == "__main__":
    main()
