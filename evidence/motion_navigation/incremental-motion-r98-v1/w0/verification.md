# W0 检查记录

包装器先写 7 个行为检查，再实现脚本。红灯为缺少 `measure_d098_trial_windows` 模块的 ImportError；实现后原 7 项通过。随后补充 T3 argv 和源码变更拒绝检查，最终 8／8。

第一轮复审补了失败场景。旧代码对“预存输出、脚本 0 退出但无新输出”未失败，也忽略新增字段和列名；不完整覆盖缺少实际检查。修正后，旧输出拒绝、配置覆盖、重复名称次数、列名与所有非计时字段检查 14／14 通过，签署于 `9a488037`。

第二轮复审发现旧 `--recheck-evidence` 会覆盖 result 并改变来源记录。先新增两项检查，旧代码因覆盖原 previous_result_sha256 和缺少一次性审计接口而失败；修正后 16／16 通过。T2／T3 的真实证据各连续只读复核两次，13 个现有证据文件的字节始终相同；对已有 v2 再次生成审计均拒绝。

```text
.venv/python.exe -m unittest tests.motion_nav.test_d098_windows_trial_wrapper -q
Ran 16 tests ... OK

.venv/python.exe -m unittest tests.motion_nav.test_trajectory_proto_m0_probe tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment tests.motion_nav.test_trajectory_proto_reference_search tests.motion_nav.test_trajectory_proto_primitive_matrix tests.motion_nav.test_standalone_export -q
Ran 113 tests ... OK
```

正式 T2／T3 原命令、环境与总时长见 execution，完整数字见 summary。没有以 smoke、minimal 或修改参数替代正式采集。

当前命令分为两种。首次生成 v2 使用 `--audit-evidence`，用于升级原 v1 结果；已有 v2 或来源记录时拒绝覆盖。下面只说明首次生成接口，本批没有对已签署的 T2／T3 执行首次生成：

```text
.venv/python.exe scripts/measure_d098_trial_windows.py T2 --trial-dir .tmp/d098-review-f5e43e7/reviews/2026-10-10-d097a-trial --output-dir <保存v1输入的新审计目录> --audit-evidence
```

日常复核使用 `--recheck-evidence`，只读取原 summary／gzip 和既有 result，重新计算后逐字段核对审计结果；result、manifest 和来源记录均不写入。当前命令如下：

```text
.venv/python.exe scripts/measure_d098_trial_windows.py T2 --trial-dir .tmp/d098-review-f5e43e7/reviews/2026-10-10-d097a-trial --output-dir evidence/motion_navigation/incremental-motion-r98-v1/w0 --recheck-evidence
.venv/python.exe scripts/measure_d098_trial_windows.py T3 --trial-dir .tmp/d098-review-f5e43e7/reviews/2026-10-10-d097a-trial --output-dir evidence/motion_navigation/incremental-motion-r98-v1/w0 --recheck-evidence
```

证据核验分别检查压缩文件字节 SHA-256、解压后 SHA-256、原 summary 字节 SHA-256、逐样本非耗时字段签名、26 个三方源码与输入文件的归档字节、冻结表哈希和项目源码相对 `54b01866` 的差异。结果见 `hash-verification.json` 与 `manifest.json`。

现有 `manifest.json`、`integrity-recheck.json`、result 与原采集文件保持 `9a488037` 签署字节。manifest 中包装器、测试及本检查说明的哈希对应那个提交；核验这些历史哈希时读取该提交的 Git blob。当前只读代码、测试与检查说明的哈希，以及重复复核结果另存于 `readonly-recheck-verification.json`。这份新记录不会替换首次 v2 的来源。

`.gitattributes` 为 W0 证据与包装器保存原字节，避免 Git 的 CRLF 转换改变已记录的哈希。暂存区的文件字节也逐项符合 manifest。

`git diff --check` 通过；`mc2p` 和既有轨迹实验源码相对采集基线没有差异。D097 与 TP 的原结论及失败未改写。本次检查仅签署 W0。
