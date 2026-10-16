# D109 精简证据

本目录保存 D109 D1 的 600 秒 B10 诊断。正式运行基于提交 `680d7a7654f3824e29662dcdba900b9e24313bce`，运行前工作树干净。

- `d1-current-head/` 保存正式命令、来源身份、外层结果、独立 raw trace 摘要和关键原始文件哈希。
- `d1-preflight-no-java-home/` 单列第一次环境 preflight 失败。该次运行没有启动游戏，也不构成产品结果。
- `d6-snapshot-materialization/` 保存基于干净提交 `9e7228d4d0042612fc6715d888f8db02bfce4099` 的 Windows 20 样本快照物化性能结果。每个场景保留全部 `raw_samples_ms`，可以独立重算 P50／P95／P99／最大值。
- `d8-windows-regression/` 保存干净提交 `cb06b44884e8a7641b797887f0c12183a6b1d451` 的 Windows 全量回归摘要、精确命令和本地原始报告哈希。大体积分片报告保留在 `.tmp`，不纳入 Git。该目录不包含最终 Fabric B10。
- `d8-final-b10-first-attempt/` 单独保存 D4 第二轮限定修正前的最终 B10 失败。功能矩阵完成，但 async writer 与 close diagnostics 的投影契约不一致，trace 未封存。原始大 trace 只保存在本地 artifact，公开文件保存摘要和哈希。
- `d8-final-b10-post-repair/` 保存修正后唯一一次 B10。功能、安全、来源释放和 trace 封存通过；求解最大值和 time attribution 两项既有门槛未过。原始大文件只保存在本地 artifact，公开文件保存摘要和哈希。
- `d8-post-b10-regression/` 保存最终 B10 证据提交后的 Windows 最终字节回归。大体积分片报告留在 `.tmp`，公开摘要保存完整计数和 SHA-256；该次没有再次运行 Fabric。
- `published-production-semantic-hashes.json` 保存 D109 当前正式生产和验收工具的 LF 规范化指纹。它只描述当前公开版本，不改写 D108 及更早阶段的历史指纹。
- 原始运行位于 `artifacts/fabric-deployment/20261015T012119841247Z-adc8b98e`。137 MB Runtime trace 和其他大文件不复制进 Git；`independent-summary.json` 保存它们的字节数与 SHA-256。

修正后的 600 秒运行完成全部 B10 功能和安全矩阵，ordered source 显式释放，trace 22409／22409 写入并正常封存。进程仍因求解最大值和 time attribution 两项验收检查退出，因此 D109 停止为未通过。
