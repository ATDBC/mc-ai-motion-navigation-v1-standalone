# 关键边界证明与连续动作实施计划

日期：2026-10-13。状态：**K0 通过；K1 完整性门槛失败，阶段停止。**

**目标：** 先把运动证明从“每个边界一条停车尾迹”缩小为关键边界证明，再简化 receiver 和局部快照，最后测清真实 tick 顺序并尝试连续两跳。

**依据：** [D104](../decisions/0104-slim-motion-proofs-before-continuity.md)、[关键边界架构](../architecture/key-boundary-motion-proof-v1.md)、[D101](../decisions/0101-clarify-short-open-loop-timing-and-lifecycle.md)。

## 全局约束

- Windows 是正式平台；Linux 只作补充复核。
- D098、D101、D102／D103 的原证据和失败不改写。
- 每项先 RED，再最小实现、定向回归和独立审查。
- 发现旁支问题只登记，不扩大当前任务。P0／P1 安全问题除外。
- 任一门槛未通过即停止，不自动进入下一项。
- 不建立第二套 worker、receiver、registry、账本或身体 owner。
- 不降低 UNKNOWN、伤害、支撑、依赖、输入窗口和身体释放条件。

## K0：冻结 oracle、量尺和范围

只修改测试、量尺和文档，不改正式运动行为。

1. 冻结 18 个 D 样本的旧逐边界扫描结果。
2. 记录每个分支的风险区间、关键节点、尾迹长度、物理步数和输入摘要。
3. 补 `jump_gap_1` 在 jump 输入前越过最后可放弃点的回归。
4. 冻结 receiver 当前耗时、bundle 字节数、快照格数和正式提交到可用的时间线。
5. 冻结 `FixedRouteController`、最长合法关键尾迹和正常 closeout 的 Windows 每 tick 物理步分布。

**通过条件：** oracle 可重复；18 个样本逐项可重算；量尺不依赖墙钟决定行为；正式代码未改。

## K1：隔离实现关键边界证明

预计修改 `experiments/motion_navigation/trajectory_proto/commitment.py`、D101 隔离量尺和对应证明测试。旧扫描器保留为测试 oracle；K1 不改 `mc2p` 正式链。

1. 主轨迹只回放一次。
2. 动态求最后可放弃、承诺和恢复边界。
3. 只在关键节点生成尾迹。
4. 尾迹删除中间状态，保留输入、端点、伤害和依赖。
5. UNKNOWN、未支持和多个风险区间保持有类型拒绝。

**通过条件：** 18 个冻结样本的风险区间与旧 oracle 一致；关键尾迹变异会被检查发现；主轨迹和输入语义不变；`jump_gap_1` 不按 jump tick 猜承诺点。

## K2：Windows 重跑 D101 M0

使用 D101 原 204 场、原 4,710 个决定和原 8／15／30 ms 门槛。不得因样本减少改分位数口径。

**通过条件：** 安全分类、输入和关键节点语义逐项一致；完整决定 P95≤8 ms、P99≤15 ms、最大值<30 ms；旧成功退步为 0。

**止损：** K2 未通过，停止。receiver、快照、F-TICK 和连续两跳不实施。

## K3：把紧凑证明迁入正式 worker

把 K1 已通过的关键边界算法迁入 `prepared_motion_proof.py` 和 `prepared_motion.py`。主轨迹继续保留；安全尾迹只保存输入、端点、伤害和依赖。正式执行器仍按主轨迹逐 tick 比较，只有使用安全尾迹时才做有界回放。

**门禁：** 正式证明与 K1 oracle 的风险区间、关键节点和分类一致；激活、首回执选分支、偏离和身体责任不退步；消息显著小于旧约 775 KiB；不修改 receiver 信任边界。

## K4：简化 trusted receiver 和 registry 载荷

生产接收路径只保留消息边界、父侧身份、当前事实、依赖版本和发布竞态。递归对象图、构造器重放、规范 proof／bundle 重算和完整物理审计移到测试。

**门禁：** generation、owner、input、ruleset、window、damage 和 dependency 任一变异都拒绝；两个旧反例仍由身份检查拦住；损坏 payload 和超限消息拒绝；candidate／executing／safe-tail 生命周期不变。

