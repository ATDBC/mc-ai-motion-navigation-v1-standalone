"""E20：T2 实机 spawn 时间线里，控制线程在交接 tick 和收尾 tick 为什么到 43 ms／58 ms。

T2 的正式时间线用 `validate_probe(raw, check_performance=False)` 生成，没有检查控制线程分位数；
原始 probe.json 里交接 tick 为 43.4 ms、收尾 tick 为 58.4 ms（Windows，含 G2 量尺的 sys.setprofile 开销）。

本脚本在 Linux 上用同一个 `tests.sim.d102_prepared_motion.record_probe`（inline worker，同一夹具和坐标）：
1. 原样运行（量尺开启），列出交接／收尾 tick 的分段耗时；
2. 把量尺的 sys.setprofile 换成空操作再运行一次，得到不含量尺开销的同一 tick 耗时；
3. 计时这两个 tick 中实际在控制线程构造的对象：每类对象 `validate_frozen_value`（整图不可变遍历）的次数与耗时。
只调用项目函数并计时，不改项目文件。运行：PYTHONPATH=<项目根> python e20_t2_control_ticks.py <临时输出目录>
"""
import json, shutil, statistics, sys, threading, time, types
from collections import defaultdict
from pathlib import Path

import tests.sim.d102_prepared_motion as D
from mc2p.contracts import motion_activation as MA
from mc2p.motion_nav import prepared_motion as PM

ROOT = Path(D.__file__).resolve().parents[2]
AXES = dict(platform_width=2, start_offset=0, entry_offset=0., direction="south", backlog=0)
WINDOWS = ROOT / "evidence/motion_navigation/sound-motion-proof-d105-v1/t2/spawn-probe/raw/probe.json"


class _NoProfileSys(types.SimpleNamespace):
    """量尺模块里的 sys：只把 setprofile 变成空操作，其余照常。"""
    def __getattr__(self, name):
        return getattr(sys, name)

    def setprofile(self, function):
        return None

    def getprofile(self):
        return None


def run(out, *, meter):
    if out.exists():
        shutil.rmtree(out)
    original_sys = D.sys
    if not meter:
        D.sys = _NoProfileSys()
    timings = defaultdict(list)
    control = threading.current_thread().ident
    current_tick = {"kind": None}
    original_tick = D._ControlMeter.tick
    original_validate = MA.validate_frozen_value

    def tick(self, kind, movement_tick, **kwargs):
        current_tick["kind"] = kind
        return original_tick(self, kind, movement_tick, **kwargs)

    def validate(value):
        started = time.perf_counter_ns()
        try:
            return original_validate(value)
        finally:
            if threading.current_thread().ident == control and current_tick["kind"] in ("handoff", "closeout"):
                timings[(current_tick["kind"], type(value).__name__)].append(time.perf_counter_ns() - started)

    D._ControlMeter.tick = tick
    MA.validate_frozen_value = validate
    PM.validate_frozen_value = validate
    try:
        raw = D.record_probe(AXES, output=out, manifest=ROOT / "tests/sim/manifests/prepared-motion-proof-d102-v1.json",
                             transport="inline")
    finally:
        D.sys = original_sys
        D._ControlMeter.tick = original_tick
        MA.validate_frozen_value = original_validate
        PM.validate_frozen_value = original_validate
    return raw, timings


def show(raw, label):
    samples = raw["control_samples"]
    ordinary = [s["duration_ns"] / 1e6 for s in samples if s["kind"] == "control"]
    print(f"  {label}：status {raw['status']}；普通控制 tick 中位 {statistics.median(ordinary):.2f} ms，最大 {max(ordinary):.2f} ms")
    for s in samples:
        if s["kind"] in ("start", "handoff", "finish", "closeout"):
            phases = {k: round(v / 1e6, 2) for k, v in s["phases_ns"].items() if v >= 5e5}
            print(f"    {s['kind']:8s} tick {s['movement_tick']:2d}: {s['duration_ns'] / 1e6:7.2f} ms  分段 {phases}")


def main():
    out = Path(sys.argv[1])
    print("== 0. Windows 正式 T2 spawn probe（量尺开启，check_performance=False）")
    show(json.loads(WINDOWS.read_text(encoding="utf-8")), "Windows spawn")
    errors = D.validate_probe(json.loads(WINDOWS.read_text(encoding="utf-8")), check_performance=True)
    print(f"    若打开 check_performance：{errors}")

    print("\n== 1. Linux inline，量尺开启（与正式口径相同）")
    raw, _ = run(out / "metered", meter=True)
    show(raw, "Linux 量尺开启")

    print("\n== 2. Linux inline，量尺的 sys.setprofile 换成空操作")
    rows = []
    for index in range(5):
        raw, timings = run(out / f"plain-{index}", meter=False)
        rows.append((raw, timings))
    show(rows[0][0], "Linux 无量尺（第 1 次）")
    for kind in ("start", "handoff", "finish", "closeout"):
        values = [next(s["duration_ns"] for s in r["control_samples"] if s["kind"] == kind) / 1e6 for r, _ in rows]
        print(f"    {kind:8s} 5 次中位 {statistics.median(values):6.2f} ms（{', '.join(f'{v:.2f}' for v in values)}）")

    print("\n== 3. 交接／收尾 tick 在控制线程构造对象时的整图不可变遍历（无量尺，5 次中位）")
    keys = sorted({key for _, timings in rows for key in timings})
    for key in keys:
        totals = [sum(timings.get(key, ())) / 1e6 for _, timings in rows]
        counts = [len(timings.get(key, ())) for _, timings in rows]
        print(f"    {key[0]:8s} {key[1]:32s} 次数 {statistics.median(counts):4.0f}  合计 {statistics.median(totals):6.2f} ms")
    for kind in ("handoff", "closeout"):
        totals = [sum(sum(v) for k, v in timings.items() if k[0] == kind) / 1e6 for _, timings in rows]
        ticks = [next(s["duration_ns"] for s in r["control_samples"] if s["kind"] == kind) / 1e6 for r, _ in rows]
        print(f"    {kind}: 整图遍历合计中位 {statistics.median(totals):.2f} ms，占该 tick {statistics.median(totals) / statistics.median(ticks):.0%}"
              "（嵌套构造会重复计入外层，合计可能略高）")
    shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    main()
