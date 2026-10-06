# D064 direct query 性能证据

这份证据来自干净的 tracked source 提交 `546e6e668d30ab22fa84df57d92fe3630dbf6f7b`。它在同一正式几何 fixture 上预热 100 次，再记录 1000 次 `RouteAdmitter.admit_ground_direct()`。

- P95：1.6753 ms，门槛不高于 2 ms；
- 最大值：1.6999 ms，门槛低于 8 ms；
- 结果：通过。

`performance.json` 保留 1,000 个原始纳秒样本、运行环境、命令、tracked source Git 哈希和 fixture Git 哈希。`benchmark_direct.py` 是运行时使用的未跟踪基准脚本副本；其 SHA-256 同时写入 `performance.json` 和 `SHA256SUMS`，不能把 tracked source clean 误读为该脚本当时已纳入 Git。
