# D122-2 拒绝清理限定修正

基于 `ad0e4d29`，生产与测试修正提交为 `96e8f2cc`。没有进入 D122-3。

通用拒绝现在按 sequence 的实际时序成员清理。带 own default 的窗口后继若在 `stageWindowed` 前被拒绝，不会删除父 fallback；已安装 pending 的拒绝只删除 pending，已安装 fallback 的拒绝才删除该 fallback。

`red.txt` 保存 `rejectTimingForRequest` 尚不存在时的组合失败。最终相邻检查 93／93，Java mutation 17／17，Fabric overlay `compileJava --rerun-tasks` 通过。

未运行完整 Windows 分片、公开快照、性能验收、D122-3 或 Fabric。
