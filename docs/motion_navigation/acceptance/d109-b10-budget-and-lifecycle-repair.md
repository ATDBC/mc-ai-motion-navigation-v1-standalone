# D109：B10 总预算复验与生命周期修复验收

日期：2026-10-15。状态：**D0—D8 已完成已授权实现和 Windows 验收。最终 Fabric B10 的功能、安全、来源释放和 trace 封存通过；求解最大值与 time attribution 门槛未过，D109 停止为未通过。后续三方 D110 Linux 复核已关闭 D109-N1／N2／N4，历史性能失败保留。**

本文件记录 [D109](../decisions/0109-retest-b10-budget-and-bound-lifecycle-cleanup.md) 的结果。实施顺序和止损条件见[阶段计划](../stages/d109-b10-budget-and-lifecycle-repair-plan.md)。D106—D108 的历史证据不回写。

## 1. RED 基线

| 项目 | 冻结结果 |
|---|---|
| B10 参数 | 未传 `--timeout-seconds`，使用 240 秒默认值 |
| 正式任务预算 | 215 秒 |
| 绑定运行 | `20261014T215836501478Z-3e0584b7` |
| trials 文件 | SHA-256 `bcc7969d6bf71d90fb3f6ff9b8d7c3f0d039d0225ed82650df3178a491fbc379`，106 行 |
| 最近停止点 | movement tick 3208 |
| 已完成 R4 | 24 个；最后一项是 tick 3171 的 `r4_validation_1-1-3` |
| 当前外层失败 | `formal Runtime must be ready` |
| 被覆盖的主失败 | `deadline_exceeded` |
| ordered source | 显式注销失败；最终由 Runtime close 清除 |
| trace 关闭 | 满队列、阻塞 sink 或漏关时可能无界 |

上表只冻结修复前事实。它不能证明 R4 运动逻辑失败。

## 2. D1：当前 HEAD 600 秒诊断

### 2.1 来源和命令

- 基线提交：`680d7a7654f3824e29662dcdba900b9e24313bce`；运行前工作树干净；
- 正式运行：`artifacts/fabric-deployment/20261015T012119841247Z-adc8b98e`；
- 精简证据：`evidence/motion_navigation/d109-b10-lifecycle-v1/`；
- 正式命令：

```text
.venv\python.exe scripts\probe_fabric_deployment_observation.py --seed 21001 --server-port 25597 --ipc-port 8140 --b10-gap-solver-probe --time-diagnostics --physics-tick-diagnostics --ftick-diagnostics --timeout-seconds 600
```

第一次启动时，当前 shell 没有设置 `JAVA_HOME`。探针在构建 preflight 报出 `OpenJDK 21 was not found through JAVA_HOME or PATH`，没有启动游戏，也没有产生试次。该环境失败单列保存。临时设置 `JAVA_HOME=.venv/Library/lib/jvm` 后，只原样重跑一次。

### 2.2 功能、安全和进度

| 集合 | 结果 |
|---|---:|
| preflight／turn preflight | 1／1 |
| validation | 40／40 |
| turn validation | 40／40 |
| 默认协调链 | 10／10 |
| R4 0—1 格／秒 | 20／20 |
| R4 1—2 格／秒 | 20／20 |
| R4 2—3 格／秒 | 20／20 |
| typed negative | 24 个，完整运行 |

最后试次是 `r4_validation_2-3-4`，anchor movement tick 为 5621；sidecar 最后 movement tick 为 5640。任务预算为 575 秒。按 trace 单调时钟估算，关闭前已使用约 338.3 秒，剩余约 236.7 秒。生命值最小值和结束值均为 20，damage event 为 0。没有身体责任未解决记录。11 个 ordered source 全部显式注销；客户端和服务器清理通过，端口释放。

F-TICK 保存 22555 条记录，丢弃 0；physics tick 保存 5640 条记录。receiver 记录 11 次交付，其中 10 次在下一控制决定消费，1 次隔 2 tick；该次使 `published_but_next_decision_invisible_count=1`。worker 完成时刻仍不在现有协议中，保持 `unavailable`。

### 2.3 外层失败、inner failure 和 cleanup

