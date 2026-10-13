# D101 连续运动最小试验验收

日期：2026-10-11。状态：预登记；M0—M2 均未运行。

依据：[D101](../decisions/0101-clarify-short-open-loop-timing-and-lifecycle.md)、[D100](../decisions/0100-use-risk-aware-continuous-deviation-recovery.md)和[实施计划](../stages/continuous-motion-minimal-probe-d101-plan.md)。

## 1. 证据边界

本阶段只验证隔离实验语义和最小 Fabric tick 顺序。它不修改 `mc2p` 正式运动链，不接入 Runtime／Session，不实现正式客户端输入队列，也不宣称连续跑酷已经交付。

D101 的正式撤销、替换和输入欠载收尾不在本轮范围。M1 只验证旧 generation 结果不能接纳；不能把这项局部检查写成完整生命周期通过。

M0／M1 使用 Windows、项目 1.21 计算器和现有冻结 `jump_gap_1:A3` 输入。M2 使用独立 Fabric deployment probe，只增加只读事件记录。

三方 E13／E14 只作参考。它们不进入分母、性能门槛或阶段判定。正式性能只采用本项目 Windows 原始样本。

D098 的 P3 失败、60 次安全门槛失败和原始性能结果保持不变。本阶段的任何成功都不能回写 D098。

## 2. 阶段顺序和止损

1. M0 验证单跳输入流起点和关键节点比较。
2. M0 全部门槛通过后，才运行 M1。
3. M1 验证连续两跳、空中提前计算和着地比较。
4. M1 全部门槛通过后，才运行 M2。
5. M2 只回答 Fabric tick 顺序，不实现队列。

任一阶段失败：保留失败证据，停止后续阶段，不改门槛、矩阵、分母或原始记录。

## 3. 共同规则

- 0／1 tick 只属于输入流第一条命令；
- 第一条实际应用回执立即确定唯一时序分支；
- 后续单条晚到、缺失或内容不同属于偏离；
- generation、owner、真实分支、身体范围和依赖一致时，关键节点只比较；
- 证明有效范围与外力归因阈值分开；
- UNKNOWN 不能当作空气或安全落点；
- 所有推进都要有 owner、generation、账本和证据来源；
- 原型 worker 在所有退出路径关闭；
- 行为不读取墙钟；
- 性能统计采用 nearest-rank，完整决定包含账本、比较、许可和结果构造。

## 4. M0 矩阵

正式输入为现有 `jump_gap_1:A3` 冻结输入。每个正例运行 100 次。

| 组 | 第一条实际应用 | 后续时序 | 预期 |
|---|---|---|---|
| M0-A | 请求 tick | 绝对 tick 连续 | 完成并安全落地 |
| M0-B | 请求 tick＋1 | 整体偏移后的绝对 tick 连续 | 完成并安全落地 |
| M0-N1 | 请求 tick＋2 | 不执行 | 拒绝起点时序 |
| M0-N2 | 第一条已确定，后续单条晚 1 tick | 不重新选分支 | 有界偏离，不冒充完成 |
| M0-N3 | 重复或倒序第一回执 | 不执行 | 契约拒绝 |

M0-A／B 是正例成功率分母。N1—N3 单独验收分类，不进入正例成功率。

### M0 通过条件

- M0-A 100／100、M0-B 100／100 完成并安全落地；
- 第一条回执处理后，另一时序分支全部删除；
- N1—N3 全部得到预期拒绝或有界偏离；
- 一致的承诺点和落地点完整证明调用增量为 0；
- 掉落、伤害超额、UNKNOWN 放行、无人负责推进和错误 generation 接纳均为 0；
- 完整决定 P95≤8 ms、P99≤15 ms、最大值<30 ms；
- 逐场输入、状态、关键节点、比较次数、physics step 和耗时可以从原始记录重算。

## 5. M1 矩阵

