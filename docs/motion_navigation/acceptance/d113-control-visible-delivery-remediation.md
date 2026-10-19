# D113 控制可见延迟与交付抖动验收

日期：2026-10-16。状态：**FAILED／STOPPED。** 授权实现和本地门禁完成，定向通过；唯一正式批次未完成，未追加运行。

## 1. 冻结口径

- 延迟：`first_control_visible_tick - worker_submit_tick`；
- 取走结果：单列 `result_take_tick`；
- 中性准备上限：5 tick；
- 生产控制准备：完整 observation received → dispatch，P95 ≤ 8 ms、P99 ≤ 15 ms、最大值 < 30 ms；
- 同步探针：只报告；
- D112 历史记录：不回写。

## 2. 正式结果与停止原因

唯一正式批次为 `20261016T140216750902Z-8111510c`，启动提交 `b6996050`。运行使用 Windows 项目 Java／Python 环境，外层及客户端期限均为 600 秒。

| 场景 | 结果 |
|---|---|
| baseline | 20／20 完成 |
| 2-tick 首次控制可见 | 8／8 完成 |
| 5-tick 首次控制可见 | 8／8 完成 |
| late1 | 8 项显式记录为 `fabric_input_late1_injection_unavailable` |
| 10-tick | 首场等待观察失败，剩余 7 场未运行 |
| never-return | 4 场未运行 |
| 后续 R4／typed moving negative | 60／24 项未运行 |

首个 10-tick 场景 `coordinator-worker-delay-10-0-0` 在 Runtime step 2243 等待观察时以 `TIMED_OUT／DEADLINE_EXCEEDED` 结束，底层类型为 `TimeoutError`。提交、receiver 和 publish 均为 tick 2237，目标首次可见为 2247；实际首次可见及 take 均缺失，注入状态为 `incomplete`。该场景不能计作精确注入成功，也不能计作安全有界终态。

最后成功观察为 tick 2242：位置 `(0.5, 0, 7.5)`、着地、health 20、水平速度为 0，已记录的 6 个该场景输入均为纯中性。最后几步沿用同一个 action deadline `1174229395744000`，下一次观察在这个期限报告超时。600 秒外层期限没有到达。现有证据只能定位到该动作期限和观察失败，不能在本次失败后推断或签署新的修复。

客户端记录 `IllegalStateException:cause=EOFException:source=ProbeTransport.java:109`。该位置表示对端关闭传输，不能证明客户端先发生异常。没有使用单场允许的一轮客户端／夹具修正，也没有在正式批次后改代码或重跑。

36 个完成协调器动作均零伤害、来源显式释放；72 个 verified 输入的授权窗口错过为 0。2／5 tick 的 32 次作业交付全部 `matched`，实际 submit→first-visible 可重算且恰好等于请求延迟。全部 54 个 identity 中 53 条链完整，唯一缺端点的是上述未完成 10-tick 作业。没有重复事件或端点倒序。

失败场景来源未显式释放；默认 ordered source 最终只在 Runtime close 时清除。清理失败为 0，客户端 graceful close、返回码 0，supervisor 登记的进程全部停止，端口释放。异步 trace 接纳／写出 9755／9755，丢弃和不确定记录均为 0。上述清理结果不能替代缺失的动作安全终态。

原始部分摘要明确 `partial=true`、`matrix_complete=false`。其中 `formal_acceptance_eligible=true` 只标识这是默认矩阵入口，不表示验收通过；本次签署结果仍为失败。原始摘要、trace、端点和生命周期的精确 gzip 副本见 `evidence/motion_navigation/d113-control-visible-v1/formal-default/raw/`，分析见同目录 `analysis.json`。

### 已记录部分的性能

| 范围 | 样本数 | P95 ms | P99 ms | 最大 ms |
|---|---:|---:|---:|---:|
| 正式生产协调器，含等待帧 | 755 | 4.1105 | 4.4411 | 4.6834 |
| 同步探针完整 received→dispatch，只报告 | 82 | 28.5225 | 36.2924 | 36.2924 |
| 其他夹具，只报告 | 1405 | 3.2329 | 9.0613 | 42.9134 |

生产组按 episode、结果观察编号唯一关联，并核对 control 和决策观察编号；关联、时间缺失和无效区间均为 0。该组满足 8／15／30 ms，但正式矩阵没有完成，因此不签署 D113 整体或关闭 D110-SOLVE-01。82 个同步求解的原始墙钟 P95／P99／最大为 26.6738／34.4710／34.4710 ms，继续只报告。

### 定向与本地门禁

唯一初始定向为 `20261016T135933394751Z-02977bdb`，启动提交 `53cbc89f`。只执行 `coordinator-worker-delay-5-0-0`。两个 solve／revalidate 作业分别为 17→22、22→27，首次控制可见延迟均为 5、注入均 `matched`，完整身份链与执行动作一致。任务完成、着地、零伤害、Runtime ready、来源显式释放。29 个生产帧 P95／P99／最大为 4.1572／4.8543／4.8543 ms。单场摘要明确 `matrix_complete=false`、`formal_acceptance_eligible=false`。证据见 `targeted-5tick/`。

固定 33 场和 180 个固定种子抖动场景已通过本地检查。整合扩大组合 292／292；Windows 线程 CPU 零采样的限定测试修正后，直接／相邻 83／83。六组变异检查均捕获，包括旧交付估计、旧中性预测、吞掉 transport cause、选旧 generation、混用容量原因、串性能组、放宽选择器、信任缓存延迟、伪造正 CPU 值等。

最终测试提交为 `f161a0c5`，生产语义自 `2ccd237a` 后未变。