外层 summary 原样保留：进程退出码为 1，primary failure 为 `DeploymentEvidenceFailure`。失败检查只有：

- `b10b_solver_is_bounded`；
- `complete_time_event_attribution`。

142 个求解样本全部完成功能场景。P95／P99／最大值为 24.5898／26.9761／46.6656 ms；最大值来自 `r4_validation_2-3-0`，超过既有 30 ms 门槛。time attribution 保存 5637 次观察和 5636 个区间，错误为 `deployment timing samples do not cover one exact Runtime episode`。

独立旁路逐条读取完整 Runtime trace，并检查 trial、exception 和 deadline 记录。没有发现非空 failure、exception 或 error，也没有 `deadline_exceeded`、`motion_solver_retry_exhausted` 或身体责任未解决。因此 inner Runtime failure 为 **不存在**；这不是从外层 reason 反推的结论。

cleanup 另行报告 `asynchronous trace worker failed`。它没有覆盖外层 primary failure，但证明 D108-C-03 仍会在正常功能矩阵结束后发生。137 MB trace 没有复制进 Git；精简证据保存其字节数、SHA-256、record type 计数和独立摘要。

### 2.4 判定

D108-C-01 已关闭为旧正式命令遗漏 `--timeout-seconds 600`，导致任务只有 215 秒预算。600 秒命令完成了整个 B10 功能和安全矩阵，没有证据支持修改 R4 或运动控制器。

D109 仍未通过。求解最大值离群、time attribution 覆盖和异步 trace cleanup 必须分开处理。D3—D8 的生命周期整改继续，连续两跳、零等待交接和新动作仍未授权。

## 3. D2：主失败与来源释放

D2 已完成，未修改 Runtime 状态机、任务期限或运动逻辑。

### 3.1 实现结果

B10 探针现在把三类事实分开保存：

- `primary_failure` 保留最先发生的执行异常。注销或关闭失败不能替换它；
- `cleanup_failures` 分别记录 ordered source 注销、motion worker、planner worker、证据 flush、Runtime close 后核对和生命周期证据写入失败；
- `b10_lifecycle.ordered_source_release.status` 区分 `explicitly_released`、`cleared_on_runtime_close` 和 `not_released`。

Runtime 仍为 READY 时，探针继续显式注销 B10 source。Runtime 已 FAILED／ENDED 时，探针不会调用只允许 READY 的接口。正式 owner `_close_client()` 在持有 Runtime 引用时完成关闭，并立即保存最终 `closed` 状态；`PlayerRuntimeV1.close()` 只有在 `arbiter.clear()` 已执行后才进入该状态，因此探针用这份只读关闭事实确认 source 已清除。它不会在已关闭 Runtime 上重新调用业务查询，也不会把兜底清除冒充成显式注销。

`d105_ftick` 优先读取新 summary 的原生生命周期字段。D1 及更早、没有该字段的历史记录继续从 trace 判断，不改写旧 summary。

### 3.2 失败测试与检查

实现前新增检查得到预期 RED：三个 lifecycle 测试因接口尚不存在而报错，一个新分类测试报错，一个旧分类断言失败。实现后：

- D2-R 直接检查、D105 F-TICK 和 Fabric 探针检查：75／75；
- Runtime、正式 client owner、B10 全链、D108 receiver 和 D107 trace audit 相邻检查：183／183；
- 合计 258 项通过；`git diff --check` 通过。

检查覆盖：Runtime deadline／driver 异常后注销、注销自身失败、无主失败但 cleanup 失败、多项 cleanup 失败、Runtime close 兜底清除、显式注销，以及外层 summary 的主次异常优先级。D2-R 又加入两条完整 owner 控制流：任务成功但显式注销注入失败，以及任务失败后 Runtime close；两者都在 Runtime 引用清空前得到 `cleared_on_runtime_close`，且不调用已关闭 Runtime 的 source 查询。

F-TICK 现在只把 `explicitly_released` 和 `cleared_on_close` 视为合格。零注册、两次注册、`not_released` 和未知状态都会失败；过渡期旧值 `released` 兼容映射为 `explicitly_released`。把验收恢复成只拒绝 `not_released`，新增分类检查会立即失败。

