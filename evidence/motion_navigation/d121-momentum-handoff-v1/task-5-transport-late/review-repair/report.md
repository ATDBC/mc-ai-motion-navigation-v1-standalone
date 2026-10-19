# Task5 独立审查修正

基线为 `071d9d5c108dcdea89443ae8c8657f3426a94e69`。本次只关闭 P2、P3；Task5 仍为 **FAILED／STOPPED**，D121 仍为 **PARTIAL／STOPPED**。

P2 已修正。原文“三场 proof／world 完全相同”超出了旧检查范围。现在比较实际路线、入口、轨迹、命令、当前完整证明与 fallback、依赖格、快照事实、规则／范围、计算器世界以及原 worker 返回的 prepared 请求、证明和输入，共12组内容。L 帧另核对 `MovementV1`、单一 winner 和来源类别 `ordered`。三场这些语义内容相同，完整 action 和 `[27,27]` 窗口也相同。

运行身份、版本和相关摘要分别记录，未强行改成相同：每场的 world epoch、dependency versions、stream/request ID、job token、parent/dependency/payload digest 各自独立。语义比较只在测试侧排除这些绑定值和观察 stamp；生产证明校验仍保留全部身份与版本检查。修改实际位置、命令、方块或路线内容会改变指纹；最终 action 相同但世界语义不同也会被矩阵拒绝。

P3 已修正。新探针、测试、修改的文档和本轮新增记录全部使用 LF。`.gitattributes` 沿用仓库已有 evidence 规则，保留 Task5 历史 CRLF 原始字节并接受 CRLF 行尾。默认 `git diff --check` 和从 `c4f3538` 基线执行的默认检查均通过，无需命令级 `cr-at-eol` 覆盖。

Windows 检查结果：

- 聚焦11/11，9.811 s。
- design metrics 与 standalone export 检查59/59，34.680 s；未登记分支0，生产行数47691。
- 独立导出内诊断与metrics46/46，15.983 s；2745文件verify通过。
- 原有D120／D121的116份证据与基线Git blob逐字节相同，含全部旧Task5记录。

`matrix-final.json` 和 `semantic-comparison.json` 保存新的语义与身份对照。早期投影遗漏 frozenset 的 setup error、尚未分离 stream/request ID 与 winner UUID 的初始矩阵均保留并在 `initial-diagnostics.json` 说明；它们没有改变物理结果。

没有修改生产 Python、Java、协议、planner、Session、profile 或安全规则，没有运行 Fabric、Java 动态验收或完整发布矩阵。新增比较只读取已有 worker 返回值，不生成第二份证明，也不改变 L 帧实际输入。timely 仍在tick27成功，late仍在tick28真实补跳，never仍为身体责任未解决。
