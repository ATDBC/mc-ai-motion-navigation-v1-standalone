# D109：B10 总预算复验与生命周期修复计划

日期：2026-10-15。状态：**D0—D8 已完成已授权实现和 Windows 验收。最终 Fabric B10 的功能、安全、来源释放和 trace 封存通过；求解最大值与 time attribution 门槛未过，D109 按止损规则停止为未通过。Linux 补充复核仍待三方完成。** 决定见 [D109](../decisions/0109-retest-b10-budget-and-bound-lifecycle-cleanup.md)，结果记录在[验收](../acceptance/d109-b10-budget-and-lifecycle-repair.md)。

## 1. 当前事实和范围

D108 的基础设施目标已经通过 Windows 正式验收，并由三方在 Linux 上复核了主要行为。D109 不重做这些工作。

本阶段冻结下面的 RED：

| 项目 | 当前事实 |
|---|---|
| B10 命令 | 未传 `--timeout-seconds`，使用 240 秒默认值 |
| 正式任务预算 | 探针扣除启动与收尾余量后为 215 秒 |
| 绑定运行 | `20261014T215836501478Z-3e0584b7` |
| trials 身份 | SHA-256 `bcc7969d6bf71d90fb3f6ff9b8d7c3f0d039d0225ed82650df3178a491fbc379`，共 106 行 |
| 最近停止点 | movement tick 3208 |
| 已完成 R4 | 24 个 `r4_validation_*` 试次；最后一项是 tick 3171 的 `r4_validation_1-1-3` |
| 外层报告 | cleanup 的 `formal Runtime must be ready` 覆盖了原始 `deadline_exceeded` |
| ordered source | 显式注销失败；`Runtime.close()` 最终通过 `arbiter.clear()` 清除 |
| trace writer | 满队列、阻塞 sink 或漏调 `close()` 时可能无限等待或阻止进程退出 |
| Linux 分片器 | zombie 会被算作存活；双 fork 守护进程可能逃出已登记进程组 |
| 快照性能单测 | 3 个样本的绝对毫秒断言在 Linux 全量负载下不稳定 |
| POSIX feeder | 错误回调强引用 worker，文件描述符等待循环 GC 才释放 |

连续两跳、新动作、运动控制器修改和证明语义调整不在范围内。

## 2. D0：先修正文档口径

### 修改文件

- `AGENTS.md`
- `docs/motion_navigation/decisions/0108-separate-world-invalidation-from-snapshot-materialization.md`
- `docs/motion_navigation/stages/d108-snapshot-and-bounded-verification-hardening-plan.md`
- `docs/motion_navigation/acceptance/d108-snapshot-and-bounded-verification-hardening.md`
- `docs/motion_navigation/acceptance/defect-ledger.md`
- 本决定、计划和验收骨架

### 要求

- D108-C-01 改成“B10 探针总预算待复验”，不再写成 R4 动作超时；
- C-02 写清 ordered source 会由最终 `Runtime.close()` 兜底清除；
- 正式 B10 命令显式包含 `--timeout-seconds 600`；
- 历史 215 秒失败、tick 3208 和 24 个 R4 试次保持原样。

### 完成标准

现行入口、缺陷台账和 D108 收尾记录使用同一口径。旧证据文件不改写。

## 3. D1：当前 HEAD 的 600 秒 B10 诊断

这是任何代码修改前的强制步骤。使用当前 HEAD、原种子、原端口、原服务器和原诊断开关：

```text
.venv\python.exe scripts\probe_fabric_deployment_observation.py --seed 21001 --server-port 25597 --ipc-port 8140 --b10-gap-solver-probe --time-diagnostics --physics-tick-diagnostics --ftick-diagnostics --timeout-seconds 600
```

### 记录内容

- 完成的 validation、turn validation、默认协调链和 R4 试次数；
- 最后阶段、试次编号和 movement tick；
- 总预算、已用预算和剩余预算；
- 外层 summary 原样副本；
- 从原始 Runtime trace、Python exception context 或明确 deadline event 独立提取的 inner failure，以及对应来源；证据不足时写 typed `unavailable`；
- cleanup failures 和诊断封存状态；
- 零伤害、身体责任和 ordered source 的最终状态。

### 分支判定

