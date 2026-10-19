# D061：分开验证独立场景和长 Session 性能

日期：2026-10-05。第四次修订日期：2026-10-05。状态：独立场景和长 Session v4 两条门槛均已通过，D060-D 完成。v3 的 prepare 和控制路径墙钟有效，但 deadline/slack 量尺不完整；v1—v3 原证据不改写。D060-E 独立复审和 D060-F 实机仍未开始。

## 要解决的问题

D060 修正已经通过组件和完整导航回归。性能工具随后按固定顺序在一个 Python 进程里连续运行十个 F1-C 场景。v5、v6 和 v8 都在第十场的同一个 prepare 样本超过 30 ms。v8 用低扰动 GC 记录确认，该帧 48.8184 ms 中有 36.9770 ms 来自一次 generation 2 完整扫描。扣除这段后约为 11.8414 ms，与相邻重帧一致。

这次扫描没有回收对象。它扫描的是当前仍可达的 Runtime、Session、世界和测试取证对象。十场之间没有重置 GC 代际，因此前九场的分配节奏决定了第十场第 68 个 prepare 触发完整扫描。

F1-C 的十个场景是十次独立试验。F1-D 也要求每场使用新世界和新目录。把它们连续放进同一 Python heap，可以检查长期进程尾部，但不能单独回答“某一场 D060 修正是否让 prepare 变慢”。反过来，只在场间清理也不能证明机器人常驻运行时的尾部耗时。

因此，性能验收拆成两条。两条都通过，D060-D 才完成。

## 已确认的量尺边界

`run_scenario()` 的 `@lru_cache(maxsize=None)` 只保留每场最终摘要，不保留完整逐帧轨迹。十个缩短场景的摘要合计约 125 KB；清空缓存只减少 87 个 tracked objects。`run_manifest()` 的 results 列表只是这些摘要的引用。

测试夹具仍会扩大活动对象图：

- `_RecordingTrace.records` 逐帧保存 Runtime payload；
- `response_frames`、距离样本和 inline worker activity 只为模拟验收服务；
- 一个完整 `straight_2_0` 使用 `_RecordingTrace` 后，场景结束时完整 GC 收走 255,210 个对象；改用空 trace 后为 134,919 个。

正式 Fabric 不是空 trace。它使用 `BoundedAsyncTraceWriter` 加 `SegmentedTraceWriter`，队列容量是 **2,048**；投影耗时样本使用 `deque(maxlen=4096)`。此前分析中写成 256 是错误的，以代码常量 `TRACE_CAPACITY = 2048` 为准。

NavigationSession、规划历史、输入账本、重试账本和 WorldKnowledge 都有明确容量。生产不会像 `_RecordingTrace.records` 一样永久保存全部帧，但一个仍可达的已知世界对象图依然可能让 generation 2 扫描进入毫秒级。因此 GC 时间仍属于产品墙钟，不能从样本中扣除。

## 决定

### 门槛一：独立场景回归

继续使用现有 F1-C 十个场景、原顺序、原世界、原业务判定和原 `RuntimeNavigationDriver.prepare_proposals()` 外层 `perf_counter_ns()` 计时。

每个场景必须完整执行 `session.close()` 和 `runtime.close()`。保存该场验收摘要后，在下一场创建之前执行：

1. `run_scenario.cache_clear()`；
2. `gc.collect(2)`。

这两个动作发生在 prepare 计时外。不得在场内手动 GC，不得改变 Python GC threshold，也不得从任何 prepare 样本中扣除 GC 时间。

十场的 prepare 原始样本仍按原顺序拼接。继续全局丢弃最前 100 个样本，不得每场各丢 100 个。保留每场在 captured 和 retained 样本中的起止 ordinal，确保聚合结果可还原。

门槛保持不变：

- 六组 route revalidation 各自 P95 不高于 1 ms；
- 完整 prepare retained 样本不少于 1,000；
- prepare P95 不高于 8 ms；
- prepare P99 不高于 15 ms；
- prepare 最大值严格小于 30 ms；
- 十场业务判定全部通过。

### 门槛二：长单 Session

使用独立子进程，创建一个正式 F1-C 已知世界、一个 Runtime、一个 NavigationSession 和一个跟随任务。先执行 `move_stop_800_resume` 的移动、静止 800 tick、恢复移动时间线。完成这段时间线后继续使用同一个 task、Session 和 world，不重置 Runtime、目标身份、世界知识、输入账本或 GC 代际。

控制周期固定为 20 Hz。trace 使用正式 `BoundedAsyncTraceWriter(SegmentedTraceWriter(...), capacity=2048)`；投影统计继续使用现有 4,096 样本上限。不得改用 `_RecordingTrace`，也不得把完整世界或普通日志写入 Git 证据。

长 Session 保留原 `RuntimeNavigationDriver.prepare_proposals()` 外层 `perf_counter_ns()` 计时。同时新增正式产品控制路径墙钟：从本 tick 开始处理目标更新起，包含当帧需要执行的 `KnownWorldFollowDriver.update()` 和 `RuntimeNavigationDriver.tick()`，到 `runtime.control_frame()` 返回、后端已经接受输入为止。`InvariantMonitor`、`_tick_evidence`、响应列表和指标整理都在计时外。

