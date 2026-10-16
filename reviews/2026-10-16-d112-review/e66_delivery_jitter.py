"""E66：交付时间抖动一 tick 时，协调器能否完成。

D112 的成功场景里，首次取用都正好领先 earliest 1 tick：准备前缀等于上一次采样到的
“提交→可见” tick 数，没有额外余量。固定延迟矩阵每次交付都一样，所以测不到“这一次比上一次晚一拍”。
这里只换项目组件夹具 `_DeliveryWorker` 的延迟来源：每个作业的延迟从 {D, D+1} 中按固定种子随机取。
其余全部是项目代码（`_gap_case`、`height_action_scenarios`、`tests.sim.runner.run`）。
失败时列出每个作业的操作（solve／revalidate）和准备前缀长度：协调器的 `_sample_solve_delivery`
只对 SOLVE 作业采样交付 tick，REVALIDATE 重试沿用第一次的采样值。
运行：PYTHONPATH=<检出> python e66_delivery_jitter.py [每格种子数]
"""
from dataclasses import replace
import collections, random, subprocess, sys

from tests.motion_nav.test_action_continuity_formal import _DeliveryWorker, _gap_case
from tests.sim.continuous_height_matrix import height_action_scenarios
from tests.sim.runner import run


class _JitterWorker(_DeliveryWorker):
    def __init__(self, base: int, seed: int) -> None:
        super().__init__(delivery_polls=base)
        self._rng = random.Random(seed)
        self._base = base
        self.delays = []

    def submit(self, job) -> bool:
        self.delivery_polls = self._base + self._rng.randint(0, 1)
        self.delays.append(self.delivery_polls)
        return super().submit(job)


def main():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    seeds = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    height = {case.name: case for case in height_action_scenarios()}
    cases = {"gap": _gap_case(), "jump_up": height["jump_up_1"], "drop": height["direct_drop_2"]}
    print(f"检出 {head}；每个作业的“提交→可见”延迟从 {{D, D+1}} 随机取，每格 {seeds} 个种子")
    for name, case in cases.items():
        for base in (2, 3, 4):
            outcomes = collections.Counter()
            failures = []
            for seed in range(seeds):
                worker = _JitterWorker(base, seed)
                result = run(replace(case, max_ticks=max(case.max_ticks, 500)), motion_factory=lambda: worker)
                outcomes[result.outcome] += 1
                if result.outcome != "success" and len(failures) < 2:
                    jobs = [f"{job.operation.value}/前缀{len(job.entry_prefix)}" for job in worker.jobs]
                    failures.append(f"种子 {seed} 延迟 {worker.delays} 作业 {jobs} → {result.reason}")
                assert result.damage == 0.0 and not result.violations, (name, base, seed, result.reason)
            print(f"  {name:<7} D={base}（{base}/{base + 1}）：{dict(outcomes)}")
            for line in failures:
                print(f"      {line}")


if __name__ == "__main__":
    main()
