# D123-3 正式试验的启动结构审查（7f1a95a2）

日期：2026-10-22。审查对象：`origin/main` 7f1a95a2（“Publish D123 transport closure”），对照上一轮 fdf22ff9。

本轮范围：D123-2 传输生命周期、D125／D126（已知地图）、D128（仓库 JDK 入口）、D129（无效运行分类）和 D123-3 的最终停止。用户的问题是：下一步是否应该改造正式试验的启动结构。

平台说明：

- 本轮只在 Linux 上运行项目函数、项目夹具、项目测试和项目 Java 类，项目树本身不改。
- E136 只把项目的 `ClientTimingSchedule.java` 和一个探针一起编译到临时目录。
- 没有运行 Minecraft 或 Fabric。Linux 上的耗时只用于说明量级和相对关系，Windows 数字引用项目自己的记录。
- 脚本和输出在 [`2026-10-22-d123-launch-review/`](2026-10-22-d123-launch-review/)。

## 结论

1. **应该改启动结构。** 但要改的是两件事：精确窗口登记在哪一拍，以及离线门禁是否和 Fabric 走同一条路径。继续在现有夹具上调整调用顺序或补字段，不会有用。
   - **现结构给父动作的余量是 0 拍。** 先取 live anchor，之后才做全部证明工作，父动作窗口却固定在 anchor+1。
     - 项目在 Windows 上实测：重放约 50 ms，加后继绑定约 17 ms，已经超过一拍（50 ms）。
     - run-11 从 anchor 到提交用了 380 ms，请求在 anchor 之后第 7 拍（tick 27）才到客户端；run-10 用了 1.39 s，第 27 拍（tick 34）才到。
     - Linux 上同一组装路径用了 3.05–3.25 s（E137）。
   - **离线门禁看不到这类失败。**
     - 组件后端的 movement tick 只随 step 加一，不随墙钟前进。E137 里组装期间按 20 Hz 实时计算已过去 61–65 拍，组件测试照样通过。
     - 离线 3／3 和 36／36 跑的是 `d121_handoff_execution_probe.ExecutionCase`：proof 冻结在 tick 0，没有 live anchor。
     - Fabric 跑的是 `_FabricExecutionCase` 加 `run_fabric_runtime`，是另一条路径。
   - **12 次正式运行中，8 次失败在只有 Fabric 才执行的代码或时序上**（§2.3）：run-4–run-9 是组装和 payload 代码，run-10–run-11 是时序。
2. **推荐“预约起跳”，不推荐先做 proof retime。** 预约起跳指：从静止 anchor 出发，把父动作窗口登记在 anchor 之后第 K+1 拍，中间 K 拍由已证明的 neutral 前奏保持静止。
   - **E138 的结果：**
     - 前奏 K 拍后，在 K+1 拍起跳。主轨迹、输入、放手安全点、依赖、后继输入和后继终态，忽略 tick 编号后都与 K=0 完全相同。
     - 唯一不同的是 fallback 尾段里的 `main_suffix_digest`，因为摘要包含绝对 tick。
     - 成本不随 K 增长：K=0 到 40，重放都是 105–112 ms，后继绑定都是 38–39 ms。
   - **所以按预约的那一拍直接计算就行，成本与 anchor+1 相同。** 反过来，若把现有 proof 平移到新的 tick，就必须改证明身份（摘要）。
   - **生产代码已有的部件：** `_prove_delayed_starts` 的 neutral 前奏规则，admission 里的 `_proved_prelude_applied` 与 `verified_start_state_matches`，以及客户端的 pending 窗口。
   - **缺的只有一条：** 固定后继的父动作要能使用落在 anchor+1 之后的单拍窗口。现在 `_prove_delayed_starts` 要求窗口从 anchor+1 开始，`bind_fixed_successor` 又拒绝 delayed variants。
