"""E53：在 Linux 上重复运行 D110 改动或新增的项目测试，统计结果。

  - D5／D110-LINUX-01：test_linux_subreaper_adversarial_matrix（D109 审查 E47 中 10/10 失败）；
  - D3：test_timeout_kills_descendant_holding_output_pipe_with_bounded_cleanup（回归）；
  - D7：test_real_worker_lifecycle_matrix_releases_without_cyclic_gc（D109 审查 E50 中稳定 `6 != 5`）；
  - D110 新增的 settle 单元测试与 close 诊断测试。
每次单独起一个解释器运行一个测试，统计结果，并保留首个失败的摘要。不改项目文件。
运行：python e53_repeat_project_tests.py <检出目录> <完整测试名> <次数> [<完整测试名> <次数> ...]
"""
import collections, subprocess, sys, time


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
            result = subprocess.run([sys.executable, "-m", "unittest", name], cwd=tree,
                                    capture_output=True, text=True, timeout=600)
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
        print(f"  {name.rsplit('.', 1)[-1]}：{repeats} 次 → {dict(outcomes)}；最长连续通过 {best}；"
              f"单次用时 {min(durations):.1f}–{max(durations):.1f} s")
        if first_failure:
            print(f"    首个失败：{first_failure}")


if __name__ == "__main__":
    main()
