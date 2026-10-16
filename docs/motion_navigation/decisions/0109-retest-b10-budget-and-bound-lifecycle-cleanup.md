# D109：先复验 B10 总预算，再修复有界清理

日期：2026-10-15。状态：**已签署；D0—D8 已完成已授权实现和 Windows 验收。最终 Fabric B10 的功能、安全、来源释放和 trace 封存通过；求解最大值与 time attribution 门槛未过，D109 按止损规则 FAILED／STOPPED。D5 的 Linux 动态矩阵和 D7 的 POSIX 文件描述符矩阵仍待三方补充复核。**

## 要解决什么问题

D108 已完成按范围物化、分片运行和异步诊断等基础设施工作。三方在 Linux 上复核后确认这些改动成立，同时纠正了 B10 失败的归因。

D108 的四次 B10 命令都没有传 `--timeout-seconds`。探针因此使用 240 秒默认值，并只给正式任务留下 215 秒。最近一次正式记录绑定到运行 `20261014T215836501478Z-3e0584b7`；原始 trials 的 SHA-256 为 `bcc7969d6bf71d90fb3f6ff9b8d7c3f0d039d0225ed82650df3178a491fbc379`。文件共有 106 行，其中 24 个是 R4 试次；最后一项是 movement tick 3171 的 `r4_validation_1-1-3`，随后在 tick 3208 失败。这个现象符合探针总预算耗尽，不能据此判断 R4 带速入口存在动作超时。

此外还有几项独立的生命周期问题：清理异常会覆盖主失败；异步 trace 可能让进程无法退出；Linux 分片器会把僵尸算作存活，也可能漏掉双 fork 后脱离原进程组的守护进程；POSIX worker 的 feeder 回调形成引用环。

## 决定

### 1. 先复验，不先改代码

任何生产代码修改之前，必须在当前 HEAD 上按原场景运行一次 B10，并显式传入：

```text
--timeout-seconds 600
```

运行必须记录所处阶段、当前试次、已完成 R4 试次数和剩余总预算。D1 不修改生产代码，因此外层 summary 必须按现行实现原样保存。内层失败只能从原始 Runtime trace、Python exception context 或明确的 deadline event 旁路提取，并单独注明来源。证据不足时写 typed `unavailable`，不能把推断结果冒充成 summary 的原生 primary failure。D2 完成后，新 summary 才原生分离 primary failure 和 cleanup failure。

- 如果完整完成，D108-C-01 归类为验收命令遗漏总预算参数。不得修改运动逻辑。
- 如果仍失败，先按具体阶段和 typed failure 登记。只有证据明确指向运动控制，才能另开运动根因阶段。
- 环境、端口或服务启动失败不算产品结果。修复环境后最多原样重跑一次。

### 2. 主失败始终优先

探针清理必须分别保存：

- 最先发生的主失败；
- 后续 cleanup failure；
- ordered source 的释放方式。

Runtime 仍为 READY 时，探针显式注销 ordered source。Runtime 已失败时，不再调用只允许 READY 状态使用的注销接口；由 `Runtime.close()` 的 `arbiter.clear()` 兜底清除，并把结果记为 `released_by_runtime_close`。cleanup failure 不能覆盖主失败的类型、消息和上下文。

这项修改不改变 Runtime 状态机。

### 3. 异步 trace 的关闭必须有期限

`BoundedAsyncTraceWriter.close()` 不能无限等待队列或 writer 线程。关闭返回有类型结果，至少包含：

- `status`：`SEALED` 或 `INCOMPLETE`；
- `sealed`、`worker_alive`；
- `accepted`、`written`、`dropped`、`indeterminate_or_pending`；
- `cleanup_failure`。

`accepted` 是成功进入异步队列的累计数；`written` 是 sink 已确认写入的数量；`dropped` 记录已知不会写入的记录；`indeterminate_or_pending` 记录 close 返回时仍无法判定结果的已接纳记录。实现必须保留足够的内部子计数，使已接纳记录满足 `written + accepted_dropped + indeterminate_or_pending = accepted`，不能用一个重叠总数伪造平账。

关闭还必须满足：

- 停止请求和 `join` 都有期限；
- sink 卡住时返回 typed cleanup failure，并标记证据未完整封存；
- writer 线程改为 daemon，不能因为 owner 漏调 `close()` 而阻止解释器退出；
- worker 仍在使用 sink 时，owner 不得并发关闭 sink；sink 的最终写入和关闭归 worker 独占；
- 重复调用 `close()` 必须有界且幂等；如果先前超时，返回最新关闭状态；
- 阻塞解除后，仍存活的 worker 必须观察停止请求，自行完成或记录失败并退出；
- Runtime 构造失败时，探针必须回滚并关闭已经创建的 trace；
- 主失败与 trace 收尾失败同时发生时，继续报告主失败。

