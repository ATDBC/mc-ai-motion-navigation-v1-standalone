# D108：快照与有界验证加固验收

日期：2026-10-15。当前状态：**A1—A5-R3 已完成；B10 不退步门槛未关闭。**

本文件记录 [D108](../decisions/0108-separate-world-invalidation-from-snapshot-materialization.md) 的正式结果。D103—D107 的历史证据不回写。

## 1. A0 Windows RED

### 1.1 借用锁阻塞

| 待合并 section | 当前控制线程等待中位 | 当前最大 | 只处理借用范围的控制等待 | 对照借用耗时 |
|---:|---:|---:|---:|---:|
| 4 | 1.1 ms | 1.9 ms | 约 0 ms | 4.1 ms |
| 16 | 62.1 ms | 62.4 ms | 约 0 ms | 4.3 ms |
| 32 | 173.7 ms | 175.5 ms | 约 0 ms | 4.3 ms |

绝对数字与 Linux 不同，但耗时随全局待合并 section 增长，根因成立。

### 1.2 语义变化观察

| 一批涉及 section | 中位 | 最大 | 纯证据刷新中位 |
|---:|---:|---:|---:|
| 1 | 3.76 ms | 7.86 ms | 0.014 ms |
| 2 | 7.70 ms | 12.14 ms | 0.019 ms |
| 4 | 15.49 ms | 21.28 ms | 0.026 ms |
| 8 | 32.35 ms | 38.80 ms | 0.044 ms |

### 1.3 分片器超时

3 秒模块上限、孙进程睡眠 30 秒。当前报告为 `timeout`，但记录用时 30.11 秒，总命令 30.26 秒才返回。超时门槛未真正有界。

### 1.4 现有 trace 字段

D106 第二轮 182 帧：

| 字段 | P50 | P95 | P99 | 最大值 |
|---|---:|---:|---:|---:|
| `control_wall_ns` | 48.0148 ms | 50.4687 ms | 52.2836 ms | 52.5369 ms |
| `backend_elapsed_ns` | 39.4569 ms | 43.1432 ms | 44.1267 ms | 44.7619 ms |
| `backend_blocking_io_ns` | 38.9153 ms | 42.6002 ms | 43.5852 ms | 44.2372 ms |
| `runtime_outside_backend_ns` | 8.8272 ms | 10.0099 ms | 16.0188 ms | 16.1032 ms |
| `proposal_elapsed_ns` | 0.3676 ms | 1.9114 ms | 2.1420 ms | 2.2602 ms |

旧记录只能冻结修复前事实，不能代替 D108 后的实机结果。

## 2. 正式结果

### 2.1 A1 按范围物化

语义变化观察已经只更新权威事实、版本和失效标记。不可变 section 延迟到相关范围借用时才物化。无关脏 section 不再增加本次借用工作。

| 检查 | Windows 正式结果 |
|---|---:|
| 1／2／4／8 section 语义变化 P95 | 0.0225／0.0238／0.0418／0.0527 ms |
| 上述最大值 | 0.0288／0.0264／0.0422／0.0603 ms |
| 4／16／32 个无关脏 section 时，相关范围借用 P95 | 3.5071／3.5500／3.4967 ms |
| 真实 B10 bounds 物化 P95／P99／最大值 | 1.6762／1.6938／1.6938 ms |
| B10 bounds owner 锁等待／Python 不可用 | 0.3056／0.3681 ms |

变异检查覆盖漏失效、漏范围物化、旧借用不可变性、空 section、墓碑、淘汰和容量回收。A1 的实现提交为 `33a40cd4`，门槛加固提交为 `911193b7`。

### 2.2 A2 有界终止

分片器现在从子进程启动时就建立 Windows Job Object 或 POSIX 进程组。超时时按父侧确认的 PID 和创建时间终止整棵树；输出读取和强杀都有期限。伪造 PID、创建时间冲突和缺失 `Popen._handle` 均有拒绝检查。3 秒期限、留下 30 秒孙进程的反例在 11 秒内返回；零测试模块记为错误。

实现提交为 `46df5fb4`、`e508718a` 和 `49bfe965`。A2 最终模块 11／11 通过；完整 D108 直接集合也覆盖这组检查。

### 2.3 A3 receiver 晚到矩阵与 trace 审计

正式链夹具经过 `PlayerRuntimeV1 → RuntimeNavigationDriver → NavigationSession → MotionRouteCoordinator`。控制循环不会在每个 movement tick 等待 receiver。测试只在主动投递结果后使用一次发布屏障，屏障次数与投递次数相同。