M1 使用朝南的两次一格跨隙。两道沟之间的平台长度为 2、3、4 格。第一跳落地观察分别注入沿前进方向 −0.05、0、+0.05 格偏差。输入流起点分别按时和整体晚一 tick。每个组合重复 10 次。

总规模：3 个平台长度 × 3 个落地偏差 × 2 个起点分支 × 10 次＝180 次。

偏差只修改第一跳真实落地观察并从该状态继续物理推进，不改写输入回执、世界或伤害结果。±0.05 只验证已声明入口范围，不代表正式 Fabric 噪声分布。

第一跳首次进入空中后，隔离 worker 提交第二跳准备。第一跳 owner 始终保留身体。第二跳结果必须绑定 generation、入口范围和依赖。着地状态仍在范围内时只比较并接纳；超出范围时旧候选失效，并从真实着地状态重新求解。重求解期间只能继续预登记的短地面安全前段，不能停车等待，也不能越出平台。

正式 M1 以每 tick 50 ms 的固定节拍推进隔离客户端。sleep 不计入决定耗时；控制逻辑不读取经过时间选择输入。每个 tick 只轮询一次 worker，着地前未交付就按失败记录，不能暂停客户端等待。

### M1 通过条件

- 180／180 完成两跳；
- 掉落、伤害、安全违规、UNKNOWN 放行和无人负责推进均为 0；
- 180／180 在第一跳空中提交第二跳准备，并在第一跳着地前交付；
- 着地仍在入口范围的运行完整证明调用增量为 0；
- 着地超出入口范围的运行全部拒绝旧候选，从真实状态重求解，并在安全前段耗尽前交付；
- −0.05、0、+0.05 三组掉落均为 0；
- 无偏差组着地到后续有效输入空档≤1 tick；
- 偏差重求解组没有完整停车，并在离开平台前进入第二跳；
- 两跳之间平台段完整停车 tick 为 0；
- 旧 generation 结果接纳数为 0；
- 每场结束后 `d101-precompute` 活子进程为 0；
- 完整决定 P95≤8 ms、P99≤15 ms、最大值<30 ms。

M1 另外报告第一跳着地速度、第二跳起跳速度、两跳总用时、反向输入 tick、空中中性 tick 和 worker 交付提前量。这些指标本轮只报告，不另设隐藏门槛。

## 6. M2 Fabric tick 顺序

M2 只有在 M0／M1 全部通过后运行。四个水平方向各 10 次，共 40 次。

每次在第一跳着地观察送达 Python 后立即产生普通前进命令。探针记录：

1. 着地观察在客户端生成；
2. Python 决定开始和结束；
3. 命令 frame 到达客户端线程；
4. `Input.tick()` 读取输入；
5. `tickMovement` 完成实际应用。

跨进程不直接相减纳秒时钟。事件只通过 episode、request sequence、observation sequence 和 movement tick 关联；纳秒只用于各自进程内部排序。

### M2 分类

| 分类 | 含义 |
|---|---|
| `NEXT_MOVEMENT_TICK` | 着地观察后的命令赶上下一个 movement tick |
| `LATER_MOVEMENT_TICK` | 命令在更晚 movement tick 才被读取或应用 |
| `INCOMPLETE` | 事件缺失、重复、identity 不一致或 tick 回退，不能下结论 |

M2 的通过条件是 40／40 时间线完整、可重算且没有改变输入行为。`NEXT_MOVEMENT_TICK` 的比例不作为本轮通过门槛；它决定后续正式设计是否需要有限预排程。

M2 不新增命令队列，不测试撤销队列，不据此宣称 D101 generation 协议已经实现。

## 7. 性能口径

M0／M1 在正式 Windows 环境测量：

- P95≤8 ms；
- P99≤15 ms；
- 最大值<30 ms。

决定时间包括输入账本处理、时序剪枝、关键节点比较、许可检查、worker 结果轮询和结果构造。不包括 fixture 建造、证据写盘、进程启动和 worker `close()`。

分别报告 compare-only、完整证明、worker 准备和闭环总耗时。平均每 tick 成本只作说明，不替代 P95／P99／最大值。

