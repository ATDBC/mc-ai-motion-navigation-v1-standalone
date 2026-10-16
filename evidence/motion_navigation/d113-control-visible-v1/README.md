# D113 控制可见交付证据

当前状态：**FAILED／STOPPED**。本地门禁和唯一初始定向通过，唯一正式批次在首个 10-tick 场景等待观察时失败；没有重跑。

定向证据见 `targeted-5tick/analysis.json`：两个 identity 实际 submit→first-visible 均为 5，完整链、任务完成、零伤害、来源显式释放。单场不能替代默认矩阵。

正式结果见 `formal-default/analysis.json`：baseline 20／20、2-tick 8／8、5-tick 8／8；8 个 late1 显式不支持。首个 10-tick 只记录 submit／receiver／publish，注入 `incomplete`；剩余 10-tick、never-return 和后续 R4 未运行。755 个已记录生产帧 P95／P99／最大 4.1105／4.4411／4.6834 ms，只签署已记录部分性能。最后观察在原位着地、health 20、中性输入，但该场缺少安全终态，来源仅在 Runtime close 清除。D110-SOLVE-01 保持打开。

两个 Fabric 目录中的 `invocation.json` 保存准确命令、启动提交和生产语义哈希；`raw/` 保存原始 JSON／JSONL 的精确 gzip 副本及未压缩 SHA256。完整客户端控制台、启动配置和运行目录留在本地；不发布启动凭据。异常只摘录受控类型和项目文件位置。正式部分摘要中的 `formal_acceptance_eligible=true` 表示默认入口，不表示通过；本次 `formal_acceptance_passed=false`、`matrix_complete=false`。

- 生产与最终测试提交：`f161a0c5`。生产语义自 `2ccd237a` 后未变。
- Windows 源树正序、逆序：各 191 个模块、2387 项，全通过。
- 独立公开快照正序、逆序：各 190 个模块、2363 项，全通过。
- 四组失败、错误、超时、跳过和清理失败均为 0。
- 源树 Java／真实离线构建：13／13；快照纯 Java 门禁：3／3。
- 独立快照前、后 verify 均为 2386 文件。

完整结果和命令见 `windows-regression/summary.json`、`commands.txt`。原始分片报告与日志的精确 gzip 副本位于 `windows-regression/raw/`。`.log` 原件仍留在本地；公开导出按既有规则排除 `.log`，使用可逐字节复原的 gzip 副本。

四次工作树环境中断、旧 jar 来源拒绝，以及公开快照中 Windows 线程 CPU 零采样导致的旧测试失败均单独保留。它们不计入最终全绿矩阵。只修正报告专用单测对合法零采样的假设；墙钟、工作量、输入签名和产品门槛均未放宽。

`d112-read-only-baseline.json` 只重算 D112 历史样本。D112 原始证据与 FAILED／STOPPED 结论不变。`published-production-semantic-hashes.json` 记录 D113 当前生产文件的 CRLF→LF 语义哈希。