1. **完整完成：** C-01 关闭为验收命令问题。后续不修改运动逻辑。
2. **再次耗尽总预算：** 记录进度是否继续增长。D109 不延长预算，也不直接归因运动。
3. **具体 typed failure：** 只有 raw source 直接支持时才采用；否则记为 `unavailable`。保留原始 summary，并按发生阶段登记。只有证据指向基础设施时才在 D109 修复。
4. **环境失败：** 不计入产品结论。修复环境后最多原样重跑一次。

### 止损

如果需要先改代码才能让这次诊断启动，停止 D1，先报告环境或夹具阻塞。不能把修改后的结果冒充当前 HEAD 基线，也不能在旁路分析中改写旧 summary 的 `primary_failure`。D2 之后的新 summary 才负责原生分离主失败和清理失败。

### D1 结果

基线提交 `680d7a76` 的 600 秒运行完成了整个功能和安全矩阵：40／40 validation、40／40 turn validation、10／10 默认协调链、60／60 R4 和 24 个 typed negative。最后 movement tick 为 5640，零伤害、零身体责任未解决，11 个 ordered source 全部显式注销。D108-C-01 关闭为旧命令预算不足，不进入运动根因修复。

阶段仍继续，因为同一运行还有三项独立失败：求解最大值 46.6656 ms 超过 30 ms、time attribution 没覆盖一个精确 Runtime episode、异步 trace cleanup 失败。外层原始结果和独立 raw trace 摘要分别保存；raw trace 中没有 inner Runtime failure。精简证据见 `evidence/motion_navigation/d109-b10-lifecycle-v1/`。

## 4. D2：保留主失败并正确释放来源

状态：**已完成，含 D2-R。** B10 包装层现在先保存主执行异常，再分别收集 ordered source 注销、worker 关闭和证据封存错误。Runtime 不是 READY 时不会调用 READY-only 注销接口；正式 client owner 在 Runtime 引用清空前保存 close 结果，结果明确区分 `explicitly_released`、`cleared_on_runtime_close` 和 `not_released`，不在已关闭 Runtime 上调用业务查询。外层 summary 原生保留 `primary_failure` 与 `cleanup_failures`，cleanup-only 失败不会制造假的 primary failure。F-TICK 只接受显式注销或 close 兜底清除。实现和检查结果见验收第 3 节。

### 预计修改文件

- `scripts/b10_gap_solver_runtime.py`
- `scripts/probe_fabric_deployment_observation.py`
- `tests/test_b10_runtime_probe.py`
- 对应的 Fabric 探针检查

### 先写失败测试

- Runtime 已 FAILED 时，显式注销抛错，但报告仍保留原 primary failure；
- cleanup failure 单独进入列表；
- Runtime READY 时仍显式注销；
- Runtime 非 READY 时由 `close()` 清除 registry，并记录 `released_by_runtime_close`；
- close 本身也失败时，主失败仍不被替换。

### 实现

把主逻辑、来源释放和诊断封存拆成独立的 `try`／`finally` 边界。探针只在 Runtime READY 时调用 ready-only 注销接口。其余状态直接进入有界 close，并单列释放方式。

### 完成标准

summary 的 `primary_failure` 与最初异常类型和消息一致；cleanup failures 保留但不改变退出根因。Runtime 状态机和 ordered registry 公共契约不变。

## 5. D3：修复 Linux zombie 判定

状态：**实现完成，Windows 与跨平台单元门禁通过；最终 Linux 动态字节等待三方复核。**

### 预计修改文件

- `scripts/process_tree.py`
- `scripts/run_motion_nav_shards.py`
- `tests/motion_nav/test_d107_shard_runner.py`

### 先写失败测试

- 忽略 SIGTERM 的孙进程被强杀后短暂成为 zombie；
- 最终 `surviving=[]`、`errors=[]` 时，cleanup 状态必须恢复为 `passed`；
- 模块本身通过且只留下 zombie 时，不得误判为测试错误；
- mutation：重新把 zombie 当存活时，测试必须失败。

### 实现

统一存活判断，排除 `psutil.STATUS_ZOMBIE`。`_refresh_cleanup_liveness` 每次都根据最终 surviving 和 errors 重新计算状态。

### 完成标准

Linux 对抗场景至少连续 30 次通过；Windows Job Object 路径结果不变。

### 实施结果