| 相对首次可投递 tick 的额外延迟 | 投递作业 | 首次提交跳跃的 movement tick | 发布屏障 |
|---:|---|---:|---:|
| 0 | `SOLVE` | 4 | 1 |
| 1 | `SOLVE → REVALIDATE` | 7 | 2 |
| 2 | `SOLVE → REVALIDATE` | 9 | 2 |

三组都没有 Runtime 失败、契约异常、伤害或身体责任丢失。工作身份与重试保持有界。0 tick 的 `SOLVE` 记录为 `APPLIED`；晚 1／2 tick 的 `SOLVE` 记录为 `RECOMPUTE`，随后 `REVALIDATE` 记录为 `APPLIED`。旧 generation 场景先正式修订目标，再让原路线依赖失效；计算 generation 从 3 前进到 5。旧结果随后到达，但没有安装证明。任务因落点已不存在而以 typed `no_route_within_complete_scope` 安全结束，Runtime 仍可用。收尾后没有活动 mailbox、身体控制或存活 worker。单纯目标修订按现行规则会保留 computation generation；真正影响计算依据的失效才推进 generation。

测试直接包装 `MotionResultInbox.wait_for_receiver`。只有主动投递后的短窗口允许调用；调用次数必须等于投递次数。故障注入在 `driver.tick()` 内增加一次等待时，门禁按预期失败，因此这个检查可以发现逐 tick barrier 回归。

trace 审计升级为 `mc2p.d108-b10-trace-audit.v2`。它分别统计 `control_wall_ns`、`backend_elapsed_ns`、`backend_blocking_io_ns`、`runtime_outside_backend_ns` 和 `proposal_elapsed_ns`。各字段单独报告缺失值，不相加。`trace.jsonl` 同时有观察接收与派发时间时，另报 `control_prepare`。旧 trace 没有 receiver 发布和首次消费 movement tick，因此明确报告为不可用。

定向结果：新增 8 项通过；正式链相邻回归 50 项通过。这里只是确定性正式链证据，还不是 D108 后的 Fabric receiver 时序证据。

### 2.4 A4 POSIX 生命周期与公开边界

生产关闭顺序已经调整：POSIX 在子进程退出且父 feeder 存活时，由主线程先关读端，再发布 Queue 关闭哨兵并有界等待。feeder 未退出时，主线程不关闭写端；退出后再做幂等收尾。预期 `EPIPE` 与其他 feeder 错误分别计数。Windows 的 overlapped 取消顺序保持不变。

Windows 平台隔离检查覆盖：POSIX 读端／哨兵／feeder 的先后顺序、feeder 超时后写端仍归 feeder、无 feeder 的重复关闭、预期 `EPIPE` 与其他错误分流，以及现有的 Windows overlapped 关闭路径。实际 Linux 重复创建关闭、队列满、大消息和 worker 强杀尚未在本机动态运行，继续列为三方复核项，不能写成已通过。

分片器默认单模块期限现在是 Windows 300 秒、POSIX 600 秒，命令行显式值优先。子报告只能提供候选 PID，不能授予终止权限。父侧必须先用 Windows Job Object 或 POSIX 进程组实时确认所有权，并读取当时的创建时间，之后才能把漏过轮询的身份加入清理清单。报告伪造独立进程的 PID 和创建时间时，该进程不会进入清理清单；PID 创建时间冲突时仍保留父侧已确认的身份。Windows Job Object 固定使用 CPython 3.11 的 `Popen._handle`，句柄缺失时拒绝启动。

D107 的主项目 Windows 记录为 184 个模块、2264 项。D108 新增两个模块；公开快照仍排除依赖约 46 MiB 本地原始证据的 `tests/motion_nav/test_d102_evidence_contract.py`。实际 A5 数量见下节。Linux 重复创建关闭、队列满、大消息和 worker 强杀仍待三方动态复核。

### 2.5 A5 回归、公开快照与 B10

直接检查按冻结来源运行：A1—A4 为 75／75，D107 原 11 个回归模块为 126／126，D106 原直接集合在当前源码为 79／79。D106 历史记录仍是 78 项；多出的 1 项来自 D108 快照检查，不能回写旧记录。

直接检查命令没有重新挑选模块：A1—A4 使用本阶段五个登记模块；D107 使用其计划第 5 节列出的 11 个模块；D106 使用 R1、R2、R3 的原证据命令。实际入口分别为：