3. **后继这一侧时间够用（E139）。** 后继 `PreparedMotionJob` 在 Linux 单核上要 368–444 ms，父动作执行期 26 拍约 1.3 s。
4. **D123-2 修好了上一轮 E130 指出的问题，但计算器和 Java 的事件顺序不一致。**
   - **已修好（E135）：** `local_not_dispatched` 之后的观察交换改成带窗口的 neutral，生效拍设在默认结束后的第一拍（tick 35）。它在默认执行期内任何一拍被接纳，972 个入口都停在中间平台。
   - **事件不一致（E136）：**
     - Java 在 tick 35 先发 `default_exhausted`，再发 `window_applied`；
     - 计算器在同一场景下完全不发 `default_exhausted`；
     - 所以“新观察必须晚于 `default_exhausted`”这条验收在组件测试里无法检查。
5. **公开仓库无法复核 D129 的分类（E140）。**
   - run-8 到 run-11 的 manifest 共绑定 24 个 `.log` 文件。`.gitignore` 里有 `*.log`，所以它们没有进入公开仓库。
   - `test_d129_invalid_harness` 因此在 setUpClass 就失败（run-10 manifest 的哈希不符）。`offline_preflight` 调用的是同一个 validator。
6. **其他。**
   - D126 改用测试已知地图，与上一轮建议的方向一致，也符合决定 0009。
   - 导出元数据恢复为 `source_worktree_dirty: false`。
   - 上一轮的三项仍未改：z=0 的评估规则、D124 后台求解变慢（1 s 工作窗口）、1577 个纯行尾改写。用户已说明尚未处理。
7. **全量分片：** 214 个模块中 208 个通过。6 个报错：5 个依赖 Windows 环境，1 个是上面第 5 条的公开仓库问题（§8）。

## 1. 建议的下一步：一份新决定（暂称 D130）

本决定只覆盖“预约起跳”和“统一试验入口”。暂停新的 Fabric 运行，直到以下第 1–3 项在离线通过。

1. **正式 API：允许固定后继的父动作使用预约的单拍窗口 `[S, S]`，其中 `S = anchor + 1 + K`。**
   - **K：** 冻结为常量，按 Windows 上实测“anchor 到可派发”的 P99 加传输时间，再留至少 2 倍余量。以 run-11 的 380 ms 为参照，K=20（1 s）约有 2.5 倍余量。
   - **前奏：** 沿用 `_prove_delayed_starts` 现有的 neutral 前奏检查（着地、无水平碰撞、y 不变），依赖并入父证明。
   - **派发：** 在 S−1 的观察上走现有 admission，即 `intended_start_tick == anchor + 1`、`_proved_prelude_applied` 和 `verified_start_state_matches`，然后发出第一条命令。这是普通控制帧，只做廉价检查。
   - **不变的部分：** `bind_fixed_successor`、D122 的 wire 和 Java 都不改。
2. **启动结构：与 anchor 无关的工作全部移到 anchor 之前。**
   - **提前做：** 静态已知地图的快照封存，Session、driver 和 route 的构造，worker 预热，冻结用例加载。E137 中仅快照封存一项在 Linux 上就要 2.65–2.82 s。
   - **anchor 之后只做：** 预约那一拍的重放、后继绑定和 admission。
   - **超时的含义改变：** 超出 K 是有类型的产品时序失败，不再属于 `INVALID_HARNESS_RUN`。D129 的 fixture-scheduling 子类可以就此关闭。
3. **离线门禁和 Fabric 共用一个 trial runner，只替换 backend、建图和传送。**
   - **计算器要有实时节拍模式：** movement tick 按墙钟以 20 Hz 前进，不依赖 host 请求。这样 run-10 和 run-11 在离线就会得到同样的 `window_missed`。
   - **验收：** 注入 380 ms 和 1.4 s 的 host 延迟，旧结构必须报出 `window_missed`，预约结构必须通过。
4. **发布：** manifest 绑定的文件要么公开，要么在 manifest 里明确标为“仅主项目保管”，并让 validator 区分这两类。否则公开仓库里的 D129 validator 和 `offline_preflight` 都无法运行。
5. **计算器时序表与 Java 对齐：** 先检查默认是否耗尽，再处理 pending，与 Java `prepare()` 的顺序一致。补一条测试：“默认最后一拍之后，同一拍的 pending 生效前必须先出现 `default_exhausted`”。