存活判断现在要求 PID 与创建时间仍匹配，并把 `STATUS_ZOMBIE` 视为已经停止。检查状态失败时仍按存活处理，不能因为 `AccessDenied` 误报清理成功。最终复查会同时读取 surviving 和 errors，重新得到 `passed` 或 `failed`，不再单向保留较早的失败状态。

Windows 的 24 项分片器检查通过，真实超时场景仍在 11 秒期限内结束，Job Object 路径没有行为变化。30 次最终状态重算、zombie、真实 survivor、cleanup error 和 PID 复用由不依赖平台的单元检查覆盖。三方提出的 Linux 对照补丁曾连续 30 次通过；项目最终字节仍须在 Linux 重跑同一动态场景，不能用 Windows 结果代替。

## 6. D4：让异步 trace 有界退出

状态：**已完成。** writer 使用独立停止事件，不再往可能已满的队列塞停止标记。worker 独占 sink，并使用 daemon 线程。`close()` 只发停止请求并在同一个单调时钟期限内等待；超时返回 `INCOMPLETE`，不会在 worker 仍写入时从 owner 线程关闭 sink。阻塞解除后 worker 会继续排空已接纳记录、写 summary、关闭 sink 并退出；下一次 `close()` 返回最新 `SEALED` 结果。

`AsyncTraceCloseResult` 已冻结 `status`、`sealed`、`worker_alive`、`accepted`、`written`、`dropped`、`indeterminate_or_pending`、`cleanup_failure`，并另存入队前丢弃、入队后确定丢弃和写入结果不确定的数量。Runtime 从构造开始取得 trace 所有权；中途构造失败会有界回滚，正常关闭会把未封存结果记入 cleanup failure。主构造异常仍是抛出的 primary，trace 回滚问题只作为 note 保留。

### 预计修改文件

- `mc2p/runtime/async_trace.py`
- `scripts/probe_fabric_deployment_observation.py`
- `tests/test_async_trace.py`
- `tests/test_fabric_deployment_probe.py`

### 先写失败测试

- 队列已满时，`close()` 不得无限阻塞在停止标记；
- sink 永久阻塞时，`close()` 在期限内返回 typed failure；
- owner 漏调 `close()` 时，解释器仍能退出；
- Runtime 构造抛错时，已经创建的 trace 被回滚；
- 主失败与 trace 失败同时发生时，主失败优先；
- mutation：去掉 close 期限、改回非守护且不回滚时，测试必须失败。

### 实现边界

- 使用独立停止事件或有期限的停止标记提交；
- `close(timeout_seconds=5.0)` 使用同一个单调时钟期限覆盖停止请求与 `join`；测试可以注入更短期限；
- writer 线程改为 daemon，不能阻止解释器退出；
- `close()` 返回冻结的 `AsyncTraceCloseResult`：`status`、`sealed`、`worker_alive`、`accepted`、`written`、`dropped`、`indeterminate_or_pending` 和 `cleanup_failure`；
- sink 自构造后由 worker 独占；worker 仍存活时，owner 不并发调用 `sink.close()`；
- 重复 close 有界且幂等，返回当时最新状态；阻塞解除后 worker 必须自行完成关闭并退出；
- 账目必须能区分入队前丢弃与入队后丢弃，并满足已接纳记录的平账关系；
- 无法封存时返回“证据不完整”，不能伪报成功；
- 正常路径继续显式 close，不依赖解释器退出兜底。

### 完成标准

阻塞 sink、满队列和构造失败场景都在冻结期限内结束，且没有覆盖业务主失败。测试还要覆盖“第一次 close 超时，解除阻塞，worker 自退，第二次 close 得到 `SEALED` 或明确失败”的全过程。

### 实施结果

容量为 1 的冻结场景中，2 条记录成功入队、1 条在入队前被拒绝。sink 阻塞时，第一次和重复的零期限 close 都返回 `INCOMPLETE`：`accepted=2`、`written=0`、`dropped=1`、`indeterminate_or_pending=2`、`worker_alive=true`，sink 尚未被 owner 关闭。解除阻塞后，第二次有期限 close 返回 `SEALED`：`written=2`、`indeterminate_or_pending=0`、`worker_alive=false`，再次 close 得到相同结果。故意漏调 close 的独立子进程能自行退出；Runtime 构造前置失败和构造中途失败都会关闭已取得的 trace。

