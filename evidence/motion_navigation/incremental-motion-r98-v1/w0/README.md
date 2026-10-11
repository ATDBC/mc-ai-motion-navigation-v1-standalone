# D098 W0 Windows 原样复测证据

2026-10-11，复现完整性和 T2／T3 的 10／50 ms 探索性能通过。F1 普通／承诺决定的严格 P95 为 T2 6.1356／13.6474 ms、T3 5.2729／0.0316 ms。T3 普通决定最大 243.2021 ms 的长尾完整保留，正式 8／15／30 ms 闭环仍待验证。

- `manifest.json`：项目与三方来源、环境、源码及输入哈希、判定和产物哈希。
- `T2-summary.json`、`T3-summary.json`：原计时脚本直接生成的完整统计，字节未经改写。
- `T2-samples.json.gz`、`T3-samples.json.gz`：原样本的确定性 gzip，未压缩 SHA-256 在对应 result 中。
- `T2-result.json`、`T3-result.json`：逐样本非计时字段签名、冻结记录对照、完整性与探索性能分别判定。
- `T2-execution.json`、`T3-execution.json`：原 argv、平台负载、包装器和输入前后哈希、执行状态与总时长。
- `hash-verification.json`、`verification.md`：核验结果和测试命令。
- `integrity-recheck.json`：复审后从原 summary／gzip 重算的来源、命令与原字节核验。没有重跑 T2／T3；原 execution 保留首次包装器哈希与内嵌旧审计，当前 result 为 v2 审计。

v2 审计按 `columns` 签名所有非 `ms` 字段，并实际核对三配置的完整 `(pass,cand)` 集合、full_scan 总行数及 plan/full_scan 运行次数。冻结 C 集合有 18 个同名候选，出现次数也必须相同。分类结论由这些覆盖检查、原脚本断言和冻结签名共同推导，样本没有独立分类列。

包装器仅用于明确指定的三方复测目录。原脚本仍在 `.tmp` 内生成临时明文样本，Git 只保存 gzip 和紧凑记录。项目正式 Runtime、Session 和动作链不依赖该目录。

两次 psutil Windows 负载接口的实际读数都是零，证据保留原值，不以该读数单独证明空闲。T2／T3 的分类检查覆盖计时集合中的正例；本批没有重新运行三方完整负例等价集合、正式输入账本或 Fabric。