M2 不使用跨进程墙钟计算控制延迟；只按 movement tick 判断能否赶上下一 tick。

## 8. 证据文件

每个已运行阶段保存：

- `manifest.json`：提交、源码、配置和输入哈希；
- `summary.json`：从原始记录重算的汇总；
- 固定时间戳压缩的逐决定 JSONL；
- 测试命令、退出码和平台信息；
- worker 创建、关闭和残留线程计数；
- 阶段通过或停止原因。

证据根目录为 `evidence/motion_navigation/continuous-motion-d101-v1/`。未获准运行的阶段不创建空证据目录，也不写假通过记录。

## 9. 正式结果写入规则

运行前，本文件第 1—8 节保持不变。实施后只在文件末尾追加“正式结果”小节，不回写矩阵、门槛、分母或证据边界。

阶段最多报告到实际运行的最后一步。M0 或 M1 失败时，必须明确写 M2 未运行，而不是记为跳过通过。

## 10. M0 正式结果：性能门槛未通过

2026-10-11，Task 2 在项目当前 Windows 环境真实运行 M0。**M0 未通过，停止后续阶段。M1、M2 和 Fabric 均未运行。** 第 1—9 节保留原预登记内容；以下记录实际结果。

正式命令：

```powershell
.venv\python.exe scripts/measure_continuous_motion_d101.py M0 --rounds 100 --output evidence/motion_navigation/continuous-motion-d101-v1/m0
```

runner 退出码为 1，保留 `passed=false`。按时和整体晚一 tick 各 100／100 完成并安全落地。两组输入哈希均为 `8e1c73bf14b044c746c557b2e569198860ea9b25f00f646a17a370bc55af02ed`，没有搜索或更换 D101 动作输入。

| 检查 | 实测 | 判定 |
|---|---:|---|
| M0-A 按时起点 | 100／100 完成并安全落地 | 通过 |
| M0-B 整体晚一 tick 起点 | 100／100 完成并安全落地 | 通过 |
| N1 首条晚两 tick | `CONTRACT_REJECTED`，无身体推进 | 通过 |
| N2 后续单条晚一 tick | `BOUNDED_DEVIATION`，原 owner 按已证明停止尾迹收尾 | 通过 |
| N3 重复／倒序首条回执 | 两项均 `CONTRACT_REJECTED`，无身体推进 | 通过 |
| 首条回执后的错误分支存活 | 0 | 通过 |
| 一致关键节点比较 | 400 次 `COMPARE_ONLY` | 通过 |
| 一致关键节点新增完整证明 | 0 | 通过 |
| 掉落、伤害超额、安全违规、UNKNOWN 放行 | 均为 0 | 通过 |
| 无人负责推进、错误 generation 接纳 | 均为 0 | 通过 |
| 完整决定 P95≤8 ms | 0.1683 ms | 通过 |
| 完整决定 P99≤15 ms | 48.6079 ms | **未通过** |
| 完整决定最大值<30 ms | 57.5597 ms | **未通过** |

正例分母为 200。四项负例单独运行一次，不进入正例成功率。所有场景合计 4,710 个完整决定，P50 为 0.0886 ms；完整证明 204 次，physics step 为 87,735。实际推进为 4,707 tick，其余物理步来自初始证明扫描。每个 generation 使用进程内单调分配、互不重叠的 control_sequence 区间。

完整决定从建立请求、generation 和账本前开始计时。第一条决定包含完整 `scan_commitment`、依赖绑定、范围构造和该 tick 的协议／物理处理。每个后续决定包含输入资格检查、依赖比较、物理推进、立即首条回执或关键节点比较，以及结果构造。fixture 和冻结输入准备、写盘均在计时之外。204 个包含初始证明的决定 P50／P95／P99／最大值为 48.3461／48.9930／57.2872／57.5597 ms；另外 4,506 个决定为 0.0884／0.1146／0.1675／0.1893 ms。这两组只用于解释失败位置，正式判定仍使用全部 4,710 个决定。

