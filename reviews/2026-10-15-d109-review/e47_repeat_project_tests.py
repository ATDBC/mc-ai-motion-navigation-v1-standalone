"""E47：在 Linux 上按 D109 的完成标准重复运行项目自己的分片器测试（D3 zombie、D5 subreaper）。

  - test_timeout_kills_descendant_holding_output_pipe_with_bounded_cleanup：D3 的标准是 Linux 对抗场景
    至少连续 30 次通过（D108 审查 E43 中，a9d51a2 上 12/12 失败）；
  - test_linux_subreaper_adversarial_matrix：D5 的 Linux 动态矩阵（快速 root 退出、setsid、双 fork、
    忽略 SIGTERM 并持有输出、清理期间再 fork）。它在 Windows 上只核对场景名，Linux 才真正执行。
每次单独起一个解释器运行一个测试，统计结果，并保留首个失败的摘要。不改项目文件。
运行：python e47_repeat_project_tests.py <检出目录> <测试名> <次数> [<测试名> <次数> ...]
"""
import collections, subprocess, sys, time

PREFIX = "tests.motion_nav.test_d107_shard_runner.MotionNavShardRunnerTests."


def main():
    tree = sys.argv[1]
    pairs = list(zip(sys.argv[2::2], map(int, sys.argv[3::2])))
    head = subprocess.run(["git", "-C", tree, "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    print(f"检出 {head}")
    for name, repeats in pairs:
        outcomes, first_failure, durations = collections.Counter(), None, []
        consecutive = best = 0
        for _ in range(repeats):
            started = time.perf_counter()
            result = subprocess.run([sys.executable, "-m", "unittest", PREFIX + name], cwd=tree,
                                    capture_output=True, text=True, timeout=300)
            durations.append(time.perf_counter() - started)
            last = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "?"
            outcomes[last] += 1
            if result.returncode == 0:
                consecutive += 1
                best = max(best, consecutive)
            else:
                consecutive = 0
                if first_failure is None:
                    lines = [line for line in result.stderr.splitlines()
                             if "Error" in line or "!=" in line or "FAIL:" in line]
                    first_failure = " | ".join(lines[-4:])
        print(f"  {name}：{repeats} 次 → {dict(outcomes)}；最长连续通过 {best}；"
              f"单次用时 {min(durations):.1f}–{max(durations):.1f} s")
        if first_failure:
            print(f"    首个失败：{first_failure}")


if __name__ == "__main__":
    main()