| 门禁 | 结果 |
|---|---|
| Windows 源树正序／逆序 | 各 191 模块、2387 项 |
| 独立公开快照正序／逆序 | 各 190 模块、2363 项 |
| 四组失败／错误／超时／跳过／清理失败 | 全为 0 |
| 源树 Java／真实离线构建 | 13／13 |
| 快照纯 Java | 3／3 |
| 矩阵前后快照 verify | 各 2386 文件 |

原工作树的冻结字节、缺失历史 `.tmp`、Python 环境和旧 jar 来源问题导致的中断均单独保留。仅恢复有冻结依据的 Windows 字节和物理依赖；没有改历史期望哈希。首次公开快照遇到合法线程 CPU=0 的旧测试假设，确定性复现后只改测试：允许零 CPU 采样，同时保留墙钟、工作量与设备准入断言，四组最终矩阵全部重跑。原失败不计入最终全绿数量。完整命令、逐文件字节依据和原始分片见 `windows-regression/`。

D112 保持 FAILED／STOPPED，历史证据不变。D110-SOLVE-01 保持打开。连续两跳、零等待交接和新动作仍未授权。

## 3. D113-2 本地诊断与量尺

诊断 schema v4 新增 `first_control_visible` 与 `result_take`。旧 `first_control_consume` 仍表示 legacy take 或 prepared READY 的首次消费。prepared READY 不移除结果，其 `access_kind=prepared_ready_query`、`removes_result=false`，不生成 `result_take`。三类事件按 identity 去重，历史长度受 inbox 原有 `MAX_HISTORY` 限制。

提交记录分别写实际 `worker_submit_tick` 和 `job_anchor_movement_tick`。prepared 请求没有 job anchor 时保留空值，并单列其预测入口状态的 tick；不能把预测入口或 anchor 当成实际提交 tick。

诊断时钟优先使用 `publish_diagnostic_movement_tick()` 显式发布的控制 tick，未发布时才回退到当前身体事实；读取诊断时钟不更新 admission 使用的身体事实。真实 prepared 夹具在身体事实停留于 10、显式发布 11 时，实际提交、登记、首次可见和 prepared 消费均记为 11，提交到可见延迟为 0。

prepared wire 当前仍缺少 `receiver_observed_result` 事件，其完整交付链不用于本阶段签署。未来签署前必须由父侧 active record 的 job token 关联 identity 后补齐；不能解码或信任 worker 自报 identity 来绕过父侧绑定。当前冻结的 gap Fabric 使用 legacy 路径，本轮不扩展 prepared wire 协议。

Fabric 夹具让真实 receiver 正常发布结果，只在本次 typed inbox 实例的 `peek`、`take` 和 `query_prepared` 上限制首次读取。每份作业记录请求延迟、目标 tick、实际首次可见 tick 和注入状态。`worker_not_ready`、`control_tick_skipped`、`old_generation` 和 `incomplete` 均不计作精确注入成功。never-return 仍在 worker 端口保留结果，不用读取门禁代替未完成请求。

新的分析器为 `scripts/analyze_d113_delivery_baseline.py`。生产帧按 episode、结果观察编号唯一关联，并核对存在的 control／决策观察编号。等待帧同样参与完整 received→dispatch 统计；重复、缺失、歧义或无效时间阻止性能签署。同步探针和其他夹具分别报告。

对 D112 最终本地原始目录的只读重算已写到 `evidence/motion_navigation/d113-control-visible-v1/d112-read-only-baseline.json`：

| 范围 | 样本数 | P95 ms | P99 ms | 最大 ms |
|---|---:|---:|---:|---:|
| 生产协调器，含等待帧 | 574 | 3.3017 | 3.5172 | 3.6088 |
| 同步探针 | 82 | 22.8508 | 29.9488 | 29.9488 |
| 其他夹具 | 1344 | 2.5175 | 5.5730 | 26.3139 |

四个源文件的读前、读后 SHA256 一致。结果同时记录分析器字节哈希和实施前提交。D112 未记录首次可见事件，因此其对应字段保留空值，旧 consume 延迟另列，不能补造 D113 端点。该重算不是新的性能采样或正式 D113 签署，D112 保持 FAILED／STOPPED。

协调器矩阵异常退出时，外层 finally 从已保留的探针试次生成 `b10-gap-solver-partial-summary.json`，包括 planned／completed 数量、原始求解分位数和 GC 诊断。部分摘要明确写 `partial=true`、`matrix_complete=false`，原始异常继续上抛，清理失败单列。本任务未运行 Fabric。

首个候选的本地检查为 9 个直接／相邻模块、181／181 项通过，包含固定 33 场和已有 180 场抖动。18 项新增专项覆盖上述端点、门禁、关联及部分摘要；去掉可见性门禁和把同步探针混入生产组的两个变异均被断言捕获。该结果仅签署本地实现与测试，不签署实机注入或 D113 整体阶段。

Task 2 复核首轮修正补了真实 prepared 提交时钟检查，并把 D113 基线加入公开导出 glob 和 required path。导出测试同时检查清单要求与独立导出副本的原始字节，避免验收文档引用的证据在快照中缺失。

首轮修正的定向检查为 3／3；扩大检查为 187 项、186 通过、1 失败。唯一失败是旧导出桥仍用 D112 已发布的 coordination 哈希比对 D113 当前源码；该差异从 Task 1 已存在，Task 2 候选 `d0e62f83` 也已存在，本轮未改该文件。D112 历史记录不回写。最终集成时必须新增 D113 当前版本语义证明并让桥读取现行阶段，完整检查恢复全绿后才可继续签署。
