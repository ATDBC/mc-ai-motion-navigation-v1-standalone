"""E30：D107 在测试里加入的 receiver 屏障（`wait_for_receiver`）替代了什么。

D107 让若干测试在每个模拟 movement tick 后显式等待后台 receiver 完成一轮。它把“结果在下一次
决策前已被 receiver 收进 inbox”变成了测试前提。本实验在进程内替换这个屏障，运行同一测试模块：
  - barrier：原样（对照）；
  - none：立即返回 True，不等待（完全依赖线程调度）；
  - sleep50：等待 50 ms 后返回 True，不做握手（相当于真实 20 Hz 下每 tick 留给 receiver 的墙钟）。
若 none 失败而 sleep50 通过，说明这些测试依赖的是“receiver 在一个真实 tick 内完成一轮”这个
时序前提——生产代码里 receiver 每 10 ms 自行醒来一次，前提在 20 Hz 下通常成立，但没有被生产
代码强制，也没有实机计数。
只调用项目函数和项目测试，不改项目文件。
运行：PYTHONPATH=<检出> python e30_receiver_barrier.py <mode> <测试模块名>
"""
import io, sys, time, unittest

from mc2p.motion_nav.motion_worker import MotionResultInbox


def main():
    mode, module = sys.argv[1], sys.argv[2]
    if mode == "none":
        MotionResultInbox.wait_for_receiver = lambda self, *, timeout=5.: self._receiver is not None
    elif mode == "sleep50":
        def paced(self, *, timeout=5.):
            time.sleep(.05)
            return self._receiver is not None
        MotionResultInbox.wait_for_receiver = paced
    elif mode != "barrier":
        raise SystemExit(f"unknown mode {mode}")

    stream = io.StringIO()
    started = time.perf_counter()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(
        unittest.defaultTestLoader.loadTestsFromName(f"tests.motion_nav.{module}"))
    first = ""
    for _, trace in (result.failures + result.errors)[:1]:
        lines = [line for line in trace.strip().splitlines() if line.strip()]
        first = lines[-1][:150] if lines else ""
    print(f"{mode:<8} {module:<42} ran={result.testsRun:<3} fail={len(result.failures)} err={len(result.errors)} "
          f"{time.perf_counter() - started:6.1f}s {first}")


# spawn 子进程会以 __mp_main__ 重新导入本脚本；必须加入口保护，否则子进程会再次运行测试。
if __name__ == "__main__":
    main()