**不建议做的事：**

- **proof retime 或相对时间证书。** E138 表明它节省不了计算，反而要改摘要身份。
- **扩大父动作的起跳窗口，让它覆盖多拍（delayed variants）。** 每多一拍就要多一次完整重放（Windows 约 50 ms，与一拍相当），窗口永远追不上自身的计算时间，而且固定后继本来就不接受这种窗口。

## 2. 现结构为什么命中不了 anchor+1

### 2.1 时间账

| 来源 | anchor 之后的宿主工作 | 父窗口 | 结果 |
|---|---|---|---|
| 项目 Windows 设计复核 | 重放 49.8–52.2 ms，后继绑定 16.4–17.0 ms | anchor+1 | 下限已超过一拍 |
| run-11（Windows Fabric） | anchor 到提交 intent 380 ms | `[21,21]` | 第 27 拍收到，第 28 拍 `window_missed` |
| run-10（Windows Fabric） | anchor 到派发 1,389 ms | `[8,8]` | 第 34 拍收到，第 35 拍 `window_missed` |
| E137（Linux 组件，四方向） | 共 3.05–3.25 s；其中快照封存 2.65–2.82 s，组装重放 278–315 ms，后继绑定 40–52 ms，admission 14–18 ms | `[7,7]` | 组件测试通过，但按 20 Hz 已过去 61–65 拍 |

作为 anchor 的那次观察到达之后，`_FabricExecutionCase.__init__` 依次执行以下步骤，然后 `bind_and_start` 才做第一个 `driver.tick()`：

- 建 Session 和 driver，调用 `driver.start`，再从 Session 读出 anchor；
- 组装冻结 proof（完整重放）；
- 封存快照，绑定后继；
- 建 route，执行 admission，安装 verified motion。

这些步骤里依赖 anchor 的只有组装重放、后继绑定和 admission；其中 admission 很便宜。E137 的计时从 Session 读出 anchor 开始，没有包含前面建 Session 和 driver 的时间，所以略偏小。

### 2.2 组件门禁为什么看不到

- **锁步后端：** `tests/sim/backend.py` 中 `CalculatorBackend.step` 每次只把 `movement_tick` 加一，不读墙钟。所以无论 host 花多久，请求总在下一拍“及时”到达。
- **路径不同：** `offline_preflight` 的组件 smoke 和 matrix 调用 `component_trial → d122_formal_timing_probe.run_case → d121_handoff_execution_probe.ExecutionCase`。proof 冻结在 tick 0，backend 也从 tick 0 开始，不经过 live anchor 组装。
- **Fabric 专用代码：** Fabric 正式运行走 `run_fabric_runtime` 和 `_FabricExecutionCase`。
  - `run_fabric_runtime` 在离线测试里从未被执行，只有 `inspect.getsource` 的结构断言；run-9 之后才单独补测了其中的 terminal payload helper。
  - `_FabricExecutionCase` 只有一条组件测试会执行（`test_component_runs_the_exact_fabric_pretrial_assembly_after_fixture_steps`）。它只跑到 `prepare_fixed_successor`，后端仍是锁步的。

### 2.3 12 次正式运行的去向

| 运行 | 分类 | 失败点 | 共用 runner + 实时计算器能否在离线发现 |
|---|---|---|---|
| D123-3 smoke-run-1 | environment／jdk_discovery | 调用终端无 JDK | 否；D128 的入口检查已覆盖 |
| D128 run-1 | environment／build_cache_native | `:remapJar` 缺 24 个 LWJGL native | 否；需真实离线构建，已加入 |
| run-2 | tool／standalone_export_manifest | required 清单与 `.log` 排除冲突 | 否；需真实导出步骤 |
| run-3 | environment／runtime_classpath_jline | 离线 client classpath 缺 JLine | 否；需真实启动链 |
| run-4–run-7 | fixture／assembly | 冻结 proof 的坐标、窗口、anchor tick、candidate revision 对不上 | 是：Fabric 专用组装代码 |
| run-8 | fixture-adapter | 用错 velocity 属性名 | 是：Fabric 专用 payload 代码 |
| run-9 | fixture-adapter | 引用未定义的 `current` | 是：同上 |
| run-10、run-11 | INVALID_HARNESS／fixture-scheduling | anchor 之后工作过长，窗口错过 | 是：需要实时节拍 |