每个正式控制帧使用同一对 `perf_counter_ns()` 起止点计算墙钟耗时。控制周期余量固定为 `50,000,000 ns - control_duration_ns`；余量小于等于 0，也就是控制路径墙钟达到或超过 50 ms 时，记一次 deadline miss。这里不读取传给 Runtime 的 500 ms 逻辑 lease，也不把模拟时钟推进后的 450 ms 写成控制周期余量。

GC 记录使用预分配数值缓冲，不读取 Session 私有状态，不增加逐帧字典。容量从 v2 的 32,768 提升到至少 65,536。每条记录保存 generation、start／end、collected、线程、是否处于产品控制路径、活动 control ordinal、活动 prepare ordinal 和最近完成的 ordinal。数组溢出、start／stop 不配对或控制路径回调不配对都失败关闭。

取样规则：

- 前 100 个正式控制帧为 warmup；
- retained 正式控制帧至少 4,096；
- retained 窗口内，产品控制路径或正式 trace 线程自然发生的 generation 2 GC 都算覆盖；主控制线程在监视器、证据整理等计时外触发的 GC 不算产品覆盖；
- 首个符合条件的 generation 2 GC 后，至少再完成 128 个正式控制帧；
- retained 窗口没有符合条件的 generation 2 时，继续同一 Session，最多到 8,192 个 retained 控制帧；
- 到上限仍未覆盖时保留全部样本，`sample_limit_reached=true`、`retained_gen2_observed=false`、`post_gen2_complete=false`，整体门槛为 false，命令退出码为 1。

两层性能门槛必须同时满足：

- prepare P95≤8 ms、P99≤15 ms、最大值<30 ms；
- retained 产品控制路径墙钟最大值严格小于 50 ms；
- input deadline miss 为 0；
- minimum deadline slack 大于 0。

产品控制路径的最大值、miss 和 minimum slack 都只消费去掉前 100 帧后的 retained 原始数据。captured 与 retained 帧数和统计分别输出，不能把 warmup 的结果混进任一最终控制门槛。

此外还必须同时满足：

- 原 `move_stop_800_resume` 行为门槛通过；
- 同一 task／Session／world 身份始终一致；
- 规划、跟随和输入仍走正式链；
- 安全违规为 0；
- 结束时取消有界，source 释放；
- trace worker failure 为 false、dropped records 为 0；
- 分段 trace manifest、文件哈希和 SHA256SUMS 闭合。

长 Session 中自然发生的 GC 不从任何墙钟中扣除。发生在 prepare 内的影响由 prepare 门槛捕获；发生在目标更新、Runtime、后端接受输入或 trace 竞争期间的影响由完整控制路径、50 ms 周期和 deadline slack 捕获。不得为了通过而在场内 `gc.collect()`、禁用 GC、调整 threshold、清空正式历史或重启 Session。

## 证据和停止规则

v5、v6、v8 保留为原组合量尺失败证据。v7 加入了 phase wrapper、thread clock 和逐样本对象，改变了分配与 GC 相位，只算诊断，不算性能验收。

工具提交后，先确认小样本测试、普通非诊断兼容和证据拒绝覆盖。随后从同一个干净工具提交分别运行一次：

- `d061-independent-scenarios-v1`；
- 第二次修正量尺后的 `d061-long-session-v3-formal`。

每批只运行一次。任一批业务或性能门槛失败都停止，不刷结果，不运行 Fabric。失败证据原样保留。

## 首次正式运行和量尺修订

独立场景批次从干净提交 `36986a4` 运行一次并通过。完整 prepare 保留 3,413 个样本，P95／P99／最大值为 6.8668／10.9606／14.4872 ms；六组路线重验 P95 为 0.0060—0.5180 ms。十场顺序和业务结果保持冻结值。

同一提交的长 Session v1 跑满 8,192 个 retained 样本。prepare P95／P99／最大值为 5.2877／5.5120／7.7875 ms，行为、安全、身份、取消和 trace 完整性均符合各自门槛；但是量尺只记录到 127 次 generation 0，没有记录到 generation 1 或 generation 2。旧工具把“达到 8,192 上限”直接写成 `gen2_followup=true`，继而输出 `passed=true`。这个布尔值违反本决定的覆盖要求，不能作为通过结论。

v1 原始目录 `artifacts/d061-long-session-v1/` 保持原字节。它有 20 个 trace 分段、33,340 条记录和 331,251,797 字节 trace；没有 dropped record 或 worker failure。修订后的回调在预分配数组中记录所有 GC，并区分 GC 是否发生在 prepare 内，同时保存最近完成的 prepare、warmup／retained 阶段和线程。数组溢出、start／stop 不配对、未观察到 retained prepare gen2，或 gen2 后不足 128 个样本，都会让整体门槛失败。

