# D107：D103 回归修复验收

日期：2026-10-14。当前状态：**通过。**

本文件记录 [D107](../decisions/0107-repair-d103-regressions-before-new-motion-work.md) 的运行结果。D103—D106 的历史记录不回写。

## 1. R0 基线

正式 Windows 抽查：

- `tests.motion_nav.test_r27_async_admission`：25 项，15 个失败、1 个错误；
- F1 缩短场景：80 tick，14.6 秒，约 182 ms/tick；
- `apply_observed_blocks` 41 次，共 9.5 秒；
- `_commit_snapshot_sections` 53 次，共 13.6 秒，占总时间 93%；
- `validate_frozen_value` 177 次，共 13.0 秒，占总时间 89%；
- `ImmutableWorldSection` 构造 177 次，section 事实数中位 3,408。

这些数字只冻结修复前基线，不是验收通过结果。

## 2. R1 分片运行器

新增 `scripts/run_motion_nav_shards.py`。它按稳定顺序逐模块启动独立进程，支持过滤、逆序、单模块期限和原子 JSON 报告。状态明确区分通过、断言失败、运行错误、超时和进程启动失败。

运行器的 4 项直接检查通过。Windows 并发读取原子报告时可能短暂遇到 sharing violation；读取端只对这一种系统错误有界重试。损坏 JSON 仍立即失败，最终报告读取期限仍为 5 秒，异常路径会回收子进程。

## 3. R2 世界观察热路径

根因是同一方块仅更新时间戳时，也会把整个 section 标成脏数据。观察提交随后重建约 3,400 个格子的对象图，并再次做深度校验。

修复后的规则是：

- 方块身份、碰撞形状、空气状态或依赖事实变化时，立即重建 section；
- 只有观察时间和证据版本变化时，先更新证据 owner，到下次借用快照时再合并；
- 已借出的快照保持不可变；
- 可信 owner 的内部构造做结构检查，公共构造和进程边界继续做完整深度校验。

F1 缩短场景从约 182 ms/tick 降到 14.57 ms/tick，低于 40 ms 门槛。该场景中的深度校验从 177 次降到 0；几何变化、旧快照不可变和依赖失效检查保持通过。专项检查 227／227 通过。

## 4. R3 回归分类与修复

计划列出的 11 个模块全部通过。修复分成三类：

| 类别 | 根因 | 处理 |
|---|---|---|
| 正式行为 | legacy 提交后没有唤醒 receiver；已应用并被后继覆盖的输入账本没有退场 | 提交事实发布后唤醒 receiver；只清理已应用且已有更新实际应用接管的旧记录 |
| 测试时钟 | 测试 worker 用 receiver 轮询次数代替 movement tick，结果受线程调度影响 | 所有确定性交付夹具改用模拟 movement tick，并在需要消费结果的位置使用 receiver 屏障 |
| 契约夹具与量尺 | Mock 缺少现行接口、指标分类遗漏代理能力、公开依赖白名单过宽 | 补齐公开协议，按现行类型分类，并收紧到唯一允许路径 |

历史 D105 证据继续验证原文件和原哈希。当前源码变化由严格模式单独报告，不能通过改写 D105 哈希冒充旧证据。公开快照另登记 D107 修改的三个生产文件语义哈希。

首轮完整盘点为 184 个模块中 179 个通过、5 个失败。5 个失败均完成根因修复后，在同一分片入口下 5／5 通过。后续完整正序与逆序结果见下一节。

## 5. R4 完整门禁

最终代码在正式 Windows 环境按每模块独立进程、单模块 300 秒上限运行：

| 顺序 | 模块 | 测试 | 失败 | 错误 | 超时 | 跳过 | 模块用时合计 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 正序 | 184／184 | 2264 | 0 | 0 | 0 | 0 | 1058.64 s |
| 逆序 | 184／184 | 2264 | 0 | 0 | 0 | 0 | 1041.93 s |

紧凑报告位于：

- `evidence/motion_navigation/d107-regression-recovery-v1/full-forward-summary.json`；
- `evidence/motion_navigation/d107-regression-recovery-v1/full-reverse-summary.json`。

每份报告保存模块状态、用时、测试数和原始完整报告的 SHA-256，不把逐项 stdout 重复提交进 Git。

公开导出相关检查 16／16，通过实际导出校验：`STANDALONE_EXPORT_OK files=2237`。

## 6. R4 现有 B10 trace 只读复核

本轮只读取现有 Fabric 目录，没有重放或修改原始记录。工具为
`scripts/audit_d107_b10_trace.py`，结果保存在
`evidence/motion_navigation/d107-regression-recovery-v1/trace-audit.json`。

控制准备耗时按每条 `step` 的
`dispatched_monotonic_ns - decision_observation_received_ns` 计算。分位数使用最近秩。

| 原始运行 | step 数 | P50 | P95 | P99 | 最大值 | 大于 50 ms |
|---|---:|---:|---:|---:|---:|---:|
| D105 `20261013T131224340162Z-90a78bfa` | 452 | 12.6924 ms | 33.8375 ms | 34.9894 ms | 62.7074 ms | 2 |
| D106 第一轮 `20261014T012937677931Z-99f5767c` | 71 | 8.8623 ms | 31.2649 ms | 41.0317 ms | 41.0317 ms | 0 |
| D106 第二轮 `20261014T014559863045Z-42781cde` | 2771 | 11.6071 ms | 33.5568 ms | 39.0341 ms | 47.2847 ms | 0 |

三份 trace 的 receipt movement tick 都连续，没有缺口。每帧包含 1—2 条
`input_applications`。D105 有 2 个 `lease_exhausted` 样本；D106 第一轮有 1 个，第二轮有
4 个。三份 `ftick-events` 的 `INPUT_SAMPLED` 和 `MOVEMENT_APPLIED` tick 也各自连续。

D106 第二轮另有 182 条 `b10-coordinator-control-frames`。其中
`control_wall_ns` 的 P50／P95／P99／最大值为
48.0148／50.4687／52.2836／52.5369 ms，13 帧大于 50 ms。这是协调器自己的整帧量尺，
与上表的 trace 控制准备区间不同，不能混成同一个指标。D105 和 D106 第一轮没有这份文件，
报告明确记为 `unavailable`。

D103 前 2026-09-25 的原始运行目录不在当前工作区或公开证据包中。因此现有记录只能描述
D105、D106 各自的时序，不能证明或排除 D103 性能回归导致 B10 跨 tick。section 提交次数也
不在这些 trace 中，当前同样无法从原始记录判断。该因果结论保持 `unavailable`，等待有可比
原始记录或新的同源码对照试验。

## 7. 结论与边界

D103 的两类回归已关闭：观察摄取不再因同语义时间戳刷新而重建整个 section；现行异步接纳、复核、预算和重新锚定路径恢复通过。测试基础设施也不再用真实 receiver 轮询次数充当 Minecraft movement tick。

D107 没有调整 D106 的同步起始证明门槛，没有修 B10 带速入口，也没有授权连续两跳。现有 trace 不能证明 D103 性能回归导致 D106 的 B10 跨 tick；该因果关系继续记为无法判断。