### 3.3 边界

本批只验证失败保真和来源释放记账。D1 前的历史 `summary.json` 保持原字节。新的 Fabric 结果留到 D8 使用同一条 600 秒命令复跑；D2 不单独宣称实机产品结果。

## 4. D3：Linux zombie

D3 已完成实现，并保持 Windows 正式路径不变：

- 同一 PID 和创建时间对应的进程如果处于 `STATUS_ZOMBIE`，清理结果视为已经停止；
- PID 已复用时仍不取得清理权限；
- 无法读取身份或状态时按存活／失败处理，不把权限不足冒充成已经清理；
- `_refresh_cleanup_liveness` 每次根据最终 surviving 和 errors 重算状态。最终两者都为空时可以从早期 `failed` 恢复为 `passed`；任一非空时仍为 `failed`。

实现前，新增测试分别因缺少 zombie 判定和状态只会单向变为 `failed` 而报错。实现后，Windows `test_d107_shard_runner` 为 24／24。它覆盖真实 3 秒模块期限与 30 秒持管道孙进程，整个调用须在 11 秒内返回；同时覆盖 Job Object、真实 survivor、cleanup error、零测试、PID 复用和权限拒绝。另用不依赖 POSIX 调度的检查连续 30 次复核最终状态重算，30／30 通过。

三方的 Linux 对照字节曾在同一对抗场景连续 30 次通过。当前项目最终字节尚未在 Linux 动态运行，因此 D3 的平台结论是：实现和跨平台门禁完成，Windows 不退步；Linux 动态证据等待下一轮三方复核。D5 的双 fork／subreaper 仍是独立问题，不能由本结果关闭。

## 5. D4：异步 trace 有界关闭

### 5.1 正常规则和故障注入

`BoundedAsyncTraceWriter` 现在用独立停止事件结束 worker。`close(timeout_seconds=5.0)` 不再向有界队列提交停止对象，也不再无期限等待。sink 从 writer 构造后由 worker 独占；owner 只等待，不会和仍存活的 worker 并发调用 sink。worker 是 daemon，因此漏调 close 不会阻止解释器退出；这项兜底不会把未封存证据冒充成成功。

容量为 1 的阻塞场景结果如下：

| 时点 | status | sealed | worker_alive | accepted | written | dropped | indeterminate_or_pending | cleanup_failure |
|---|---|---:|---:|---:|---:|---:|---:|---|
| 第一次 50 ms close | `INCOMPLETE` | false | true | 2 | 0 | 1 | 2 | `worker_join／TimeoutError` |
| worker 仍阻塞时重复 close | `INCOMPLETE` | false | true | 2 | 0 | 1 | 2 | `worker_join／TimeoutError` |
| 解除阻塞后 close | `SEALED` | true | false | 2 | 2 | 1 | 0 | null |
| 再次 close | 与上一行逐字段相同 | true | false | 2 | 2 | 1 | 0 | null |

上述 `dropped=1` 是未成功入队的第三条记录；已接纳记录始终满足 `accepted = written + accepted_dropped + indeterminate_or_pending`。只有确定不会写入的记录才计入 `dropped`。如果 sink 先产生写入副作用再抛错，当前记录进入 `accepted_indeterminate`，同时计入 `indeterminate_or_pending`，不会冒充确定丢弃；后续尚未尝试而被失败路径排空的队列记录才进入 `accepted_dropped`。该故障注入得到 `accepted=2`、`written=0`、`accepted_indeterminate=1`、`accepted_dropped=1`、`dropped=1`、`indeterminate_or_pending=1`。

独立子进程创建永久阻塞 sink 后故意遗漏 close，在 3 秒检查期限内以 0 退出，证明 daemon 兜底成立。正常 owner 仍必须显式 close 并核对 `sealed`。

### 5.2 Runtime 所有权