v2 从干净提交 `179d30c` 运行一次，并按修正后的门槛退出 1。它记录到 116 次 gen2，全部发生在 prepare 外；其中 99 次在主控制线程，17 次在 trace 线程。gen2 耗时最小／P95／最大值为 4.4167／29.3525／33.7916 ms。prepare 本身的 P95／P99／最大值为 5.1944／5.3794／7.0613 ms，不能说明完整控制路径是否满足 50 ms。诊断同时写满 32,768 个槽位并溢出，所以 v2 只证明原 prepare-only 边界不完整，不能作为长期性能通过证据。原始目录 `artifacts/d061-long-session-v2-diagnostic/` 保持原字节。

v3 不再要求 gen2 必须落进 prepare。它要求 gen2 在 retained 长 Session 的正式产品路径或 trace 线程自然出现，并用后续 128 个正式控制帧验证控制墙钟和 deadline。v3 仍只允许运行一次；任何覆盖、性能、deadline、行为、身份、trace 或诊断完整性门槛失败都停止，不运行 Fabric。

## v3 结果与第三次量尺修订

v3 从干净提交 `a6d823b` 只运行一次，批次为 `artifacts/d061-long-session-v3-formal/`。它保留 4,096 个正式控制帧。prepare P95／P99／最大值为 5.2493／5.3928／10.1588 ms；完整产品控制路径 captured P95／P99／最大值为 11.2243／30.5366／41.2163 ms，retained P95／P99／最大值为 11.2050／30.5680／41.2163 ms。这些墙钟结果有效并保留。

v3 的 `minimum_deadline_slack_ns=450,000,000` 来自 `logical_deadline - simulated_clock_after_backend`。它验证的是 500 ms Runtime lease，不是 50 ms 控制周期。虽然 v3 控制最大值实际低于 50 ms，但 deadline miss 和 slack 两项没有按冻结周期独立计算，因此 v3 不能关闭长 Session 门槛。

首个有效 retained gen2 出现在 retained ordinal 23。它由 trace 线程触发，耗时 14.6725 ms；此后继续完成的正式控制帧超过 128。GC 共记录 17,306 次，容量 65,536，没有溢出、start／stop 不配对或未结束事件。正式异步 trace 写入 16,955 条、丢弃 0、worker failure 为 false；完整分段流为 11 段、16,956 条记录。

行为门槛同时通过：稳定 excess mean／P95 均为 0，最终原始距离 0.192373 格，规划提交／接受修订为 0.974359，修订响应 P95 为 1 tick，task recovery 和安全违规均为 0，取消有界且 source 释放。v3 文件记录的 18 个 gate 全部为 true，但其中 deadline/slack 两项沿用错误量尺，不能作为现行最终通过结论。

公开证据位于 `evidence/motion_navigation/d061-independent-scenarios-v1/` 和 `evidence/motion_navigation/d061-long-session-v3-formal/`。v1、v2、v3 的完整 trace 分段继续保存在本地 `artifacts/`；公开目录只保存原始性能／GC、摘要、完整标记、分段清单和每段 SHA-256。这些材料足以复核统计和哈希，但没有本地大分段时不能逐条重放 trace 内容。旧 `performance.json`、摘要和分类按当时结果保留，不回写 v4 口径。

v4 使用相同正式路径、trace、20 Hz、warmup、retained、gen2 和行为门槛。COMMAND 必须保存本次实际使用的标准外层命令：`D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python ...`。v4 仍只允许运行一次；任一门槛失败就停止，不重跑、不运行 Fabric。

## v4 正式结果

v4 从干净提交 `4f194da` 只运行一次，批次为 `artifacts/d061-long-session-v4-formal/`。prepare 保留 4,096 个样本，P95／P99／最大值为 **4.3106／4.4078／5.7910 ms**。正式控制路径 retained P95／P99／最大值为 **9.9315／25.3232／39.7818 ms**；最小 50 ms 周期余量为 **10.2182 ms**，deadline miss 为 **0**。这些门槛全部只消费去掉前 100 帧后的 retained 原始数据。

首个有效 retained gen2 位于 ordinal 23，随后继续完成至少 128 个正式控制帧。GC 共记录 17,304 次，容量 65,536，没有溢出或回调不配对。行为、身份、安全、取消和 source 释放同时通过；正式 trace 写入 16,955 条、丢弃 0、worker failure 为 false，11 个分段共 16,956 条记录。

本次实际标准命令、15 个 artifact 哈希、原始 retained ns、18 个 gate、GC 事件、trace 完成标记和分段哈希均已独立重算或完整遍历。公开证据位于 `evidence/motion_navigation/d061-long-session-v4-formal/`。约 160 MiB 的完整 trace 分段只保存在本地 artifact；公开证据保留每段 SHA-256 和原始目录相对路径。没有本地分段时可以复核统计和哈希，不能逐条重放 trace 内容。

## 文档状态

D060-A 至 D060-D 已完成。旧完整 motion_nav 仍是既有 5 项失败，没有新增失败。D060-E 独立复审和 D060-F 实机继续等待。

旧 D058／D059 性能数字按当时口径保留，不回写。D061 只修正 D060 尚未签署的性能验收方法。
