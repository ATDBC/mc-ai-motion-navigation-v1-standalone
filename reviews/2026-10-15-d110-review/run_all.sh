#!/bin/sh
# 用法：
#   PROJECT=<origin/main f663586 检出> D109=<c2cc1ad 检出> sh run_all.sh
# 只调用项目函数、项目脚本和项目测试并计时，不改任何检出。输出写到本目录的 output/。
# E40 复用 D108 审查目录里的脚本。
# E54 必须在后台运行或在不含 "time.sleep(30)" 字样的 shell 中运行：它会强杀残留的场景进程。
# 计时类实验（E52、E55）应在机器空闲时运行，不要与全量检查并行。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
R8="$HERE/../2026-10-15-d108-review"
OUT="$HERE/output"
git -C "$PROJECT" rev-parse HEAD > "$OUT/project_head.txt"
# E51：close 诊断修复（B10 入口 _runtime_trace），D110 两种 CPU 绑定，D109 作改前对照
{
  echo "== f663586，taskset -c 1,2"; (cd "$PROJECT" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e51_close_diagnostics_fix.py" 50)
  echo "== f663586，taskset -c 1（单 CPU）"; (cd "$PROJECT" && PYTHONPATH=. taskset -c 1 python3 "$HERE/e51_close_diagnostics_fix.py" 50)
  echo "== c2cc1ad（D109），taskset -c 1,2"; (cd "$D109" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e51_close_diagnostics_fix.py" 50)
} > "$OUT/e51_close_diagnostics_fix.txt" 2>&1
# E52：求解 GC 归因（项目 _measure_gap_solve + gc.callbacks 停顿计时 + Windows 粒度量化）
(cd "$PROJECT" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e52_solver_gc_attribution.py" 200 > "$OUT/e52_solver_gc_attribution.txt" 2>&1)
# E53：D110 改动或新增的项目测试重复运行
P=tests.motion_nav.test_d107_shard_runner.MotionNavShardRunnerTests
taskset -c 1,2 python3 "$HERE/e53_repeat_project_tests.py" "$PROJECT" \
  $P.test_linux_subreaper_adversarial_matrix 20 \
  $P.test_timeout_kills_descendant_holding_output_pipe_with_bounded_cleanup 10 \
  $P.test_settle_kills_exact_registered_descendants_without_another_sigterm 10 \
  $P.test_settle_preserves_kill_access_denial_as_cleanup_failure 10 \
  tests.motion_nav.test_d102_registry_lifecycle.RegistryTests.test_real_worker_lifecycle_matrix_releases_without_cyclic_gc 10 \
  tests.test_fabric_deployment_probe.FabricDeploymentProbeTests.test_async_close_diagnostics_survives_real_runtime_backend_close 30 \
  > "$OUT/e53_repeat_project_tests.txt" 2>&1
# E54／E54b：Linux 矩阵逐子场景；fork_during_cleanup 直接交给分片器（D110 与 D109）
(cd "$(mktemp -d)" && taskset -c 1 python3 "$HERE/e54_subreaper_matrix_subtests.py" 10 "D110=$PROJECT" "D109=$D109" > "$OUT/e54_subreaper_matrix_subtests.txt" 2>&1)
taskset -c 1 python3 "$HERE/e54b_fork_during_cleanup.py" 10 "D110=$PROJECT" "D109=$D109" > "$OUT/e54b_fork_during_cleanup.txt" 2>&1
# E40：D108 审查的分片器对抗场景（回归）
taskset -c 1 python3 "$R8/e40_shard_runner_adversarial.py" "$PROJECT" > "$OUT/e40_shard_runner_adversarial.txt" 2>&1
# E55：B10 证据缓冲对完整 GC 停顿的贡献
(cd "$PROJECT" && PYTHONPATH=. taskset -c 1 python3 "$HERE/e55_evidence_buffer_gc_cost.py" > "$OUT/e55_evidence_buffer_gc_cost.txt" 2>&1)
# 项目自己的分片器，完整 tests/motion_nav（Linux 约 35 分钟，单独运行）：
#   (cd "$PROJECT" && taskset -c 3 python3 scripts/run_motion_nav_shards.py --report "$OUT/linux-forward.json")
# output/linux-forward-summary.txt 由该报告逐模块整理而来。