12 次正式运行没有产生一次有效 trial。

- **前 4 次：** 环境和工具问题，D128／D129 已逐项修好。这部分做得对。
- **后 8 次：** 都是离线门禁本来能拦下的问题，却要等到真实 Fabric 运行才暴露。

D129 的七项合取条件本身严格且有原始证据支撑。但当一条规则需要反复用来给失败运行“去资格”时，问题出在门禁不具代表性，而不在规则本身。

## 3. 预约起跳（E138）

E138 只调用项目函数：

- `fixed_case(0)`：D122 冻结的父 proof，静止 anchor 在 tick 0；
- `project_movement_command`、`physics_1_21.step`；
- `_replay_verified_commands`；
- `bind_fixed_successor`。

| K | 前奏是不动点 | 起跳拍 | 出口拍 | 忽略 tick 后与 K=0 相同 | 后继绑定 | 后继窗口 | 前奏+重放 | 绑定 |
|---:|---|---:|---:|---|---|---|---:|---:|
| 0 | 是 | 1 | 26 | — | solved | `[27,27]` | 107.8 ms | 38.3 ms |
| 1 | 是 | 2 | 27 | 除 fallback 尾段外全部相同 | solved | `[28,28]` | 106.0 ms | 38.9 ms |
| 5 | 是 | 6 | 31 | 同上 | solved | `[32,32]` | 105.3 ms | 37.9 ms |
| 10 | 是 | 11 | 36 | 同上 | solved | `[37,37]` | 109.5 ms | 37.9 ms |
| 20 | 是 | 21 | 46 | 同上 | solved | `[47,47]` | 109.5 ms | 39.1 ms |
| 40 | 是 | 41 | 66 | 同上 | solved | `[67,67]` | 112.3 ms | 38.9 ms |

- **“全部相同”覆盖的项：** 主轨迹、输入、放手安全点、依赖、后继输入和后继终态。
- **fallback 尾段的差异：** 逐字段比对后，唯一不同的是第 15 步尾段的 `main_suffix_digest`。这个摘要覆盖已承诺的主轨迹后缀，而后缀状态带绝对 tick。
- **含义：** 预约起跳不是给旧 proof 重新标时间，而是对预约的那一拍正常计算一次 proof，摘要自然正确。成本与 anchor+1 相同，与 K 无关。
- **E138 的局限：** 实验里用 `anchor_movement_tick_id=K` 表示预约起点，只是为了能调用绑定函数。正式表示方式由 D130 决定：可以是“父证明的 anchor 仍是观察到的 T，另声明 K 拍前奏”，与 `proof.preparation.source_anchor` 的用法类似。
- **为什么不改用多拍窗口：** 生产的 `_prove_delayed_starts` 为窗口里每一拍做一次完整重放。在 Windows 上，每多一拍窗口就多约 50 ms 计算，正好是一拍。从 anchor+1 开始的多拍窗口，算完时已经过去差不多同样多的拍数。所以起跳窗口必须与 anchor 隔开，而不是变宽。

## 4. 后继这一侧（E139）

E139 运行同一条组件测试，收集正式提交的 `PreparedMotionJob`，再在本进程里调用 `motion_worker._execute_job` 计时。

- **四个方向：** 367.5、367.8、408.0、444.1 ms，结果都是 `PreparedWireMessage`。
- **可用时间：** 后继要在父动作的 L 拍之前就绪。父动作有 26 条命令，执行期约 1.3 s。
- **对照 run-11：** run-11 的控制流是“父动作第一拍返回后才提交后继”，这在及时的情况下仍有约 0.8 s 余量。run-11 本身没走到这一步。