```text
python -m unittest tests.motion_nav.test_d108_snapshot_materialization tests.motion_nav.test_d107_shard_runner tests.motion_nav.test_d107_b10_trace_audit tests.motion_nav.test_d108_receiver_tick_delivery tests.motion_nav.test_d102_registry_lifecycle
python -m unittest tests.motion_nav.test_r27_async_admission tests.motion_nav.test_async_gap_revalidation_delivery tests.motion_nav.test_motion_baseline_recovery tests.motion_nav.test_motion_start_delivery tests.motion_nav.test_r28_persistent_budget_formal tests.motion_nav.test_r28_planning_retry_successor tests.motion_nav.test_r28_reanchor_handoff tests.motion_nav.test_f2rec_r0_baseline tests.motion_nav.test_navigation_design_metrics tests.motion_nav.test_navigation_product_delivery tests.motion_nav.test_public_repository_completeness
```

D106 不另抄一份可能漂移的模块表，逐条使用 `sound-motion-proof-d106-v1/r1/index.json` 的 `commands.tests`、`r2/index.json` 的 `command` 和 R3 登记的 direct modules。三组用时分别为 71.24／81.43／6.76 秒。A5—A5-R2 共新增 9 项诊断与写盘边界检查；它们已经包含在最终 2307 项完整集合中。

| 集合 | 模块 | 检查 | 失败／错误／超时／跳过 | 模块耗时合计 |
|---|---:|---:|---:|---:|
| 主源码正序 | 186／186 | 2307 | 0／0／0／0 | 992.10 s |
| 主源码逆序 | 186／186 | 2307 | 0／0／0／0 | 996.03 s |
| 公开快照正序 | 185／185 | 2283 | 0／0／0／0 | 收据在导出外 `.tmp` |
| 公开快照逆序 | 185／185 | 2283 | 0／0／0／0 | 收据在导出外 `.tmp` |

公开导出的最终文件数记录在证据索引中。源树和独立快照均通过导出校验；Java 门禁各 3／3。公开集合少 1 个模块、24 项，原因仍是本地 46 MiB D102 原始证据没有进入公开仓库。证据目录里的 `public-*-summary.json` 保留 A5-R 历史结果；A5-R2 最终公开正逆报告只存在本机导出清单排除的 `.tmp` 中，没有进入公开仓库。证据索引只保存这两份外部收据的路径、字节数和 SHA-256。公开仓库读者只能核对生产代码语义哈希、证据哈希和 `verify`，不能把收据哈希当成已经公开了完整运行报告。

B10 的历史 D108 命令是 `--b10-gap-solver-probe --time-diagnostics --physics-tick-diagnostics --ftick-diagnostics`。它遗漏了历史正式 B10 使用的 `--timeout-seconds 600`，因此实际采用 240 秒默认值，并只给任务留下 215 秒。D108 的原命令和失败记录保持不变；D109 起的正式命令必须显式追加 `--timeout-seconds 600`。第一次 A5 批次的 receiver 证据仍在每次 `propose()` 后逐行打开文件、flush 和 fsync，因此该批次的控制时序只保留为失败记录，不能作为最终 receiver 延迟结论。它还把 receiver 从结果队列取到消息的时刻误写成 worker 完成时刻；旧字段和原始证据不回写。

A5-R 将记录改为容量 4096 的有界内存缓冲。控制循环只追加有类型、不可变的小记录；重复记录去重，溢出会写入结构化计数并使批次失败。运行完成或异常收尾时只批量写盘并 fsync 一次。诊断默认关闭，只有正式探针发布 movement tick 标签后才启用。

A5-R 四方向 validation 仍为 40／40 `NEXT_MOVEMENT_TICK`，sidecar 丢弃 0。receiver 共记录 603 轮，周期 P95／P99／最大值为 0.1881／0.8710／1.2268 ms。现有协议只能测“receiver 观察到结果 → 注册发布 → 控制线程首次消费”；worker 完成到 receiver 观察的时长明确为 `unavailable`。10 次交付都在观察当 tick 注册，并在下一控制决定消费，`published_but_next_decision_invisible_count=0`。

A5-R trace 的 `runtime_outside_backend` P50／P95／P99／最大值为 1.1748／1.9196／2.2956／2.4393 ms；proposal 为 0.3702／1.8714／2.1027／2.1780 ms。`control_prepare` 在 3219 帧上的 P50／P95／P99／最大值为 3.1111／4.6472／23.3283／31.2439 ms。相对含同步 receiver 写盘的 A5 批次，P95 从 6.5817 ms 降到 4.6472 ms，最大值从 43.1130 ms 降到 31.2439 ms；两次均未出现超过 50 ms 的 control-prepare 样本。

