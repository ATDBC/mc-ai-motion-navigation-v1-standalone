# R1／R2 最终验证

## R1

最终命令和结果在唯一审查修正完成后重新采集：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment tests.motion_nav.test_trajectory_proto_reference_search -q
```

`Ran 82 tests in 41.718s / OK`。

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_b09r_physics_step tests.motion_nav.test_b09r_physics_rollout tests.motion_nav.test_b10_gap_solver tests.motion_nav.test_motion_continuation_quality -q
```

`Ran 62 tests in 16.089s / OK`。

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_standalone_export -q
```

`Ran 12 tests in 28.153s / OK`。

修改的 Python 文件执行 `py_compile` 返回0。`git diff --check` 返回0，只有 Windows 行尾提示。

Gap1 单独复核仍为 FOUND；准时与晚一 tick 两支的风险边界都为 `2／3／17`。本轮没有修改 `mc2p`、R2／R3 或正式文档。

## R2 候选检查

最终命令在动作元矩阵和 `flat_sprint` 边界修正后重新运行：

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment tests.motion_nav.test_trajectory_proto_reference_search tests.motion_nav.test_trajectory_proto_primitive_matrix -q
```

`Ran 94 tests in 81.859s / OK`。

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_b09r_physics_step tests.motion_nav.test_b09r_physics_rollout tests.motion_nav.test_b10_gap_solver tests.motion_nav.test_motion_continuation_quality -q
```

`Ran 62 tests in 15.969s / OK`。

```powershell
.\.venv\python.exe -m unittest tests.motion_nav.test_standalone_export -q
```

`Ran 12 tests in 26.073s / OK`。

八个修改或新增的 Python 文件执行 `py_compile` 返回0；`r2-matrix.json` 通过 `json.tool`；`git diff --check` 返回0，只有 Windows 行尾提示。

候选冻结矩阵为22项正例和6项负例。全部正例 FOUND；`start=4／width=3／A3` 为 `NO_TRAJECTORY_IN_BUDGET/SEARCH_EXHAUSTED`，并未耗尽 P0 节点或 physics step 预算。不同 `PYTHONHASHSEED` 下，`jump_gap_continue` 的结果哈希、证明哈希和四项计数一致。逐场计数见 `r2-matrix.json`。后续审查证明该矩阵遗漏独立 G／J／A 组合，因此这些数字只属于 `cfd36acd` 候选，不签署R2。

R2 没有运行 R3 的预热、20次墙钟采样或150／400 ms门槛。没有修改 `mc2p`、正式文档、worker、Runtime 或闭环。

## R2 终结复核

限定修正的审查反例、直接 scanner、自由搜索和三项冻结失败计数见 `r2-independent-gja-failed.json`。完整未提交diff见 `r2-independent-gja-failed.diff`。

反例转绿后，三个冻结A15正例都用尽65,536 physics step，R2门槛失败。按D096停止，不运行R3，也不对限定修正运行完整TP签署。

恢复到 `cfd36acd` 后重新运行候选现有检查：TP 为 `Ran 94 tests in 84.070s / OK`，standalone 为 `Ran 12 tests in 27.786s / OK`。恢复后 `git diff -- experiments/motion_navigation/trajectory_proto tests/motion_nav` 为空，确认没有把失败修正留在候选源码。`r2-independent-gja-failed.diff` 在恢复前通过 `git apply --reverse --check`；结构化JSON通过 `json.tool`。最终 `git diff --check`只检查本次文档与证据改动。
