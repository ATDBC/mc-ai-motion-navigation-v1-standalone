"""审查探针（既有边界，非 D094 引入）：受控下降的落点在离地前第几 tick 被移除仍能安全放弃。

运行：在源树根目录 PYTHONPATH=. python <本文件>
场景 direct_drop_2（平台边缘下降 2 格）。现有测试只覆盖离地前 4 tick 移除落点
（test_removed_landing_support_is_rechecked_four_ticks_before_departure）。
这里把移除时刻从离地前 6 tick 扫到离地当 tick，记录结果、伤害和最低高度。
这是连续轨迹原型 N4（承诺点前一 tick 落点被移除）的现行基线。
"""
from dataclasses import replace

from tests.sim.backend import Perturbations
from tests.sim.runner import run
from tests.sim.scenarios import SCENARIOS

BASE = next(s for s in SCENARIOS if s.name == "direct_drop_2")
LANDING = {(x, 61, z): None for x in (-1, 0, 1) for z in (3, 4, 5)}

clean = run(replace(BASE, name="drop_reference"))
departure = next(row["tick"] for row in clean.trace if not row["on_ground"])
print(f"clean: {clean.outcome} {clean.reason}; departure tick {departure}")
for before in range(6, -1, -1):
    tick = departure - before
    edge = next(row for row in clean.trace if row["tick"] == tick)
    result = run(replace(BASE, name=f"landing_removed_{before}_before_departure",
                         perturbations=Perturbations(world_edits={tick: dict(LANDING)}),
                         expect="failed"))
    print(f"remove at departure-{before} (tick {tick}, clean z={edge['position'][2]:.3f}): "
          f"{result.outcome:8s} {result.reason:32s} damage={result.damage:.1f} "
          f"min_y={min(row['position'][1] for row in result.trace):.2f} violations={len(result.violations)}")
