"""E43：Linux 全量中 test_d107_shard_runner 的唯一失败——被杀的孤儿孙进程在 PID 1 收养前是僵尸，
分片器把它算作“仍存活”，之后存活列表清空了，cleanup.status 却停在 failed。

步骤：
  1. 测本容器 PID 1 收养孤儿僵尸的延迟（孤儿退出后 /proc 里还看得到它多久）；
  2. 用项目测试本身（不改测试）在给定检出上重复运行 R 次，统计通过与失败，记录首个失败摘要；
  3. 同一测试在“只把僵尸视为已停止、并在最终存活检查后重算 status”的对照检出上重复运行。
对照改动见 e43_zombie_cleanup_proposal.diff（2 个文件、11 行），只用于本实验，不提交到项目。
运行：python e43_shard_cleanup_zombie.py <项目检出> <对照检出> [重复次数]
"""
import os, subprocess, sys, time

import psutil

TEST = ("tests.motion_nav.test_d107_shard_runner.MotionNavShardRunnerTests."
        "test_timeout_kills_descendant_holding_output_pipe_with_bounded_cleanup")


def reaper_latency(samples=5):
    lingering = []
    for _ in range(samples):
        parent = subprocess.Popen([sys.executable, "-c",
                                   "import os, time\npid = os.fork()\n"
                                   "if pid == 0:\n    time.sleep(0.2)\n    os._exit(0)\n"
                                   "print(pid, flush=True)\nos._exit(0)\n"],
                                  stdout=subprocess.PIPE, text=True)
        pid = int(parent.stdout.readline())
        parent.wait()
        time.sleep(0.25)
        started = time.perf_counter()
        while psutil.pid_exists(pid):
            time.sleep(0.005)
        lingering.append(time.perf_counter() - started)
    return lingering


def run_test(tree, repeats):
    outcomes, first_failure = {}, None
    for _ in range(repeats):
        result = subprocess.run([sys.executable, "-m", "unittest", TEST], cwd=tree,
                                capture_output=True, text=True, timeout=120)
        last = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "?"
        outcomes[last] = outcomes.get(last, 0) + 1
        if result.returncode != 0 and first_failure is None:
            lines = [line for line in result.stderr.splitlines() if "Error" in line or "!=" in line]
            first_failure = " | ".join(lines[-3:])
    return outcomes, first_failure


def main():
    project, control = sys.argv[1], sys.argv[2]
    repeats = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    init = psutil.Process(1)
    lingering = reaper_latency()
    print(f"PID 1 = {init.name()}；孤儿退出后僵尸残留 {', '.join(f'{value:.2f}' for value in lingering)} s")
    for label, tree in (("项目检出", project), ("对照检出（僵尸视为已停止 + 重算 status）", control)):
        head = subprocess.run(["git", "-C", tree, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", tree, "diff", "--stat"], capture_output=True, text=True).stdout.strip()
        outcomes, failure = run_test(tree, repeats)
        print(f"  {label} {head}{'（工作区有对照改动）' if dirty else ''}：{repeats} 次 → {outcomes}")
        if failure:
            print(f"    首个失败：{failure}")


if __name__ == "__main__":
    main()