`PlayerRuntimeV1` 从构造开始持有 trace。backend schema 在构造前段拒绝，以及观察适配器在构造中途抛错，两种故障注入都会关闭已持有的异步 writer。若回滚本身返回 `INCOMPLETE`，原 `ContractViolation` 或构造异常保持 primary，回滚问题只进入 exception note。正常 Runtime close 收到未封存结果时，会把它登记为 cleanup failure，并继续把 Runtime 置为 `CLOSED`。

正式 B10 和 F1 的组合 trace owner 也会检查内部异步 writer 的 `sealed`。它们保留已有“先保存主失败，再报告 cleanup failure”的边界。F1 在 diagnostics 构造失败时会回滚已经创建的 formal trace；回滚返回 `INCOMPLETE` 或抛错都只作为 exception note，不能替换 diagnostics 的构造异常。

### 5.3 检查和边界

- writer 与 Runtime 直接检查：57／57；
- Fabric 探针、D108 receiver、F1 跟随和 host 相邻检查：71／71；
- Windows 合计：128／128；
- `git diff --check`：通过。

D4 没有修改运动、输入期限或动作逻辑，也没有运行 Fabric。D1 的旧异步 trace failure 保持原记录；修复后的正式 B10 留到 D8，不能用单元检查回写旧实机结果。

D8 首次最终 B10 的功能矩阵完整运行，ordered source 也显式注销，但 trace cleanup 报出 `dict` 没有 `observation` 属性。原因是异步 writer 已经完成投影，close diagnostics sink 却再按 typed payload 读取。该次原始失败保留，不回写。D4 第二轮限定修正增加了真实组合 RED，改为单次投影契约；2／2 组合检查和 134／134 直接、相邻检查通过，零跳过。修正后只允许再跑一次相同 B10。

## 6. D5：Linux subreaper

Linux 路径现在由父 runner 在任何模块启动前建立 subreaper。`PR_SET_CHILD_SUBREAPER` 后必须通过 `PR_GET_CHILD_SUBREAPER` 复核；启用前后已有后代、同时打开第二个模块 scope 或无法读取精确身份都会 fail closed。模块 root 的 PID 和创建时间在 start gate 放开前登记，关闭了快速退出竞态。

本轮只授权父 runner 在串行模块窗口中清理两类精确身份：从 root 扫到的后代，以及 reparent 后经过两次 PPID 核对的直接子进程。每个身份同时绑定 PID、创建时间和 root 时间窗。child report 只提供诊断提示，不授予清理权限。PID 复用、伪造 PID、启动前已有进程和 `AccessDenied` 均不能取得权限。清理期出现的新 fork 会在有界稳定扫描中继续登记；停止后的收养进程只按精确 PID 使用 `waitpid(WNOHANG)` 回收。

Windows 直接和相邻检查共 63／63。Windows Job Object 快速 root 退出检查保持通过，正式路径没有进入 Linux subreaper 分支。Linux 对抗矩阵已经进入项目测试，覆盖快速 `os._exit()`、`setsid()`、双 fork、中间进程立即退出、忽略 SIGTERM、持有输出、清理期间再次 fork 和 zombie 回收。由于正式环境是 Windows，本轮没有运行这些 Linux 系统调用；最终字节的 Linux 动态结论等待三方复核，不能用 mock 结果代替。

## 7. D6：快照测试分层

跨平台门禁已从 3 个样本的绝对毫秒断言改为确定性工作量检查。`WorldKnowledge.snapshot_materialization_diagnostics` 只读报告最近一次提交的 requested、dirty-before、selected、materialized、committed 和 dirty-after section 数量。诊断只增加固定数量的整数赋值，不扫描额外 section，也不保存无界历史。

4、16、32 个无关脏 section 的场景都只请求 1 个 section。三组结果逐项相同：`requested=1`、`selected=1`、`materialized=1`、`committed=1`；dirty-after 分别为 3、15、31。当前借用得到新事实，所有旧借用继续保留旧事实。缓存 section、dirty section 和 dirty position 继续受现有世界容量约束。故意把 `_commit_snapshot_sections()` 的输入扩大到全部 dirty section 时，`selected_sections=1` 断言与原 builder 调用断言都会失败。

