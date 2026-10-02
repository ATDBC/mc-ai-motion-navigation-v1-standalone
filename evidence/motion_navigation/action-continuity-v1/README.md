# 动作衔接限定试点证据

2026-10-02。当前只验证普通完整草方块、同高一格跨隙、非格心起点和落地直行。它不关闭整个 R28，也不是任意抖动安全的证明。

- `formal.json.gz` 保存 11 个正式 Runtime 调用链的计算器闭环。正常与交付正例七场均完成；另有首条移动失选、必需视角失选、空中取消和落点变化四种注入。故障场景的安全失败和取消不是到达成功；原 `result.verdict` 与 `outcome` 保持实际值。
- `benchmark.json` 保存 60 次接纳、一次冷启动和 20 次暖态后台往返的原始计时；计算调用计数另跑，不污染计时。只是一条试点工作，不是全控制帧。
- `fixed-matrix-summary.json` 保留原 14 项冻结判定：13 项任务成功、一项预设有界安全失败。不能称为 14 项任务全部完成。
- `fabric-preflight-missing-world.tar.gz` 原批次缺少坑内恢复事实，未起跳且安全结束。原始观察可以重现 `(0,98,3)` 缺信息。
- `fabric-preflight-diagnostic.tar.gz` 导航完成，但返回的工具诊断漏了 reset；整个批次仍是失败。
- `fabric-four-directions.tar.gz` 是最终四方向 4/4 批次，包含未改写的原始 Runtime 观察、动作、诊断、布置命令与汇总；没有画面、普通控制台日志或世界存档。

`index.json` 保存来源、计时口径和每个压缩归档内部文件的原字节 SHA256。`SHA256SUMS.txt` 校验本目录文件。原始本地运行仍在 `artifacts/fabric-deployment/`，此前演示和 R28 v1—v4 未删除或改写。

计时使用各自同一时钟：Python 的观察就绪到提交；JVM 的观察采样开始到输入应用。起跳前速度只在来源观察与实际起跳相邻 tick 时认作精确实测。本批四次满足该条件。观察到窗口违规为零不代表已完成 10 分钟期限验收。

检查方法与结果见 [R28 验收第 12.6 节](../../../docs/motion_navigation/acceptance/navigation-coordination-convergence.md#126-本轮实施与限定试点结果)。