本闭环逐 tick 提交，决定边界没有已进入采样的未来命令。完整扫描为每个边界绑定空的不可撤回输入事实，并据此证明停止尾迹。N2 在首条命令已应用、仍可安全放弃时注入：下一候选只在原绝对期限后一 tick 才被提供，门禁拒绝应用它，到期 tick 执行已经证明的停止尾迹。没有重新选择 0／1 tick 分支，也没有将缺输入任意解释为中性输入。本结果只覆盖这项承诺前负例；不宣称撤销、替换、承诺后欠载或真实传输时序已经实现。

关键节点位置和速度范围取完整证明中的单点范围。比较绑定实际分支、完整 generation 和依赖。扫描产生的承诺点、恢复落地点在运行时只比较；没有扩大旧范围，也没有使用外力归因容差。该事实只验证确定性计算器中的一致状态，不代表 Fabric 身体噪声已经处理。

直接检查命令：

```powershell
.venv\python.exe -m unittest tests.motion_nav.test_d101_input_stream tests.motion_nav.test_d101_continuous_probe tests.motion_nav.test_d101_measurement -q
```

23／23 通过。计时测试用受控时钟确认物理步、首条回执处理和关键节点比较都被完整决定时间覆盖。两个新增模块各自先完成模块缺失红测。

证据位于 [`evidence/motion_navigation/continuous-motion-d101-v1/m0/`](../../../evidence/motion_navigation/continuous-motion-d101-v1/m0/)：`manifest.json`、`summary.json`、`source-hashes.json`、固定 gzip 时间戳的 `decisions.jsonl.gz`、`runs.jsonl.gz` 和 `fixture.jsonl.gz`。测量父提交为 `68ac47d8a131398684c20cdc4ba186cfe85a0f48`，测量源码树哈希为 `4aeea7d920cba0b730ba545426a9abfebaf3c6442d3d90ca63df159bbe9f1500`。逐文件哈希见 manifest；核验记录见 `verification.json`。

`--recompute` 从压缩原始记录重算同一失败判定，退出码仍为 1。独立 `recompute_evidence.py` 校验全部源码和证据哈希，重新计算 204 份原证明，并回放 4,707 次已记录身体推进；所有状态、证明哈希、单调序号、关键节点范围和统计一致，退出码 0。独立核验新增计时样本为 0，没有重跑或替换正式性能证据。

仓库的 `core.autocrlf` 可能在以后检出时改变文本字节。`measured-bytes.zip` 因此保存测量时全部源码和原始证据的精确字节，共 103 个文件；压缩包内每个源码／原始证据的哈希已经与原 manifest 核对。复核者可将压缩包解压到独立目录，再用项目 Python 运行其中的 `recompute_evidence.py`；避免把检出时的换行变换误记为原测量遭到修改。原 manifest 和原始计时没有改变。

本轮没有创建 worker，没有修改 `mc2p/**`、正式 Runtime／Session／规划器／执行器，没有运行 Fabric，也没有改写 D098 或 E13／E14。M0 的 P99 和最大值未达到冻结门槛，因此不得进入 M1。

## 11. Task 6 收尾核验

2026-10-11，**D101 最小原型止于 M0，整体未通过。** M0 功能和安全条件通过，正式性能失败；Task 3／4／5 未获授权，M1／M2 未实现、未运行，Fabric 未运行。本文开头及第 1—9 节保留运行前的预登记文字，当前结论以第 10—11 节为准。收尾没有改变矩阵、门槛、分母或原始耗时。

### 原始证据复核

对 `97b13ea5c160becdb6ec0b86482d5ded537e76ad` 提交的 M0 证据运行：

```powershell
.venv\python.exe scripts/measure_continuous_motion_d101.py M0 --output evidence/motion_navigation/continuous-motion-d101-v1/m0 --recompute
.venv\python.exe evidence/motion_navigation/continuous-motion-d101-v1/m0/recompute_evidence.py
```

