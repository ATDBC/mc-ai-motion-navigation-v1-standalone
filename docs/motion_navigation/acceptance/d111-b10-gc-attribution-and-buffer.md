# D111 B10 GC 归因与证据缓冲验收

日期：2026-10-15。状态：**FAILED／STOPPED**。授权实现与唯一一次正式 B10 已完成，原始求解墙钟门槛未通过。

决定见 [D111](../decisions/0111-remove-b10-observer-gc-distortion.md)，计划见[阶段计划](../stages/d111-b10-gc-attribution-and-buffer-plan.md)。

## 1. 冻结边界

- 原始求解墙钟最大值仍要求 `< 30 ms`；
- 扣除 GC 停顿后的时间只作诊断；
- 不改变运动、求解器、候选、输入或安全语义；
- 只允许一次完成前置门禁后的正式 B10。

## 2. Windows 前置门禁

| 项目 | 结果 |
|---|---|
| 两项测试时序修正所在模块 | 70／70，零跳过；原断言保留 |
| 字符串缓冲、GC 故障注入及 Runtime／trace 相邻检查 | 175 项，174 通过；1 项既有 Windows 符号链接夹具权限跳过 |
| 公开完整性与真实独立导出检查 | 18／18，零跳过 |
| 独立审查 | 79／79，零跳过；另做真实后台 GC 5／5 机制检查，不作为性能样本 |
| D110 历史语义哈希 | 16 文件 × source/public 两份 Git 对象，共 32／32 一致；记录 `67f4f1d8`／`f663586` 与 CRLF→LF |
| 源树完整正序／逆序 | 各 186／186 模块、2334 项 |
| 独立公开快照完整正序／逆序 | 各 185／185 模块、2310 项 |
| 完整矩阵失败、错误、超时、跳过和 cleanup 异常 | 四组均为 0 |
| Java | 源树、快照各 3／3 |
| 独立快照 verify | 完整矩阵前后均 `STANDALONE_EXPORT_OK files=2322` |

实施提交为 `01afa1212214e8f985c547641a30a052a062be9a`。证据缓冲和局部诊断列表只存规范 JSON 字符串，控制试次结束后才解析。正常 JSONL 与旧写法逐字节一致，修改隔离、容量、接纳顺序、溢出和 flush 失败语义检查通过。

GC 与求解使用同一 `perf_counter_ns` 时钟。全部试次后才按区间交集归因；晚 stop、跨线程、跨样本、缺配对、溢出、测量异常及第三方 callback 保留均有检查。原 trial JSONL 不回写派生值，最终汇总保留逐样本结果与全程时间线。新增的 `b10b_gc_pause_timeline_complete` 独立检查证据完整性，原始求解门槛不变。

四份分片报告位于 `.tmp/d111/`，公开摘要记录路径、大小和哈希。Java 与 verify 来自主代理终端实测转录，没有单独日志。完整矩阵和 2322 文件 verify 先于本次结果文档、公开证据和完整性清单收尾，不能算作新增文件的完整矩阵结果。

## 3. 唯一一次正式 B10

正式运行：`artifacts/fabric-deployment/20261015T151355333152Z-0f9aee96`。命令显式使用 `--timeout-seconds 600`，seed 为 21002，server port 为 25598，IPC port 为 8141。完整命令和来源见[精简证据](../../../evidence/motion_navigation/d111-b10-gc-attribution-v1/README.md)。

| 项目 | 结果 |
|---|---|
| validation | 40／40 |
| turn validation | 40／40 |
| 默认协调链 | 10／10 |
| R4 三速度带 × 四方向 × 五次 | 60／60 |
| typed negative | 24 个全部符合预定类型 |
| 伤害 | 0；初末生命均为 20 |
| ordered source | `explicitly_released` |
| cleanup failure／diagnostic failure | 0／0 |
| trace 接纳／写入 | 21805／21805；另有 1 行 delivery summary |
| trace 丢弃／不确定记录 | 0／0；worker 正常结束 |
| diagnostics | 5401 行，最后 close 诊断有效 |
| 完整时间归因 | `complete_time_event_attribution` 通过 |
| GC 时间线完整性 | `b10b_gc_pause_timeline_complete` 通过 |

22 项正式检查中，21 项通过。唯一失败为 `client-0:b10b_solver_is_bounded`。来源释放和清理无失败，不把墙钟超限写成动作、安全或生命周期失败。

## 4. 求解墙钟与 GC 归因

| 142 个求解样本 | P95 | P99 | 最大值 | 达到或超过 30 ms |
|---|---:|---:|---:|---:|
| 原始墙钟 | 31.8873 ms | 33.4408 ms | 39.9605 ms | 19 |
| 扣除已记录 GC 停顿后的诊断值 | 31.4963 ms | 32.1705 ms | 33.1761 ms | 17 |

GC 全程记录 **656 个事件、328 个完整区间**，gen0／gen1／gen2 分别为 298／27／3 次。丢弃、未配对、callback 测量错误和求解测量错误均为 0。78 个样本的旧 completed-collection delta 标记发生 GC，保留用于兼容分析；精确停顿以最终区间交集为准。

扣除值仍超限，不能把本轮性能失败归结为 GC 停顿。剩余墙钟包含求解工作和调度等时间，当前证据没有继续分离它们。D110 那次唯一 gen2 重叠样本的历史结论不回写，也不据此宣称已证明其因果。

公开摘要保留 142 份紧凑求解指标和 328 个已配对 GC 区间，可独立复算交集与分布。656 个原始 callback 事件和完整控制轨迹仍保留在本地原始文件中，公开只记录路径、字节数和 SHA-256。

## 5. 停止结论与证据边界

D111 **FAILED／STOPPED**，D110-SOLVE-01 保持未关闭。不修改 30 ms 门槛，不重跑 B10，不追加求解器优化；连续两跳、零等待交接和新动作仍未授权。

2026-10-16 后注：用户确认本次正式运行时还有其他程序在工作；三方又确认 D111 整轮的求解线程 CPU 和无关 trace 投影均比 D110 慢。该信息不改变旧门槛下的失败，但限制了性能解释：本批可以证明 GC 不是整体超限主因，不能代表空闲机器上的稳定求解速度。后续按 [D112](../decisions/0112-separate-reference-calibration-from-runtime-delivery.md) 区分参考机校准、生产交付期限和跨设备有界降级。

Windows 是正式性能平台。三方 D110 审查已在 Linux 上补齐 settle、zombie／双 fork 和 fd 动态复核；D111 又采纳两项只改测试的时序修正。Linux 机制结果不替代 Windows 墙钟，也不声称本轮 Windows 执行了 Linux fork 动态场景。相关状态见[缺陷台账](defect-ledger.md)。