守护线程只负责保证进程可以退出，不代表证据已经安全写完。正常路径仍必须显式关闭并核对封存结果。

### 4. Linux 分片器按最终事实判定

Linux 上已经退出但处于 zombie 状态的进程不算存活。清理完成后的状态必须根据最终的存活列表和错误重新计算，不能保留中途的失败状态。

Linux 父 runner 在启动模块根进程前启用 subreaper，并记录启用前已有的子进程。这样根进程快速退出或测试代码双 fork 后，孤儿会可靠交给仍存活的父 runner，而不是依赖 25 ms 轮询碰巧看见短暂后代。PID、创建时间、启动前子进程基线和本轮根进程时间窗继续共同限制清理范围，不能改成宽泛的 PID 杀进程。Windows 继续使用 Job Object，行为不变。

### 5. 语义门禁与性能门禁分开

快照单测在所有平台优先检查确定性工作量：结果正确、缓存有界，而且本次借用实际物化的 section 数只由请求范围决定，不随无关脏 section 增长。相对耗时只作补充诊断。绝对的 8／15／30 ms 门槛只在正式 Windows 性能量尺中运行，并使用足够样本。

POSIX worker 的 feeder 错误回调不得强引用 worker。回调使用 `weakref.ref(self)`。只有 feeder 已确认退出时，`close()` 才恢复原回调；close 超时后仍由弱引用回调服务晚退出的 feeder，不能提前撤掉。文件描述符不得依赖循环 GC 才释放。

## 边界

D109 只处理 B10 总预算复验、失败归因和基础设施生命周期问题。它不授权：

- 修改 R4、带速入口或运动控制器；
- 连续两跳、零等待交接或新动作；
- 放宽 UNKNOWN、伤害额度或动作证明规则；
- 回写 D106—D108 的历史失败证据。

正式验收平台仍是 Windows。Linux 用于补充验证 POSIX 生命周期和跨平台测试设计。

## D1 实施结果

代码修改前的基线提交为 `680d7a7654f3824e29662dcdba900b9e24313bce`。第一次启动因当前 shell 没有设置 `JAVA_HOME`，在游戏启动前结束；它单独记为环境 preflight，不构成产品结果。设置项目已有的 OpenJDK 21 后，使用同一条命令原样重跑：

```text
.venv\python.exe scripts\probe_fabric_deployment_observation.py --seed 21001 --server-port 25597 --ipc-port 8140 --b10-gap-solver-probe --time-diagnostics --physics-tick-diagnostics --ftick-diagnostics --timeout-seconds 600
```

正式运行完成 40／40 validation、40／40 turn validation、10／10 默认协调链和 60／60 R4；24 个 typed negative 也完整运行。最后一项是 `r4_validation_2-3-4`，最后 movement tick 为 5640。全程生命值保持 20，Runtime trace 没有非空 failure、exception 或 error，也没有 `deadline_exceeded` 或身体责任未解决记录。11 个 ordered source 全部显式注销。

因此 D108-C-01 已关闭为旧正式命令遗漏 600 秒参数。D109 不修改运动逻辑。

进程仍以 1 退出，但失败位于验收和收尾层：

- 外层原样报告 `DeploymentEvidenceFailure`，失败检查为 `b10b_solver_is_bounded` 和 `complete_time_event_attribution`；
- 142 个求解样本全部完成功能场景，P95／P99 为 24.5898／26.9761 ms；`r4_validation_2-3-0` 的 46.6656 ms 孤立最大值超过 30 ms 门槛；
- time attribution 得到 5637 次观察和 5636 个区间，未覆盖一个精确 Runtime episode；
- cleanup 独立报告 `asynchronous trace worker failed`，没有覆盖外层 primary failure。

精简证据位于 `evidence/motion_navigation/d109-b10-lifecycle-v1/`。137 MB Runtime trace 不进入 Git；证据目录保存来源运行、字节数、SHA-256 和独立结构化摘要。

## 完成条件

- 当前 HEAD 的 600 秒 B10 已保留原始外层结果，并从 raw source 得到有来源的 inner failure 或 typed `unavailable`；
- 主失败不会被 cleanup failure 覆盖；
- trace sink 卡住、遗漏关闭和构造失败都能有界退出；
- Linux 僵尸、快速崩溃和双 fork 矩阵均能有界清理；
- worker 重复创建关闭不依赖 `gc.collect()` 回收文件描述符；
- Windows 完整正序、逆序通过；
- Linux 补充矩阵通过，平台限定检查保持明确；
- 独立公开快照、证据索引和哈希可复核。