所以后继一侧不需要改结构。D130 只需在矩阵前，用 Windows 实测把这个余量确认下来。

## 5. D123-2 的观察交换（E135、E136）

`PlayerRuntimeV1.complete_local_not_dispatched` 发出的请求是 `movement=MovementV1()`，`first_effect_window=[activation_tick + valid_for_ticks]`。也就是说，它在默认结束后的第一拍才生效。

**E135（计算器客户端，D120 的 972 个入口）：**

| 交换被接纳的拍 | 普通 neutral（上一轮 E130） | 带窗口的 neutral（D123-2） |
|---|---|---|
| 27–32 | 972 掉落 | 972 停稳 |
| 33、34 | 648 掉落，324 停稳 | 972 停稳 |
| 35 | 972 停稳 | 972 停稳 |

上一轮指出的风险已经消除：带窗口的请求进入 pending，不清空正在执行的默认。

**E136（项目 Java `ClientTimingSchedule`，同一场景）：** 不论在哪一拍接纳，tick 35 的事件都是 `[default_exhausted@35#25] [window_applied@35#26]`。

**计算器的 E135 事件：** tick 34 的 `default_active` 之后，直接就是 `window_applied@35`，没有 `default_exhausted`。原因在于顺序：

- 计算器 `sample()` 先处理 pending，`_activate` 内的 `clear()` 把默认清掉，随后的耗尽检查就不再触发；
- Java `prepare()` 先检查默认是否耗尽，再处理 pending。

身体结果两边一致，但组件测试里永远看不到这一拍的 `default_exhausted`。D123 验收要求“新观察晚于 `default_exhausted`”，所以这个事件差异需要对齐（§1 第 5 项）。

## 6. 公开仓库与 D129（E140）

- **manifest 绑定的文件：** run-8、9、10、11 的 manifest 各绑定 6 个 `.log`：client console、debug、latest、server latest、server console、worker，共 24 个。
- **公开情况：** 这些文件不在公开仓库，`SHA256SUMS.txt` 也没有列出它们。`.gitignore` 里有 `*.log`，D129 run-2 当时修的也正是 required 清单与 `.log` 排除规则的冲突。
- **影响：**
  - **D129 validator：** 先校验 run-10 的 50 项 manifest，所以在公开仓库里，`test_d129_invalid_harness` 在 setUpClass 就报 `ValueError: D129 run-10 manifest hash differs: source-run/client-0/console.log`；
  - **offline_preflight：** 也调用同一个 validator。
- **和文档的出入：** smoke-run-11 的 README 写的是“逐字节复制 source run 的 44 个原始文件”，公开仓库里少了 6 个。
- **结果：** 公开读者能看到分类结论，却无法用项目自己的 validator 复核。

## 7. 其他

- **D126：** 运动验收改用 `seed_test_oracle_memory(TEST_ORACLE, …)`，把夹具地图装进 Runtime 自己的 `WorldKnowledge`。服务器建图与测试地图来自同一组 rows，四方向 58／58 已知。
  - `seed_test_oracle_memory` 只出现在脚本、`runtime_adapter.py` 的方法定义和测试里，不在正式启动路径上。
  - 这修正了 D122-4 以来“用当前相机逐格确认依赖”的范围偏差，与上一轮建议的方向一致。
- **D128：** 仓库 JDK 绑定的做法是对的：路径、canonical home 和哈希冻结，并传入每个子进程。
- **导出元数据：** `source_worktree_dirty` 恢复为 false。
- **上一轮未改的三项：**
  - z=0 的 `_release_recovery_evidence` 规则；
  - D124 让下降求解从约 0.57 s 增加到约 0.92 s（1 s 窗口）；
  - 相对 3889d10 的 1577 个纯行尾改写（本轮复核数字不变）。

## 8. 全量分片

项目分片器在 Linux 单核 CPU 3 上运行，见 `output/linux-forward-summary.txt`。

- 214 个模块，2793 项；208 个模块通过，0 failure，6 个模块报错，0 超时。
- 清理全部 passed，隔离方式都是 Linux subreaper／POSIX session。

