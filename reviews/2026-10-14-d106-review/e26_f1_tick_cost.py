"""E26：F1 跟随模块为什么让完整 tests/motion_nav 超过 60 分钟。

在给定检出上运行一个缩短的 F1 场景（直线 2.0 格/秒，移动 20 tick，结束保持 20 tick），
用进程内计时包装统计：
  - 每个场景 tick 的平均墙钟时间；
  - `apply_observed_blocks`（Runtime 摄取观察时把方块写入世界模型）的总时间与次数；
  - 其中 `_commit_snapshot_sections`（D102 起每批观察提交不可变 section）的时间（若存在）；
  - 其中 `validate_frozen_value`（整图不可变遍历）的时间（若该函数存在）；
  - 每次构造 `ImmutableWorldSection` 时 section 内的事实数与耗时，用来估计“每千格”的成本。
在 5c1485d（D103 前）与 98a0542（D106）上各跑一次即可比较。
只调用项目函数并计时，不改项目文件。运行：PYTHONPATH=<检出> python e26_f1_tick_cost.py
"""
import statistics, subprocess, time
from dataclasses import replace

from tests.sim import known_world_following as K
from mc2p.motion_nav import runtime_adapter as RA
from mc2p.motion_nav import world_model as WM

spent = {"apply": [0, 0], "commit": [0, 0], "validate": [0, 0]}
sections = []


def wrap(owner, name, key):
    original = getattr(owner, name)

    def inner(*args, **kwargs):
        started = time.perf_counter_ns()
        try:
            return original(*args, **kwargs)
        finally:
            spent[key][0] += time.perf_counter_ns() - started
            spent[key][1] += 1
    setattr(owner, name, inner)


def main():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    wrap(RA, "apply_observed_blocks", "apply")
    if hasattr(WM.WorldKnowledge, "_commit_snapshot_sections"):
        wrap(WM.WorldKnowledge, "_commit_snapshot_sections", "commit")
    try:
        from mc2p.contracts import motion_activation as MA
        if hasattr(MA, "validate_frozen_value"):
            wrap(MA, "validate_frozen_value", "validate")
    except ImportError:
        pass
    if hasattr(WM, "ImmutableWorldSection"):
        original_post_init = WM.ImmutableWorldSection.__post_init__

        def timed_post_init(self):
            started = time.perf_counter_ns()
            try:
                return original_post_init(self)
            finally:
                sections.append((len(self.facts), time.perf_counter_ns() - started))
        WM.ImmutableWorldSection.__post_init__ = timed_post_init
    scenario = replace(K.SCENARIO_BY_NAME["straight_2_0"], name="e26_short", move_ticks=20, final_hold_ticks=20)
    started = time.perf_counter()
    result = K.run_scenario(scenario)
    wall = time.perf_counter() - started
    ticks = scenario.maximum_ticks
    print(f"检出 {head}：场景 {scenario.name}，上限 {ticks} tick，墙钟 {wall:.1f} s，约 {wall / ticks * 1000:.0f} ms/tick")
    print(f"  apply_observed_blocks 共 {spent['apply'][1]} 次，{spent['apply'][0] / 1e9:.1f} s（占 {spent['apply'][0] / 1e9 / wall:.0%}）")
    if spent["commit"][1]:
        print(f"  _commit_snapshot_sections 共 {spent['commit'][1]} 次，{spent['commit'][0] / 1e9:.1f} s（占 {spent['commit'][0] / 1e9 / wall:.0%}；含在 apply 内）")
    if spent["validate"][1]:
        print(f"  validate_frozen_value 共 {spent['validate'][1]} 次，{spent['validate'][0] / 1e9:.1f} s（占 {spent['validate'][0] / 1e9 / wall:.0%}；含 apply 内部）")
    else:
        print("  validate_frozen_value：该检出中不存在或未被调用")
    if sections:
        facts = sum(n for n, _ in sections)
        elapsed = sum(t for _, t in sections) / 1e6
        sizes = sorted(n for n, _ in sections)
        print(f"  ImmutableWorldSection 构造 {len(sections)} 次，section 事实数中位 {statistics.median(sizes):.0f}、最大 {sizes[-1]}；"
              f"合计 {elapsed / 1000:.1f} s，约 {elapsed / facts * 1000:.1f} ms／千格")
    print(f"  safety_violations={result.get('safety_violations')} source_released={result.get('source_released')}")


if __name__ == "__main__":
    main()