相对耗时使用 20 个样本，只补充报告，不决定跨平台测试成败。正式 Windows benchmark 仍由 `_passes_deadline()` 强制 P95≤8 ms、P99≤15 ms、最大值<30 ms；边界值检查证明 8.001、15.001 和 30.0 ms 都会失败，没有删除或放宽门槛。

干净提交 `9e7228d4d0042612fc6715d888f8db02bfce4099` 上的 Windows CLI 20 样本正式 benchmark 通过。命令、UTC 时间、HEAD、每项原始样本和哈希保存在 `evidence/motion_navigation/d109-b10-lifecycle-v1/d6-snapshot-materialization/`；`result.json` 为 5,446 字节，SHA-256 为 `2eb374f56701339bde7f480ab27f7ff3bdef6052264713564349f702be0a0bcb`。测试会从这些原始数组使用冻结口径重新计算 P50／P95／P99／最大值，并要求 `passed=true`。正式分布摘要如下：

| 场景 | P50 ms | P95 ms | P99 ms | 最大值 ms |
|---|---:|---:|---:|---:|
| 4 个无关脏 section | 3.4777 | 3.5413 | 3.5627 | 3.5627 |
| 16 个无关脏 section | 3.4585 | 3.5107 | 3.5333 | 3.5333 |
| 32 个无关脏 section | 3.4727 | 3.4939 | 3.5702 | 3.5702 |
| B10 范围 | 1.6242 | 1.6893 | 1.6937 | 1.6937 |

这些毫秒数只代表本次 Windows 运行。跨平台结论来自工作量和语义检查，不来自这张表。

快照物化直接检查 12／12，世界快照、Runtime 激活和动作交接相邻检查 105／105，合计 117／117。D6-R 最终字节再次运行直接检查 12／12；独立证据检查重算 8 行、160 个原始样本，并核对结果文件和总清单哈希。`py_compile` 和 `git diff --check` 通过。D6 只增加固定大小的只读诊断，没有改变借用结果或控制行为。

## 8. D7：POSIX feeder 引用环

旧回调闭包直接捕获 worker，形成 `worker → Queue → callback → worker`。构造失败后，Queue 也会保留这个回调。RED 分别捕获了这两条路径。

修复后，Queue 的回调只捕获 worker 的弱引用和原 handler。owner 存活时沿用原错误分类；owner 已释放时调用原 handler。`close()` 只在 feeder 不存在或确认退出、且当前 handler 仍归本 worker时恢复原 handler。close 超时且 feeder 仍存活时不会提前恢复；feeder 晚退出后第二次 close 可以恢复。Windows overlapped 取消和关闭顺序没有修改。

Windows 结果：

- 直接生命周期模块 40／40；
- worker、receiver、prepared proof 和 Windows matrix 相邻检查 84／84；
- 合计 124／124；
- idle、killed、队列满、8 MiB 消息和 worker crash 共重复创建关闭 50 个真实 worker；关闭自动 GC 后，worker 弱引用立即清空，QueueFeederThread 和子进程均回到基线；
- close 超时、feeder 晚退出和构造失败的 handler 所有权检查通过。

Windows handle 数只保存为诊断。它还包含多进程库拥有的 process／semaphore handle，不能用来替代 POSIX 文件描述符门禁，也不能据此扩张 D7 范围。当前环境不能运行 Linux；关闭自动 GC 后的 POSIX fd 零增长矩阵留给三方在最终字节上复核。因此 D109-N4 的代码问题在 Windows 已关闭，跨平台验收尚未签署。

## 9. Windows 正式验收

最终 Windows 回归绑定干净提交 `cb06b44884e8a7641b797887f0c12183a6b1d451`。所有长矩阵串行运行，没有并行正序和逆序。

| 集合 | 模块 | 检查 | 结果 |
|---|---:|---:|---|
| D2—D7 直接与相邻 | 20 个显式模块 | 425 | 全部通过 |
| 源树正序 | 186 | 2331 | 全部通过 |
| 源树逆序 | 186 | 2331 | 全部通过 |
| 源树 Java 门禁 | — | 3 | 全部通过 |
| 独立公开快照正序 | 185 | 2307 | 全部通过 |
| 独立公开快照逆序 | 185 | 2307 | 全部通过 |
| 快照 Java 门禁 | — | 3 | 全部通过 |
| 快照 `verify` | — | 2299 个文件 | 导出后和全量后均通过 |