6 个报错模块：

| 模块 | 原因 |
|---|---|
| `test_d101_measurement`（2 error） | 要求项目的 Windows 环境，已知 |
| `test_d123_fabric_transport`（4 error） | 需要仓库 `.venv` 中的 Windows JDK |
| `test_d125_three_fact_observation`（14 error） | 需要 Windows `surface_cache.dll` 和 `WindowsPath` |
| `test_d127_transport_lifecycle`（1 error） | 硬编码 `D:/My_project/mc_ai/.venv/Library/bin/javac.exe` |
| `test_d128_repository_jdk_launch`（7 error） | 需要仓库 `.venv` 中的 Windows JDK |
| `test_d129_invalid_harness`（setUpClass） | 公开仓库缺少 run-10 manifest 绑定的 `.log`（§6） |

前 5 个都依赖 Windows 环境。最后一个在任何平台的公开仓库上都会失败。

上一轮分片中的两条墙钟测试，这次都通过了：`test_continuous_descent` 和 `test_b10_motion_candidate`。这只说明本次机器负载下余量够用，D124 带来的求解变慢仍在（§7）。

## 9. 证据边界

- **平台：** 全部结果来自 Linux，没有运行 Minecraft 或 Fabric。Windows 时间引用项目自己的记录（D123 验收第 20–22 节、`d128/final-stop/`）。
- **E137 的数字：** 来自锁步计算器上的组件路径。快照封存在这里很慢（2.65–2.82 s），是因为测试把整个 29×11×29 场景都写进了已知记忆。Fabric 中的已知格少得多，run-11 全程只有 380 ms。这里只用它说明两点：哪些工作在 anchor 之后，以及组件测试对墙钟不敏感。
- **E138 的范围：** 只覆盖南向冻结用例和静止 anchor。预约起跳的正式表示，以及 S−1 拍在 Windows 上的控制帧耗时，需要 D130 实测。
- **E139 的范围：** 在本进程中运行 job，不含 IPC 和 worker 调度。
- **E135 与 E136：** 前者用项目计算器客户端，后者直接驱动项目的 Java 类，都没有经过真实 Fabric 客户端。
- **D127：** 传输生命周期的其余实现本轮没有逐项审查，只核对了上一轮 E130 的风险点。
- **E140 的一次额外失败：** E140 里 `test_d123_fabric_transport` 除 4 个 Windows 环境 error 外，还多出 1 个 failure。当时同一核上另有一个进程在跑。之后单独重跑 3 次都没有复现，我没有拿到具体测试名，推断是负载下的计时测试。

## 10. 实验索引

| 编号 | 脚本 | 输出 | 内容 |
|---|---|---|---|
| E135 | `e135_windowed_exchange_during_default.py` | `output/e135_windowed_exchange_during_default.json` | 默认执行期内接纳普通或带窗口的 neutral 后，972 个入口的去向和计算器事件 |
| E136 | `e136/TimingOrderProbe.java` | `output/e136_java_timing_order.txt` | 同一场景下项目 Java `ClientTimingSchedule` 的事件顺序 |
| E137 | `e137_anchor_to_dispatch_budget.py` | `output/e137_anchor_to_dispatch_budget.json` | 正式 pre-trial 组装从 anchor 到父请求出站的耗时与阶段 |
| E138 | `e138_scheduled_start.py` | `output/e138_scheduled_start.json` | 预约起跳 K=0…40：等价性、后继绑定和成本 |
| E139 | `e139_successor_job_budget.py` | `output/e139_successor_job_budget.json` | 后继 `PreparedMotionJob` 的耗时 |
| E140 | `run_all.sh` 内联 | `output/e140_new_modules_and_unpublished_logs.txt` | 本轮新增测试模块单独运行，以及 manifest 绑定但未公开的文件 |
| 分片 | 项目 `scripts/run_motion_nav_shards.py` | `output/linux-forward-summary.txt` | 完整 `tests/motion_nav`，Linux 单核 |

复现：`PROJECT=<7f1a95a2 检出> sh run_all.sh`。