同一阶段只在紧凑证明已稳定后删除登记对象中的完整 bundle 和重复规范 bytes；不删除 candidate／executing／safe-tail 三种用途。

## K5：局部 detached 快照和正式生产延迟

父侧按模板最大包络生成局部快照。远处事实不进入 IPC；越界查询返回 `NEEDS_WORLD`。登记对象改为紧凑证明后，删除完整 bundle、规范快照 bytes、snapshot lease return 队列和专用返回线程。

**门禁：** 大世界和等价局部快照结果一致；依赖变化仍使结果过期；close 可撤销 candidate，但 executing／safe-tail 不丢；关闭后 worker、receiver、feeder 均归零。

在 Windows 测量提交、worker 完成、receiver 发布、首次消费四个时间点。完整决定继续使用 8／15／30 ms。

每 tick 物理步上限由 K0 数据冻结。门禁只禁止无界搜索、整份证明、整图快照和递归对象图复核，不再要求全部 physics 调用为 0。

**通过条件：** receiver 身份检查为毫秒级；准备结果能在声明期限前实际可用；资源容量和关闭门槛通过。未通过则停止，不进入 F-TICK。

## K6：独立 F-TICK

F-TICK 不使用 D102 G3 predecessor。复用 B10 正式 Runtime＋Fabric 跨隙链，固定 seed 21001，四方向各 10 次。

记录每条输入流首命令的客户端 observation 发送、Python observation 接收与决定发送、客户端 frame 接收、输入采样、movement tick 应用和第一条正式回执。

跨进程时钟不相减，只按 episode、generation、observation sequence 和 request sequence 联结。缺失、重复、错身份、缓冲溢出和未封存都算 `INCOMPLETE`。

**通过条件：** 40／40 可完整复算；B10 输入、回执和安全落地不退步；如实报告 next／later 分布。

## K7：连续两跳和零等待交接

在现有 B10 motion worker 链上增加当前动作空中准备下一动作、着地关键节点比较和有界交接。不复制 D102 临时 pipeline。

顺序固定：单跳 → 两跳有安全落点 → 落地即起跳。前一步不通过，后一步不运行。

**通过条件：** 下一动作在当前动作运行期间准备；有安全停车点时不产生无必要完整停车；无安全停车点时两个动作形成同一个承诺段；落地事后核对失败时原 owner 继续负责恢复；安全、伤害、UNKNOWN、依赖和旧成功均不退步；Fabric 只在 K6 证明的 tick 协议范围内运行。

## 证据目录

新证据使用 `evidence/motion_navigation/key-boundary-motion-proof-d104-v1/`。每个阶段单独保存 index、源码哈希、命令、正式平台和结果。失败阶段保存失败指针，不创建伪造的下一阶段 predecessor。

## 实施结果

- K0：18 个 D 样本的旧扫描结果连续两次一致，正式代码未修改。
- K1 候选：18 个冻结样本的新旧风险区间、主轨迹和输入逐项一致。旧扫描总计 11,072 个物理步，候选为 5,263 个；尾迹步从 9,958 降到 4,149。`jump_gap_1` 从 407 个物理步降到 177 个。
- 独立审查反例：候选只在输入变化、支撑布尔变化和相邻边界检查停车尾迹。两个采样点之间若短暂出现 `UNSAFE→SAFE`，候选可能漏掉风险。最小反例中，旧扫描器得到 `(最后可放弃 4，承诺 5，恢复 6)`，候选却返回零风险并接纳。
- 结论：18 个样本一致只证明样本范围，不能证明关键节点推导完整。K1 未通过，候选已从活动 D101 路径撤回。
- 诊断：在发现上述问题前运行过一次 Windows 原 M0。它的 P95／P99／最大值为 0.1285／18.2522／18.5278 ms，功能与安全样本没有失败。由于 K1 前提不成立，这批数据只保留为诊断，不进入 K2，也不能用于签署性能。

按本计划的止损规则，K2—K7 不实施。失败原型、K0 对照和无效候选诊断保存在 `evidence/motion_navigation/key-boundary-motion-proof-d104-v1/`。
