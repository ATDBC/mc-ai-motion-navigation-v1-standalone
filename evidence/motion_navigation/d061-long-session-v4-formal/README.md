# D061 长 Session v4 正式证据

分类：`passed`。原始目录：`artifacts/d061-long-session-v4-formal`。

v4 用同一个 `perf_counter_ns` 记录每帧正式控制路径的开始和结束，按 50 ms 周期直接计算余量。控制路径统计和 deadline 门槛只使用丢弃前 100 帧后的 4,096 帧。命令由标准 conda 外层入口原样记录。

本目录保留原始性能、GC、命令、trace 完成标记和分段清单。完整 trace 分段约 160 MiB，只在本地 artifact 中保留，不进入 Git 或 standalone。`TRACE-MANIFEST.jsonl` 保存每个原始分段的 SHA-256；`ARTIFACT-SHA256SUMS` 是原始目录运行时生成的完整文件哈希。