写入调用先产生副作用再抛错时，当前记录记为 `accepted_indeterminate`，不会冒充“确定不会写入”的 `accepted_dropped`；后续尚未尝试而被排空的记录仍记为确定丢弃。20 个线程同时 close 的检查中，所有调用都在期限内得到同一个 `SEALED` 结果，sink 只关闭一次。F1 组合 trace 在 diagnostics 构造失败时也保留原异常；trace 回滚不完整或抛错只作为 note 附加。

Windows 直接与相邻检查共 128 项通过。它覆盖异步 writer、Runtime V1、正式 Fabric 探针包装器、D108 receiver、F1 跟随包装器和 host 收尾。D4 没有运行 Fabric，也没有改变运动、期限或动作逻辑；新的正式 B10 仍留到 D8。

D8 首次最终 B10 补到了上述单元矩阵没覆盖的真实组合：`BoundedAsyncTraceWriter` 先把 payload 投影为 `dict`，`CloseDiagnosticsTrace.write()` 却仍按 typed object 读取 `backend_result.observation`，导致 trace 未封存。第二轮限定修正统一为单次投影契约：`JsonlTraceWriterV0.write_projected()` 直接写已投影值，close diagnostics 从该结构读取证据；缺少 observation 时保留 formal record，不伪造 diagnostics。真实组合 RED 为 0／2，修正后 2／2；直接与相邻 134／134 通过、零跳过。修正后 B10 只允许再跑一次，若仍失败就停止报告。

## 7. D5：收住 Linux 双 fork 后代

状态：**实现完成，Windows 与跨平台门禁通过；Linux 动态矩阵等待三方复核。** Linux 父 runner 在启动任何模块前启用 `PR_SET_CHILD_SUBREAPER`，并用 `PR_GET_CHILD_SUBREAPER` 读回确认。启用前后如果已经存在无法归属的后代，runner 会在启动模块前明确失败。

每轮模块使用一个串行独占窗口。父 runner 先登记 root 的 PID 和创建时间，再放开 start gate。运行中同时扫描 root 后代和被 subreaper 收养的直接子进程。收养身份必须通过两次 PPID 核对、PID／创建时间核对、本轮 root 时间窗和启动前基线检查。清理会继续扫描到两轮稳定，并只对已登记的精确收养 PID 调用 `waitpid`。`AccessDenied`、身份变化和所有权无法证明都会让 cleanup 失败，不能扩大 kill 范围。Windows Job Object 路径保持原样。

### 预计修改文件

- `scripts/run_motion_nav_shards.py`
- `scripts/process_tree.py`
- `tests/motion_nav/test_d107_shard_runner.py`

### 检查矩阵

- 根进程启动后立即崩溃；
- 一次 fork 后 `setsid()`；
- 双 fork 后中间进程立即退出；
- 守护进程忽略 SIGTERM；
- 孙进程持有 stdout／stderr；
- 所有场景先确认预期 PID 已写出，再判断清理结果。

### 实现

Linux 父 runner 在启动任何模块前调用 `prctl(PR_SET_CHILD_SUBREAPER)`，并记录启用前已有的直接子进程。根进程快速退出或双 fork 后，孤儿由持续存活的父 runner 接管。清理授权同时核对：不是启动前已有子进程、PID 与创建时间匹配、创建时间落在本轮根进程时间窗内，且由 subreaper 接管或可追溯到本轮根进程。不能只靠轮询短暂后代。

Linux 无法启用 subreaper 时，分片器必须明确报告能力不足，不能宣称完整进程树已经受控。Windows 不启用这条路径，继续由 Job Object 保证归属和清理。

### 完成标准

上述矩阵全部在总期限内返回；登记后代全部退出并被回收；不能误杀未登记的同机进程。

### 实施结果

实现前的六项身份和所有权检查因接口不存在而 6／6 报错。实现后，Windows 分片器模块 32／32、Fabric 探针相邻模块 31／31，共 63 项通过。检查覆盖：subreaper 启用与读回、能力不足时启动前失败、已有子进程拒绝、串行独占窗口、PPID 二次核对、时间窗、伪造 PID、PID 复用、`AccessDenied`、精确 `waitpid` 和 Windows Job 回归。