第一条命令退出码 1，重算的全部字段与原 `summary.json` 一致，仍是 P99 48.6079 ms、最大值 57.5597 ms 未通过。第二条命令退出码 0，204 份原证明和 4,707 次身体推进逐项一致；证明扫描 83,028 步，加实际推进后共 87,735 physics step。两次核验新增计时样本均为 0。

原 manifest、源码哈希清单、原始压缩记录、复核输出和精确字节包共十个文件的当前 SHA-256 写入[根清单](../../../evidence/motion_navigation/continuous-motion-d101-v1/manifest.json)。字节包共 103 个文件，其中源码和原始证据逐个与原 manifest 的哈希一致。原 M0 目录没有修改。

### 收尾检查与限定修正

Task 6 冻结命令中的 `tests.motion_nav.test_d101_fabric_tick_probe_options` 本应由 Task 5 创建。M0 未通过后 Task 5 未获授权；该模块记录为**未授权、未创建、未运行**，不计作跳过或通过。本次执行原命令中其余九个已存在模块：

```powershell
$env:JAVA_HOME = 'D:\My_project\mc_ai\.venv\Library\lib\jvm'
$env:PATH = "$env:JAVA_HOME\bin;$env:PATH"
.venv\python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_m0_probe tests.motion_nav.test_incremental_motion_controller tests.motion_nav.test_incremental_motion_proof tests.motion_nav.test_incremental_motion_closed_loop tests.motion_nav.test_d101_input_stream tests.motion_nav.test_d101_continuous_probe tests.motion_nav.test_d101_measurement tests.test_standalone_java_gates -q
```

首轮未设置 JDK 环境，运行 96 项，退出码 1，得到一项失败和三项错误。三项错误均因 shell 没发现已安装的 JDK 21。一项失败来自既有导入检查：Task 2 新增的实验 runner 没有登记，检查将其两项实验导入报告为生产导入。

限定修正只修改 `tests.motion_nav.test_trajectory_proto_contracts`：按完整路径登记 `scripts/measure_continuous_motion_d101.py`，只放行该文件已有的两个实验模块导入。新增反例检查其他脚本、同名嵌套脚本和 `mc2p` 文件仍被拒绝，该 runner 直接导入整个 `experiments` 也被拒绝。没有使用目录通配，没有改实验行为。JDK 使用项目已有的 OpenJDK 21.0.10，只临时设置检查进程环境。修正后上述命令 **97／97 通过**，耗时 51.131 s，退出码 0，失败、错误和跳过均为 0。

### 未实施阶段和身体责任

通过路径检查确认 `m1/`、`m2/` 证据目录不存在。通过源码检查确认双跳接口 `TwoJumpProbeSpec`、`PreparedSecondJump`、`D101PrecomputeWorker`、`SecondJumpPreparationRequest`、`run_two_jump_probe`、`TwoJumpProbeResult`、`M1Summary`、`run_m1` 不存在；runner 仍只支持 M0。

M2 的 `D101TickTrace.java`、`D101TickTraceTest.java`、两个 Fabric runner 文件和参数测试均不存在。deployment 与启动脚本中没有 D101 tick 探针标记；相对计划提交 `bab72bcb`，`mc2p`、deployment、既有 deployment launcher 和 Java 门禁文件无差异。

原 M0 manifest 的 worker 创建数和结束存活数均为 0。M0 没有 worker，因此没有应记录却缺失的 close 事件；不能把这一点写成 M1 worker 生命周期通过。收尾进程检查没有发现命令行包含 `d101-precompute` 的运行进程。

本次没有修改正式链或探针，没有运行 Fabric，没有 push，也没有创建未登记的阶段证据。真实客户端 tick 顺序、连续两跳、正式队列、撤销／替换／承诺后欠载和完整生命周期均未交付。D098 P3 仍失败，E13／E14 继续只作参考。继续实施或优化需要新的明确授权。
