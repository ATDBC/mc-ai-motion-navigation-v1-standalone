# D105 成本、量尺与 tick 归因修正验收

日期：2026-10-14。当前状态：**部分完成并停止。**

本文件只记录 [D106](../decisions/0106-repair-d105-cost-measurement-and-tick-diagnostics.md) 的新运行。D105 原 T1／T2／T3 的失败结果保持不变。

## 1. R0 基线

R0 已冻结。实施基线为 `9888776d6101480ea3c5490b971b4f81800e5f2f`，方案提交为
`399b8a26ffb1cbd86367ca631a0ae1df7064c271`，正式平台为 Windows。D105 的 T1、T2、T3
索引只按 SHA-256 引用，不复制、不改写。冻结记录位于
`evidence/motion_navigation/sound-motion-proof-d106-v1/r0/index.json`。

D105 状态继续是 T1 `FAILED／STOPPED`、T2 `PARTIAL／STOPPED`、T3
`FAILED／STOPPED`。R0 没有把方案 B、T2-3、连续两跳或零等待交接加入授权范围。

## 2. R1：T1-R

R1 **FAILED／STOPPED**。扫描器现在只计算一次完整证明摘要，公共验证入口仍会重算摘要并拒绝篡改。dataclass 字段表可以复用，但字段顺序、规范字节和摘要值没有变化。相关 43 项检查通过；D105 验证结果除源码哈希外逐项一致。

Windows M0 共 204 场、4,710 个决定。200／200 正例完成，4／4 负例分类正确，安全违规为 0。P50／P95／P99／最大值为 `0.0723／0.1252／16.5732／23.2877 ms`。P99 仍高于 15 ms，所以 R1 按预登记门槛停止。方案 B 没有实施。

证据位于 `evidence/motion_navigation/sound-motion-proof-d106-v1/r1/`。

## 3. R2：G2 量尺

R2 **PASS**。正式量尺现在只接受两种模式：`call-count` 启用 profile，只记录调用数和来源，并明确 `timing_valid=false`；`performance` 拒绝已有 profile，也不自行安装 profile，只记录显式时间段，并明确 `profile_enabled=false`。旧的混合模式只用于读取历史证据和组件兼容，不能生成新的正式量尺。

同一场景配对运行的任务结果、输入序列、最终状态、安全结果和来源释放逐项一致。新增检查 4／4、Windows matrix 36／36、全部 D102 相关检查 280／280 通过。压缩后的配对原始记录和索引位于 `evidence/motion_navigation/sound-motion-proof-d106-v1/r2/`。

## 4. R3：T2-2-R

R3 **PASS**。现有生产代码已经满足安全规则，不需要增加“动作模板最大可达包络”算法，也没有修改生产代码。新增检查证明：可信 receiver 投影与完整构造逐字段一致；6,496 格完整快照缩到 18 格实际依赖范围后，证明、物理计数和依赖一致；范围外原本已知的空气或方块都变成 UNKNOWN，并返回 `MISSING_INFORMATION`；正式依赖变化后，父侧以 `DEPENDENCY` 拒绝旧证明。新增 5／5、相关回归 81／81 通过。

Windows 诊断中，完整／局部快照分别为 6,496／18 格、1,514,939／4,753 bytes。封存核心中位数为 641.15／2.34 ms，`prepare_fixed_motion` 为 780.43／151.00 ms，可信 receiver 注册为 14.25／13.27 ms。该计时没有覆盖真实 spawn 的 IPC、序列化和首次消费，因此只说明量级，不冒充完整准备链结果。证据索引位于 `evidence/motion_navigation/sound-motion-proof-d106-v1/r3/index.json`。

## 5. R4：T3-R

R4-A 已完成。现有 trace 足以解释 D105 的失败，不需要无 sidecar 对照。第 23 个 validation 试次遗漏的是 movement tick 453；客户端、F-TICK、physics sidecar 和 V3 receipt 都保存了这一帧。B10 手工探针只摄取当前 request 的样本，把旧 request 的 `lease_exhausted` 中性 tick 453 从私有账本过滤掉，随后误报 `coast_input_unobserved_retain_landing`。正式 Runtime 会摄取整份 receipt，不受这处探针缺陷影响。

第一轮修正让手工探针摄取整份 receipt，并让 manual ordered source 在正常和异常路径都通过 `finally` 注销。第一次正式重跑随即暴露第二本账的根本问题：Runtime 已提交的旧中性请求并不存在于试次私有账本，整份摄取会正确触发 `input sample has no submitted command`。

第二轮也是最后一轮限定修正删除了私有账本、手工提交和重复 receipt 摄取。手工 executor 改用 Runtime 的正式账本，已验证命令的 `CandidateExecutionWindow` 同时交给 Runtime。相关联合回归仍为 123／123。

第二次正式 Fabric 批次得到 40／40 完整时间线，四方向各 10 次，全部为 `NEXT_MOVEMENT_TICK`；ordered source 正常注销，sidecar 已封存且丢弃为 0。F-TICK 目标已经达到。批次随后在 40 个 validation 之后的旧 B10 R4 移动入口夹具失败：东向低速带 `(0.5, 1.0)` 的准备器一次观察跨两个 movement tick 后错过窄位置与速度交集，80 tick 内没有恢复。它发生在动作求解前，与本轮账本修正无关。身体全程着地、满血、无碰撞，最后安全静止。由于“B10 不退步”门槛没有通过，R4 最终仍为 **FAILED／STOPPED**。不能只取 40／40 时间线把 R4 写成整体通过。

第一次 preflight 缺少 `JAVA_HOME`，没有启动游戏、没有产生试次，单独保存在 `r4/preflight-no-java-home/`。两次正式结果分别位于 `r4/formal-t3/` 和 `r4/formal-t3-round2/`。

无 sidecar 对照只作诊断，不能计入正式 T3 分母。

## 6. 当前边界

- 方案 B 未授权、未实现。
- T2-3 未运行，不删除既有载荷和责任对象。
- 连续两跳和零等待交接未授权、未运行。
- Linux 只作补充检查；性能与正式实机结论以 Windows 为准。
- D106 最终状态为 R1 `FAILED／STOPPED`、R2 `PASS`、R3 `PASS`、R4 `FAILED／STOPPED`。

## 7. 检查范围

- R1 相关检查 43／43；
- R2 新检查 4／4、Windows matrix 36／36、全部 D102 相关检查 280／280；
- R3 新检查 5／5、相关回归 81／81；
- R4 最终联合回归 123／123；
- 第二次正式 Fabric 批次得到 40／40 完整 F-TICK 时间线，但外层 B10 门槛失败，不能记为整体通过。

另外启动了一次完整 `tests/motion_nav`。该命令运行超过 60 分钟，主进程持续计算，但 runner 没有阶段进度、单项上限或最终汇总。按 fail-fast 规则终止，退出码为 1。该运行不计为通过，也不覆盖上面的定向结果。以后应把完整检查拆成有阶段日志和独立上限的分片，再作为正式门槛运行。
