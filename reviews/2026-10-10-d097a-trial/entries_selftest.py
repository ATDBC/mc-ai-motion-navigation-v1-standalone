"""entries.py 自检：每个模板、每个分箱的中心与角点都能构造合法请求；
并用项目 m0_probe 的转向枚举（只读，monkeypatch 夹具）看每个模板中心入口是否可解。
这不是 M1 的 oracle，只用来确认模板本身没有写错。
运行：PYTHONPATH=<be38685 根目录>:<本目录> python entries_selftest.py
"""
import time

from experiments.motion_navigation.trajectory_proto import m0_probe
from experiments.motion_navigation.trajectory_proto.scenarios import RepresentativeFixture

import entries as E

invalid = valid = 0
for template_id in E.TEMPLATES:
    for entry_bin in E.entry_bins():
        for spec in E.bin_corner_points(template_id, entry_bin):
            try:
                request, world = E.make_request(spec)
                valid += 1
            except E.EntryInvalid as error:
                invalid += 1
                print("invalid", spec, error)
print(f"corner/centre entries: valid={valid} invalid={invalid}")

current = {}
original = m0_probe._fixture_for


def fixture_for(scenario_id, tier_id):
    if scenario_id == "trial":
        request, world = E.make_request(current["spec"])
        return RepresentativeFixture("trial", request, world, "d097a selftest")
    return original(scenario_id, tier_id)


m0_probe._fixture_for = fixture_for
for template_id in E.TEMPLATES:
    for bin_id in ("rest-C", "walk-C-s", "sprint-C-s"):
        entry_bin = next(b for b in E.entry_bins() if b.bin_id == bin_id)
        spec = E.bin_corner_points(template_id, entry_bin)[0]
        current["spec"] = spec
        started = time.perf_counter()
        result = m0_probe.run_case("trial", None, options=m0_probe.FORMAL_OPTIONS)
        anchor = E.make_request(spec)[0].anchor_state
        speed = 20 * (anchor.velocity_blocks_per_tick[0] ** 2 + anchor.velocity_blocks_per_tick[2] ** 2) ** .5
        print(f"{template_id:7s} {bin_id:11s} entry z={anchor.position[2]:.3f} x={anchor.position[0]:.3f} "
              f"v={speed:.2f}b/s -> {result.status.value:17s} steps={result.physics_steps:6d} "
              f"ticks={len(result.inputs):2d} {1000 * (time.perf_counter() - started):.0f} ms", flush=True)