上述结果的失败、错误、超时和跳过均为 0。每个模块保持 300 秒上限。大体积分片报告保留在本地 `.tmp/d109-d8/`；公开证据只保存命令、计数、边界和原始报告 SHA-256，见 `evidence/motion_navigation/d109-b10-lifecycle-v1/d8-windows-regression/`。

D4 第二轮修正和最终 B10 证据提交后，又在干净提交 `53b6ffa5486ac23c5de6c4728955ef0c81d04821` 上按相同顺序完成一次最终字节复跑：直接与相邻 427／427；源树正逆各 186 个模块、2331 项；独立公开快照正逆各 185 个模块、2307 项；源树和快照 Java 门禁各 3／3；快照导出后和测试后两次 `verify` 均为 2305 个文件。失败、错误、超时和跳过仍为 0。大报告的字节数和 SHA-256 见 `d8-post-b10-regression/`，本次没有再次运行 Fabric。

本轮按 fail-fast 保留了三个预检和中止记录：测试夹具漏同步 `tracked_entity` 的世界 tick；Java 门禁首次启动缺少 `JAVA_HOME`；历史 D105 证据测试把源仓库 `.venv` 误当成公开快照内容。三者都在后续高成本矩阵前或对应模块处停止，限定修正分别见 `15b142f6`、`016a9685` 和 `cb06b448`。旧失败报告不改写。

## 10. Linux 补充验收

待填写 POSIX 生命周期矩阵和完整检查结果。Linux 不是正式产品性能门槛平台。

## 11. 最终 B10 与结论

修复后的唯一一次 600 秒 B10 绑定提交 `d9c564d400bc3fe809b1f7293033922bd6832b1b`，运行目录为 `artifacts/fabric-deployment/20261015T055515931896Z-82d614a6`。它与 D1 和 D4 第二轮修正前的失败分别保存，没有回写旧证据。精简证据见 `evidence/motion_navigation/d109-b10-lifecycle-v1/d8-final-b10-post-repair/`。

功能与安全矩阵全部完成：validation 40／40、turn validation 40／40、默认协调链 10／10、R4 60／60，24 个 typed negative 全部运行。生命值最小值和结束值均为 20，damage event 为 0。ordered source 为 `explicitly_released`。异步 trace 接纳 22409 条并写入 22409 条，确定丢弃、不确定记录均为 0，`worker_failed=false`；外层和 B10 lifecycle 的 `cleanup_failures` 都为空。因此 D2 的来源释放和 D4 的异步 trace 生命周期已由正式 Fabric 链关闭，D108-C-03 也随之关闭。

批次仍以失败结束，失败检查只有两项：

- `b10b_solver_is_bounded`：142 个样本的 P95／P99／最大值为 24.5388／26.4234／41.0339 ms，最大值超过 30 ms；
- `complete_time_event_attribution`：保存 12026 个事件和 5552 次观察，回归计数为 0，但报告以 `'NoneType' object is not subscriptable` 结束。

这两项与生命周期修复分开记录。按本阶段最多两轮限定修正的规则，不再开始第三轮修正，也不再重跑 B10。D109 最终状态为 **未通过并停止**；连续两跳、零等待交接和新动作仍未授权。

## 2026-10-15 后续 Linux 复核

上文 D3／D5／D7 和 POSIX 待办保留为 D109 签署时的证据边界。三方随后对 D110 最终公开字节完成 Linux 动态复核：185 个模块 cleanup 全过，D5 矩阵 20／20，fork 链 10／10 且返回后零存活，D7 无自动 GC 的 fd 门禁 10／10。因此 D109-N1／N2／N4 与 D110-LINUX-01 已关闭；单 CPU 的 PID 文件及 receiver 周期两项测试竞态由 D111 采纳修正，原生产断言保留。复核来源和范围见[缺陷台账](defect-ledger.md)。这些结果不改变本页的历史 B10 失败，也不替代 Windows 性能验收。