A5-R2 继续审计同一脚本，发现 `diagnostics.jsonl` 和 coordinator frame／trial 文件仍逐条 `open + flush + fsync`，Runtime `trace.jsonl` 也由 Python 控制线程直接写入；F-TICK 又在客户端 `tickMovement` 内逐事件 flush。这些路径现已分别改为：

- 六类 B10 JSONL 共用有界内存缓冲，逐文件保留顺序，运行或异常收尾时各做一次原子批写；所有流合计最多保留 43 MiB；
- Runtime trace 使用容量 16384 的 `BoundedAsyncTraceWriter`，控制线程只做不可变快照和非阻塞入队；
- F-TICK 最多保留 65536 项、1600 万字符，客户端关闭后才生成分段文件；丢弃数继续进入 sealed summary；
- 主异常与批写异常同时出现时保留主异常，把批写错误附加到异常说明；没有主异常时，溢出或批写失败使批次失败。

门禁会在 B10 脚本重新出现 `append_jsonl()`，或缓冲追加阶段调用 `Path.open`／`fsync` 时失败。正式控制 tick 因此没有同步文件 I/O；网络后端等待仍按原字段单列，不属于文件写盘。

A5-R2 最终 Fabric 批次仍为 40／40 `NEXT_MOVEMENT_TICK`，receiver 的 10 次交付仍全部在观察当 tick 注册、下一控制决定消费，sidecar 丢弃 0。`runtime_outside_backend` P50／P95／P99／最大值降为 0.7345／0.8191／0.8568／0.8612 ms；`control_prepare` 在 3207 帧上的 P50／P95／P99／最大值为 1.3246／3.2257／22.7521／37.1607 ms，没有超过 50 ms 的样本。

批次仍在第 3208 个 movement tick 触发同一 `deadline_exceeded`。每个游戏 tick 的后端等待中位约 48.9 ms，约 3200 个 tick 已接近固定任务期限；去掉同步文件 I/O 后失败仍出现。因此这些同步写盘是量尺污染和延迟来源，但不是旧 B10 deadline 的充分根因。

A5-R3 修正了两个诊断边界。第一，证据索引把旧 A5 的 `receiver_ready_but_next_decision_invisible` 移入明确的 legacy 区域；该字段无法表示 worker 完成时刻，现行结果只报告 receiver 观察、registry 发布和首次控制消费。第二，receiver 去重集合现在只在记录真正通过行数与字节上限后登记；大量唯一记录溢出时，缓冲和去重集合都保持有界。

本次只影响溢出后已经被拒绝的诊断记录和证据说明，不改变控制、动作、观察或正常证据路径。A5-R2 的主源码 2307 项、公开快照 2283 项仍是最近一次全量结果；A5-R3 按 D089 只运行受影响与相邻检查、独立复核、导出与证据哈希。没有把这次定向检查冒充为新的全量矩阵。

A5-R3 的 receiver、B10 runtime、Fabric 探针、异步 trace 和 F-TICK 相邻集合为 90／90。第一次运行还发现两个旧测试仍向正式入口传入 `object()` 作为证据目录；测试替身已改用临时 `Path`，原有 worker 复用、来源注销和异常断言没有放宽。

独立只读复核另运行 35 项并用总字节上限探针检查拒绝路径，确认旧实现会让 1002 条唯一记录把去重集合扩到 1002，而现实现只保留已接纳的 2 条；总字节拒绝同样不会扩大集合。复核没有发现阻塞项。单条大小和总字节限制与行数限制共用同一接纳出口，后续可补参数化用例，但不阻塞本轮。

A5-R3 独立导出仍为 2280 个文件，`verify` 通过；导出内 receiver、B10 runtime 和 standalone 相邻检查为 47／47。该结果只证明本轮改动已进入可校验快照，不替代 A5-R2 的公开正逆完整矩阵。

B10 随后在 R4 矩阵中触发 Runtime `deadline_exceeded`。当前最强证据表明，这是 215 秒探针总预算用尽：最近一次停止在 movement tick 3208，原始试次文件中已经完成 24 个 `r4_validation_*` 试次。它不能证明某个 R4 动作超时。清理代码又在 Runtime 已非 READY 时调用注销，外层最终只报告 `formal Runtime must be ready`，覆盖了原始失败。ordered source 的显式注销失败，但随后会由 `Runtime.close()` 的 `arbiter.clear()` 清除。原始目录、manifest、哈希和失败结果均保留。D109 会先用当前 HEAD 和 `--timeout-seconds 600` 复验，再处理清理与生命周期问题；在此之前 B10 不退步门槛仍未关闭。

证据位于 `evidence/motion_navigation/d108-snapshot-hardening-v1/`。D103—D107 的历史记录保持原样。
