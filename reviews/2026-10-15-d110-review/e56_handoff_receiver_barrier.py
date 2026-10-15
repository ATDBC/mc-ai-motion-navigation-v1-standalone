"""E56：test_same_worker_solve_choice_is_frozen_before_prepare 的偶发 ValueError 从何而来。

D110 的 Linux 全量里，tests.motion_nav.test_d102_action_handoff 有 1 个
`ValueError: not enough values to unpack (expected 1, got 0)`：测试在 `decide()` 之后只等了
一次 `wait_for_receiver()`，随即要求 worker 里已经出现一个 PreparedMotionJob。

接收线程的一轮循环开始时才挑选待处理记录（mc2p/motion_nav/motion_worker.py 约 1599–1603 行）：
处于 snapshot_pending 的记录本轮只封存快照，带 request 的记录才在下一轮提交。如果 decide()
放入的记录恰好由 wait_for_receiver 请求的那一轮处理，这一轮只完成封存，提交要再等一轮。

本脚本在同一检出上比较两种做法，每次单独起解释器、单核运行：
  原样：项目测试方法本身；
  对照：同一测试，只把“等一轮”改为“最多再等 5 轮，直到出现 PreparedMotionJob”，并记录实际轮数。
运行：python e56_handoff_receiver_barrier.py <检出> [次数]
"""
import collections, os, pathlib, subprocess, sys, tempfile

TEST = ("tests.motion_nav.test_d102_action_handoff.ActionHandoffTests."
        "test_same_worker_solve_choice_is_frozen_before_prepare")

CONTROL = r'''
import sys, unittest
from tests.motion_nav.test_d102_action_handoff import ActionHandoffTests
from mc2p.motion_nav.motion_worker import GapMotionSolveJob, PreparedMotionJob, _execute_job

class Control(ActionHandoffTests):
    def test_control(self):
        self.executor._verified_motion.pop(1)
        self.observe_first(stop_airborne=True)
        self.coordinator.prepare_upcoming_action(self.current, self.physics_world, ledger_identity=self.ledger_id)
        self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        selection, = [job for job in self.worker.jobs if isinstance(job, GapMotionSolveJob)]
        self.worker.put(_execute_job(selection))
        self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
        self.coordinator.decide(self.frame(self.current), self.current, self.ledger, self.physics_world,
                                changed_cells=(), current_scope=self.coordinator.computation_scope)
        cycles = 0
        prepared = []
        while not prepared and cycles < 6:
            self.assertTrue(self.inbox.wait_for_receiver(timeout=10))
            cycles += 1
            prepared = [job for job in self.worker.jobs if isinstance(job, PreparedMotionJob)]
        print(f"CYCLES={cycles}", file=sys.stderr)
        prepared, = prepared
        self.assertEqual(prepared.request.commands, self.inbox._active[prepared.request.async_identity].request.commands)
        self.assertNotEqual(prepared.request.async_identity, selection.work_identity)

if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], "Control.test_control"])
'''


def run(tree, command):
    env = dict(os.environ, PYTHONPATH=tree)
    result = subprocess.run(command, cwd=tree, env=env, capture_output=True, text=True, timeout=300)
    last = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "?"
    cycles = [line for line in result.stderr.splitlines() if line.startswith("CYCLES=")]
    return last, cycles[0] if cycles else None


def main():
    tree = sys.argv[1]
    repeats = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    head = subprocess.run(["git", "-C", tree, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    folder = tempfile.TemporaryDirectory(prefix="e56-")  # keep the checkout untouched
    control = pathlib.Path(folder.name) / "e56_control.py"
    control.write_text(CONTROL, encoding="utf-8")
    try:
        original = collections.Counter(run(tree, [sys.executable, "-m", "unittest", TEST])[0] for _ in range(repeats))
        outcomes, cycles = collections.Counter(), collections.Counter()
        for _ in range(repeats):
            last, used = run(tree, [sys.executable, str(control)])
            outcomes[last] += 1
            cycles[used] += 1
    finally:
        folder.cleanup()
    print(f"检出 {head}，每组 {repeats} 次")
    print(f"  原样（decide 后只等一轮）：{dict(original)}")
    print(f"  对照（最多再等 5 轮）：{dict(outcomes)}；出现 PreparedMotionJob 时已等的轮数 {dict(cycles)}")


if __name__ == "__main__":
    main()