同一测试还定义了 Linux 动态矩阵：root 快速 `os._exit()`、`setsid()`、双 fork 且中间进程立即退出、忽略 SIGTERM 并持有输出、清理期间再次 fork，以及 zombie 回收。Windows 不执行这些 Linux 系统调用，只验证矩阵内容，因此当前不能写成 Linux 已通过。三方须在最终提交上运行该模块；预期每个场景先确认 PID 文件已经写出，再要求 `cleanup=passed`、`surviving=[]`、所有 PID 均不存在。

## 8. D6：拆开快照语义检查和绝对性能检查

状态：**已完成。** `WorldKnowledge` 暴露最近一次 section 提交的只读工作量诊断。它只保存六个整数，不保留历史，也不遍历额外对象。跨平台检查直接比较 requested、selected、materialized、committed 和前后 dirty 数量，不再用单次墙钟耗时代替范围隔离。

### 预计修改文件

- `tests/motion_nav/test_d108_snapshot_materialization.py`
- `scripts/benchmark_d108_snapshot_materialization.py`
- D108／D109 验收文档

### 跨平台门禁

- 快照结果与从头构造一致；
- 旧借用保持不可变；
- 缓存和脏集合有界；
- 用计数器确认：请求范围只含 1 个 section 时，无论无关脏 section 是 4、16 还是 32，本次构造／提交的 section 数都相同；漏做范围过滤的变异必须失败；
- 至少 20 个样本的相对耗时只作补充报告，不作为跨平台阻塞门槛。

### Windows 性能门禁

绝对 8／15／30 ms 只在正式 Windows 基准中判断，至少采集 20 个样本并保存原始分布。Linux 只报告补充耗时，不用 3 个样本的最大值阻塞完整测试。

### 完成标准

Linux 负载不再造成虚假红灯；Windows 的正式绝对门槛没有被删除或放宽。

### 实施结果

请求只覆盖 1 个 section 时，无关脏 section 分别为 4、16、32，三组可观察工作量都为 `requested=1`、`selected=1`、`materialized=1`、`committed=1`。旧借用仍保持旧方块，当前借用得到新方块；快照缓存和 dirty 集合继续受 live section 数限制。把提交范围故意扩大为全部 dirty section 时，工作量门禁和原构造门禁都会失败。

跨平台测试运行 20 个样本，只保存相对耗时分布，不按毫秒阻塞。正式 Windows benchmark 原有 P95≤8 ms、P99≤15 ms、最大值<30 ms 保持原代码和边界。干净提交 `9e7228d4` 的正式 Windows CLI 20 样本通过；4／16／32 个无关脏 section 的 P95 分别为 3.5413／3.5107／3.4939 ms，B10 范围 P95／P99／最大值为 1.6893／1.6937／1.6937 ms。每个场景的 20 项 `raw_samples_ms`、命令、UTC 时间、HEAD 和 SHA-256 均保存在 `evidence/motion_navigation/d109-b10-lifecycle-v1/d6-snapshot-materialization/`，可以独立重算；不把单次机器结果写成跨平台结论。

快照物化直接检查 12／12；世界快照、Runtime 激活和动作交接相邻检查 105／105；合计 117／117。D6 没有改变快照事实、借用身份或生产控制分支。

## 9. D7：解除 POSIX feeder 引用环

### 预计修改文件

- `mc2p/motion_nav/motion_worker.py`
- worker 生命周期测试

### 先写失败测试

- 关闭自动 GC，重复创建关闭 10 次；
- 删除最后一个强引用后 worker 可回收；
- 文件描述符、feeder 线程和子进程不增长；
- idle、killed、队列满、8 MiB 消息和 worker crash 全覆盖；
- Windows overlapped 关闭路径保持原结果。

### 实现

feeder 错误回调使用 `weakref.ref(self)`。只有 feeder 已确认退出时才恢复原回调；close 超时而 feeder 仍存活时保留弱引用回调。错误计数继续落到仍存活的 owner；owner 已释放时只走安全兜底。

### 完成标准

不调用 `gc.collect()` 也没有文件描述符增长。另覆盖“close 超时、feeder 稍后退出”：晚退出不抛未处理异常，回调最终解除，描述符回到基线。POSIX 动态矩阵和 Windows 生命周期回归通过。

### 实施结果

