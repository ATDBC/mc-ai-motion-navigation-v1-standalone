"""E28：D107 之后 F1 缩短场景的每 tick 成本，以及惰性提交把工作挪到了哪个线程。

沿用 D106 审查 E26 的缩短场景（直线 2.0 格/秒，移动 20 tick，结束保持 20 tick），
在进程内包装项目函数并按线程统计：
  - 场景墙钟与每 tick 平均；
  - `apply_observed_blocks`（控制线程摄取观察）的时间；
  - `_commit_snapshot_sections` 的次数、重建 section 数与时间，分“观察批次（带 sections 参数）”
    和“借用时补提交（无参数）”两类，并记录所在线程；
  - `borrow_snapshot_version` 的次数、时间、所在线程；
  - `validate_frozen_value` 的次数、时间、所在线程；
  - 控制线程进入 `_snapshot_version_lock` 时的等待时间（可能被接收线程的借用补提交挡住）。
只调用项目函数并计时，不改项目文件。运行：PYTHONPATH=<检出> python e28_f1_tick_cost_threads.py
"""
import collections, statistics, subprocess, threading, time
from dataclasses import replace

from tests.sim import known_world_following as K
from mc2p.motion_nav import runtime_adapter as RA
from mc2p.motion_nav import world_model as WM

MAIN = threading.main_thread().ident
stats = collections.defaultdict(lambda: [0, 0])   # key -> [ns, count]
section_builds = collections.Counter()
batch_calls = []   # (ms, rebuilt sections, batch index) for each observation-batch commit
apply_starts = []  # perf_counter_ns at each control-thread observation ingest
dirty_after_batch = []  # sections still holding unmerged evidence after each observation batch
lock_waits = []


def where():
    return "control" if threading.get_ident() == MAIN else "background"


def wrap(owner, name, key, *, classify=None):
    original = getattr(owner, name)

    def inner(*args, **kwargs):
        label = key if classify is None else classify(args, kwargs)
        label = f"{label}@{where()}"
        started = time.perf_counter_ns()
        try:
            return original(*args, **kwargs)
        finally:
            stats[label][0] += time.perf_counter_ns() - started
            stats[label][1] += 1
    setattr(owner, name, inner)


class TimedRLock:
    """Proxy for the owner's RLock that records control-thread acquire waits."""

    def __init__(self, inner):
        self._inner = inner

    def acquire(self, blocking=True, timeout=-1):
        started = time.perf_counter_ns()
        got = self._inner.acquire(blocking, timeout)
        if blocking and threading.get_ident() == MAIN:
            lock_waits.append(time.perf_counter_ns() - started)
        return got

    def release(self):
        self._inner.release()

    __enter__ = acquire

    def __exit__(self, *exc):
        self.release()


def main():
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    wrap(RA, "apply_observed_blocks", "apply_observed_blocks")
    timed_apply = RA.apply_observed_blocks

    def stamped_apply(*args, **kwargs):
        apply_starts.append(time.perf_counter_ns())
        return timed_apply(*args, **kwargs)
    RA.apply_observed_blocks = stamped_apply
    if hasattr(WM.WorldKnowledge, "borrow_snapshot_version"):
        wrap(WM.WorldKnowledge, "borrow_snapshot_version", "borrow_snapshot_version")
    if hasattr(WM.WorldKnowledge, "_commit_snapshot_sections"):
        original_commit = WM.WorldKnowledge._commit_snapshot_sections

        def counted_commit(self, *args, **kwargs):
            lazy = not args and "sections" not in kwargs
            before = len(self._snapshot_dirty_sections)
            started = time.perf_counter_ns()
            try:
                return original_commit(self, *args, **kwargs)
            finally:
                rebuilt = before - len(self._snapshot_dirty_sections)
                label = ("commit:borrow" if lazy else "commit:batch") + "@" + where()
                stats[label][0] += time.perf_counter_ns() - started
                stats[label][1] += 1
                section_builds[label] += rebuilt
                if not lazy:
                    dirty_after_batch.append(len(self._snapshot_dirty_sections))
                    batch_calls.append(((time.perf_counter_ns() - started) / 1e6, rebuilt,
                                        f"{len(batch_calls)}/摄取{len(apply_starts)}次后"))
        WM.WorldKnowledge._commit_snapshot_sections = counted_commit
        original_init = WM.WorldKnowledge.__init__

        def init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self._snapshot_version_lock = TimedRLock(self._snapshot_version_lock)
        WM.WorldKnowledge.__init__ = init
    try:
        from mc2p.contracts import motion_activation as MA
        if hasattr(MA, "validate_frozen_value"):
            wrap(MA, "validate_frozen_value", "validate_frozen_value")
    except ImportError:
        pass
    scenario = replace(K.SCENARIO_BY_NAME["straight_2_0"], name="e28_short", move_ticks=20, final_hold_ticks=20)
    started = time.perf_counter()
    result = K.run_scenario(scenario)
    wall = time.perf_counter() - started
    ticks = scenario.maximum_ticks
    print(f"检出 {head}：场景 {scenario.name}，上限 {ticks} tick，墙钟 {wall:.2f} s，约 {wall / ticks * 1000:.1f} ms/tick")
    for label in sorted(stats):
        ns, count = stats[label]
        extra = f"，重建 section {section_builds[label]} 个" if label.startswith("commit") else ""
        print(f"  {label:<38} {count:>5} 次  {ns / 1e6:>9.1f} ms（占墙钟 {ns / 1e9 / wall:.1%}）{extra}")
    if batch_calls:
        rebuilding = sorted(batch_calls)
        nonzero = [c for c in rebuilding if c[1]]
        print(f"  观察批次提交：{len(rebuilding)} 次，其中实际重建 {len(nonzero)} 次；单次最长 {rebuilding[-1][0]:.1f} ms"
              f"（重建 {rebuilding[-1][1]} 个 section），单批最多重建 {max(c[1] for c in rebuilding)} 个 section")
        print("  实际重建的批次（第几次提交: 毫秒/section 数）：" + "，".join(
            f"#{index}: {ms:.1f}/{count}" for ms, count, index in
            sorted(nonzero, key=lambda c: int(c[2].split("/")[0]))))
    if len(apply_starts) > 2:
        gaps = sorted((b - a) / 1e6 for a, b in zip(apply_starts, apply_starts[1:]))
        print(f"  相邻两次观察摄取的间隔（≈每个控制周期墙钟）：中位 {statistics.median(gaps):.1f} ms，"
              f"P95 {gaps[int(0.95 * (len(gaps) - 1))]:.1f} ms，最大 {gaps[-1]:.1f} ms，>50 ms 共 {sum(g > 50 for g in gaps)} 个")
    if dirty_after_batch:
        print(f"  每批观察后仍待合并证据的 section 数：中位 {statistics.median(dirty_after_batch):.0f}，"
              f"最大 {max(dirty_after_batch)}（下一次借用会在接收线程上持锁一次性重建它们）")
    if lock_waits:
        waits = sorted(w / 1e6 for w in lock_waits)
        print(f"  控制线程取 _snapshot_version_lock：{len(waits)} 次，中位 {statistics.median(waits):.3f} ms，"
              f"最大 {waits[-1]:.3f} ms，>1 ms 共 {sum(w > 1 for w in waits)} 次")
    print(f"  safety_violations={result.get('safety_violations')} source_released={result.get('source_released')}")


if __name__ == "__main__":
    main()
