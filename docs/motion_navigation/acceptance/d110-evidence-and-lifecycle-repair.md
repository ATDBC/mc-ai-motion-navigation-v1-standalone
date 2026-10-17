# D110 关闭证据链与生命周期修复验收

日期：2026-10-15。状态：**已完成已授权实现；关闭归因通过，求解墙钟门槛未通过，阶段停止。**

决定见 [D110](../decisions/0110-repair-close-diagnostics-and-measure-solver-tail.md)，计划见[阶段计划](../stages/d110-evidence-and-lifecycle-repair-plan.md)，精简证据见 `evidence/motion_navigation/d110-evidence-lifecycle-v1/`。

## 1. 结果

| 项目 | 结果 |
|---|---|
| close 诊断真实关闭顺序 | 通过。调用线程在后端关闭前抓取诊断；诊断只随同一条已接纳并写出的 `close_release` 记录落盘 |
| 缺失或 `null` 诊断 | 通过。有上下文的 `ValueError`，不再泄漏无说明的 `TypeError` |
| 求解逐样本测量 | 通过。142 个样本都有墙钟、线程 CPU、三代 GC collection delta、试次身份；测量错误为 0 |
| Windows 直接与相邻检查 | 448／448 |
| Windows 源树完整正序／逆序 | 各 186 个模块、2333 项，零失败、错误、超时和启动错误 |
| 公开快照完整正序／逆序 | 各 185 个模块、2309 项，零失败、错误、超时和启动错误 |
| Java 门禁 | 源树 3／3，公开快照 3／3 |
| 公开快照校验 | 完整矩阵所用代码完成快照在测试前后均为 2310 个文件；补齐最终文档和精简证据后的发布快照为 2318 个文件，通过 `verify` |
| Linux settle／fd 最终字节复核 | 待 Linux 环境补充。Windows 只验证跨平台代码和测试，没有冒充 POSIX 结果 |
| 唯一一次 600 秒 B10 | 功能、安全、关闭归因通过；只有求解墙钟最大值门槛失败 |

## 2. 唯一一次 B10

正式运行目录为 `artifacts/fabric-deployment/20261015T110422093517Z-a092f04f`，源码提交为 `67f4f1d8`。第一次命令因 `JAVA_HOME` 没有指向项目 JDK 21，在启动游戏前结束，只算环境预检失败。设置项目 JDK 21 后运行了本阶段唯一一次正式 B10。

正式运行完成：

- validation 40／40；
- turn validation 40／40；
- 默认协调链 10／10；
- R4 移动入口 60／60；
- typed negative 24 个；
- ordered source 为 `explicitly_released`；
- cleanup failure 为 0；
- `complete_time_event_attribution` 通过；
- trace 接纳并写入 21209／21209 条，丢弃和不确定记录均为 0；
- 最后一个 close 诊断有效，`client_tick=5947`，`observation_sequence_id=5252`。

外层只报告一个失败：`client-0:b10b_solver_is_bounded`。

## 3. 求解离群

142 个求解样本的墙钟 P95／P99／最大值为 24.6028／27.6281／39.2484 ms。只有 `r4_validation_2-1-4` 超过 30 ms。它只检查 3 个候选，动作安全完成。

该样本同时发生一次 gen0 和一次 gen2 collection。它也是 142 个样本中唯一与 gen2 collection 重叠的样本。这个结果说明两者高度相关，但还不能证明 39.2484 ms 中有多少由垃圾回收造成。

Windows 的 `thread_time_ns` 在本轮主要只出现 0、15.625 和 31.25 ms，粒度太粗，不能精确分离求解、垃圾回收和系统调度。D110 因此保持原判定：墙钟最大值 `< 30 ms` 仍失败，不修改门槛，也不为了通过再跑 B10。

## 4. 生命周期边界

Linux 三方复核已经确认 D109 的 zombie、双 fork 和 fd 根因。D110 已把 settle 阶段改为：第一次正常清理仍给一次 `SIGTERM`，之后对身份匹配的已登记后代直接 `SIGKILL`。只有真正成为孤儿的进程才要求收养证据，fd 测试则在关闭测试自身的 stderr 副本后计数。

这些改动在 D110 签署时已通过 Windows 直接与相邻检查，POSIX 动态矩阵当时仍待 Linux 最终字节复核。后续三方 D110 审查完成 D5 矩阵 20／20、fork 链 10／10 和 D7 fd 门禁 10／10，并确认 185 个模块 cleanup 全过；D109-N1／N2／N4 与 D110-LINUX-01 已据此关闭。两项纯测试时序问题由 D111 采纳修正，原断言保持。详情见[缺陷台账](defect-ledger.md)的 D111 收尾记录；这些后续复核不改变 D110 的性能失败。

## 5. 证据边界

- Windows 是正式平台，Linux 只补充 POSIX 生命周期验收；
- 本阶段没有修改运动控制、规划、动作证明、输入窗口或伤害规则；
- 大型 trace、世界和本地分片报告不进入 Git；精简证据保存来源路径、字节数和 SHA-256；
- 冻结测试目前要求工作区同时保留部分 LF 与 CRLF 字节。D110 没有改写这些历史文件，行尾依赖另记为测试卫生债；
- 连续两跳、零等待交接、新动作和求解器优化仍未授权。
