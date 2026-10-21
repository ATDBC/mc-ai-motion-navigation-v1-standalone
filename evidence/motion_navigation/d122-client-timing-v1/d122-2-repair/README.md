# D122-2 独立审查限定修正

基于已保留的 D122-2 提交和证据，生产与测试修正提交为 `7ded5743`。没有进入 D122-3。

本次关闭三个缺口：Java 与 Python 共用精确默认激活关系；许可撤销产生 pending 的 `window_missed`、顶层 `permission_revoked` 和父身份 `disallowed` 真实采样；极大 tick 的预算计算使用饱和结果，不抛出会话级算术异常。

`tests/fixtures/d122_default_relation_vectors.tsv` 由 Python 和 Java 动态检查共同读取。`ClientPermissionRevocationTest` 的真实 Java event/sample 经 v4 receipt decoder 进入 Python `InputApplicationLedger`，父请求与 pending 均结束为 `REJECTED`。

结果：相邻动态检查 91／91，Java mutation 15／15，Fabric overlay `compileJava --rerun-tasks` 通过。`red.txt` 保留实现前四项失败。

未运行完整 Windows 分片、公开快照、性能验收、D122-3 或 Fabric。
