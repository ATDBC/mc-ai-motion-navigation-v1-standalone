# D108：快照与有界验证加固计划

日期：2026-10-15。状态：**A1—A5-R3 已完成；B10 不退步门槛未关闭。** 决定见 [D108](../decisions/0108-separate-world-invalidation-from-snapshot-materialization.md)，结果见[验收](../acceptance/d108-snapshot-and-bounded-verification-hardening.md)。

## 1. A0：冻结 RED 基线

正式 Windows 基线：

- 借用 1 个 section 范围，待合并 section 为 4／16／32 时，控制线程等待中位数为 1.1／62.1／173.7 ms；
- 相同场景只处理借用范围时，控制线程等待约 0 ms，借用约 4.1—4.3 ms；
- 每批在 1／2／4／8 个满 section 各改变 1 格，观察中位数为 3.76／7.70／15.49／32.35 ms；
- 模块上限 3 秒且留下 30 秒孙进程时，分片器实际 30.26 秒才返回；
- 现有 D106 第二轮 `runtime_outside_backend_ns` P50／P95／P99／最大值为 8.8272／10.0099／16.0188／16.1032 ms。

A0 只冻结事实，不把对照 monkey patch 当作正式实现。

## 2. A1：按范围物化快照

先写失败检查，覆盖：

- 观察语义变化后实时事实、版本和依赖立即变化；
- 已发布快照立即失效；
- 观察线程不构造 `ImmutableWorldSection`；
- 借用相关范围前完成最新 section 物化；
- 无关范围借用不处理其他脏 section；
- 旧借用对象保持不变；
- 空 section、淘汰、墓碑和容量回收后缓存及脏集合有界；
- 漏失效、漏范围物化的变异检查失败。

实现只调整 `WorldKnowledge` 内部缓存生命周期，不修改公开快照、lease、worker 和 UNKNOWN 契约。

Windows 性能门槛：

- 4／16／32 个无关脏 section 时，借用 1 个 section 的并发观察等待最大值小于 10 ms；
- 1／2／4／8 section 语义变化观察满足 P95 ≤ 8 ms、P99 ≤ 15 ms、最大值 < 30 ms；
- 真实 B10 bounds 的物化与控制锁等待单列报告，整体沿用 8／15／30 ms。

若相关范围本身的锁内物化超线，A1 停止，不在本阶段临时加入两阶段无锁构造。

## 3. A2：分片器有界终止

复用 `scripts/process_tree.py` 的 PID＋创建时间登记，不再只调用直接子进程的 `kill()`。

检查覆盖：

- 正常通过、断言失败和运行错误；
- 根进程卡住且孙进程继续运行；
- 孙进程忽略正常终止，需要强杀；
- 孙进程保持 stdout／stderr 管道；
- 清理失败进入结构化诊断；
- 0 项测试记为错误。

3 秒模块上限的总命令必须在 11 秒内返回，根进程和登记后代均已退出。

## 4. A3：receiver 时序和 trace 量尺

保留现有确定性屏障测试，再新增不使用每 tick receiver 屏障的正式链时序注入：

- 结果在当前、下一和下下 movement tick 可见；
- SOLVE 和 REVALIDATE 分开；
- 目标或 generation 改变后的旧结果不得激活；
- 允许晚到的结果继续推进，不允许的结果有界重试或失败；
- 不出现异常、无界等待或身体责任丢失。

`audit_d107_b10_trace.py` 同时读取：

- `control_wall_ns`；
- `backend_elapsed_ns`；
- `backend_blocking_io_ns`；
- `runtime_outside_backend_ns`；
- `proposal_elapsed_ns`。

旧 trace 只重算，不改写原始记录。缺少 D107 实机数据时继续写“无法判断”。

## 5. A4：POSIX 生命周期与文档边界

POSIX 的子进程已经退出、父进程仍有 Queue feeder 时，主线程先关读端，唤醒可能堵在大消息上的 feeder；随后发布 Queue 的关闭哨兵并有界等待。feeder 活着时，主线程不抢关写端。feeder 退出后，主线程只做幂等收尾。关闭期间的 `EPIPE` 记为预期诊断，其他错误继续报告。Windows 的 overlapped cancellation 和现有存活报告保持不变。

修正两处关于旧命令是否可能晚到的矛盾注释。公开 README、AGENTS 和验收并列写清主项目与公开快照的模块／测试数量。

分片器的默认单模块期限按正式平台区分：Windows 300 秒，POSIX 600 秒；显式 `--timeout-seconds` 始终优先。Windows Job Object 继续使用正式 CPython 3.11 的 `Popen._handle`。该私有句柄缺失时直接拒绝启动，不能降级为没有所有权保证的 PID 操作。

Linux 需要复核重复创建关闭、队列满、大消息和 worker 强杀；如果当前环境无法运行 Linux，只能记录为待三方复核，不能声称 Linux 已关闭。

## 6. A5：回归与 B10 Fabric

依次运行：

1. A1—A4 直接检查；
2. D107 11 个回归模块；
3. D106 78 项直接回归；
4. Windows 分片正序、逆序；
5. 独立公开导出；
6. B10 四方向正式 Fabric 批次。

B10 新记录增加 receiver 每轮耗时，以及“registry 已发布但下一决定仍不可见”的计数。现有协议没有 worker 完成时刻，不能把 receiver 观察到结果写成 worker 已就绪。B10 失败不会回写 D107 或 D106 的历史结果。

## 7. 止损

- A1 两轮内仍不能让无关脏 section 与借用耗时解耦，停止并设计两阶段物化；
- A2 无法在 11 秒内清理精确登记的进程树，停止并修复共享进程监督器；
- A3 需要通过等待真实时间才能稳定，停止并回到虚拟 movement tick 协议；
- 发现新能力需求时只登记，不扩大 D108。

## 8. 收尾结果

A1—A4 的实现、直接检查和独立复核均已完成。A5-R2 把 B10 的六类 JSONL、Runtime trace 和 F-TICK 都移出控制 tick 的同步写盘路径；有界缓冲在正常或异常收尾时写盘，主异常不会再被次级写盘错误覆盖。正式 Windows 源树正序、逆序各 186 个模块、2307 项通过，零失败、错误、超时和跳过。独立公开快照正序、逆序各 185 个模块、2283 项通过；与主源码的 24 项差额全部来自约定不公开的 D102 原始证据模块。Linux 的 A4 动态生命周期矩阵仍待三方复核。

A5-R3 没有改运动行为。它修正了旧 A5 receiver 字段的含义，把现行链明确写成“receiver 观察 → registry 发布 → 控制消费”；同时让 receiver 去重集合只保存真正进入有界缓冲的记录。A5-R2 的 2307／2283 项完整矩阵仍是最近一次全量对象；R3 只改变溢出后拒绝记录的诊断内存占用和证据元数据，因此按 D089 只运行受影响与相邻检查。独立复核确认行数、总字节和单条大小三道门槛共用同一接纳返回路径，去重集合只随已接纳记录增长；R3 可以按该限定范围签署，但不能写成新的完整矩阵结果。

B10 的四方向 receiver 时序为 40／40 下一 movement tick。A5-R2 诊断确认 10 次结果都在 receiver 观察当 tick 注册，并在下一控制决定消费；worker 完成时刻因现有协议没有该字段而明确为不可用。移除同步文件 I/O 后，控制线程自身 P95 为 0.8191 ms，但批次仍在第 3208 个 movement tick 触发旧 `deadline_exceeded`；清理继续覆盖原始失败并留下 ordered source。这些缺陷不在本阶段修改。D108 因此不恢复新增运动能力，也不把 B10 写成通过。