Queue 先保存原 feeder error handler，再安装只捕获 `weakref.ref(worker)` 和原 handler 的回调。owner 存活时继续按原规则区分预期的 `EPIPE`／Windows 109 与非预期错误；owner 已释放时转交原 handler。`close()` 只有在 feeder 不存在或确认退出、且 Queue 当前 handler 仍是本 worker 安装的对象时才恢复。close 超时且 feeder 仍存活时保留弱回调；feeder 晚退出后第二次 close 可以恢复。没有增加 watcher，也没有改变 Windows overlapped 取消与 endpoint 关闭顺序。

失败测试先证明旧实现的闭包直接捕获 worker，且构造失败后 handler 没有恢复。修复后覆盖 idle、killed、队列满、8 MiB 消息和 worker crash；关闭自动 GC 后共创建关闭 50 个真实 worker，worker 弱引用立即清空，QueueFeederThread 和子进程回到基线。close 超时场景确认活 feeder 期间不恢复，晚退出后恢复；构造失败也会恢复原 handler。Windows 直接检查 40／40、相邻检查 84／84，合计 124／124。

Windows `num_handles()` 只作诊断：多进程库持有的 process／semaphore handle 不等同于这个 Python 引用环，因此本切片不借此扩大到另一项生命周期改造。POSIX 下关闭自动 GC 后的文件描述符零增长仍需 Linux 最终字节动态矩阵确认，当前不写成已通过。

## 10. D8：最终回归、实机和公开证据

### Windows 正式矩阵

1. D2—D7 直接检查；
2. D107、D108 相邻回归；
3. `tests/motion_nav` 分片正序、逆序；
4. Java 门禁；
5. 独立公开快照正序、逆序和 `verify`。

要求失败、错误、超时和未登记跳过均为 0。Windows 单模块默认 300 秒保持不变。

### Linux 补充矩阵

- POSIX Queue 5 场景 × 20 次 × 3 轮；
- zombie、快速崩溃、双 fork 和持管道场景；
- feeder 无 GC 文件描述符矩阵；
- 完整 `tests/motion_nav`，POSIX 单模块上限 600 秒；
- 两项 Windows-only 检查继续登记为平台限定，不能记为产品失败。

### 最终 B10

D2 和 D4 修改了探针收尾，完成回归后再用同一条 600 秒命令运行一次。结果与 D1 基线分别保存，不能合并或回写。

### 公开证据

公开包保存命令、环境、摘要、清单、哈希和必要的结构化轨迹。大体积原始 trace 按现行规则保留在本地或发布附件，公开索引写明边界。

Windows 回归已在干净提交 `cb06b448` 上完成：直接与相邻检查 425／425；源树正序、逆序各 186 个模块、2331 项；独立公开快照正序、逆序各 185 个模块、2307 项；源树和快照 Java 门禁各 3／3。两轮快照 `verify` 均通过 2299 个文件。失败、错误、超时和跳过均为 0。精简证据见 `evidence/motion_navigation/d109-b10-lifecycle-v1/d8-windows-regression/`。

最终 B10 证据提交后，干净提交 `53b6ffa5` 又按同一顺序完成严格复跑：直接与相邻 427／427；源树正逆各 186 个模块、2331 项；独立公开快照正逆各 185 个模块、2307 项；Java 各 3／3；两次快照 `verify` 均为 2305 个文件。全部零失败、错误、超时和跳过。本次只确认最终字节与公开闭包，没有再次运行 Fabric。

最终 Fabric B10 已在 `d9c564d4` 上按同一条 600 秒命令运行。功能、安全、ordered source 释放和异步 trace 封存通过；求解最大值 41.0339 ms 和 time attribution 的 `NoneType` 错误仍使批次失败。D2、D4 和 D108-C-03 已由正式链关闭；D109 按两轮限定修正规则停止，不再做第三轮修正或重跑。精简证据见 `evidence/motion_navigation/d109-b10-lifecycle-v1/d8-final-b10-post-repair/`。

## 11. Fail-fast 和停止条件

- D1 未取得有效当前 HEAD 结果前，不修改生产代码；
- 每个切片最多两轮限定修正；
- 新旧成功出现清单外退步，立即停止该切片；
- 清理方案需要修改 Runtime 状态机、运动控制器或证明语义时，停止并另写决定；
- Linux subreaper 无法建立可靠所有权时，降低 POSIX 声明，不能扩大 kill 范围；
- D109 完成前，不恢复连续两跳、零等待交接或新动作。
