# 连续动作证明性能与偏离验证验收

日期：2026-10-11。状态：进行中；W0、P1、P2 已完成，P3—P4 未开始。

## 1. 证据边界

| 证据 | 能说明什么 | 不能说明什么 |
|---|---|---|
| W0 Windows 原样复测 | 三方试做在正式平台的常数成本 | 项目已经实现控制器、表或逐次证明 |
| P1／P2 组件 | 项目自有实验与扫描语义一致 | 正式控制链能够按期执行 |
| P3 偏离闭环 | 预计算作废和重算在实验闭环中的表现 | 正式 Runtime、仲裁、客户端输入账本、Fabric 和真实服务端表现 |

## 2. W0 Windows 实际结果

W0 复现完整性通过，T2／T3 的 10／50 ms 探索性能也通过。T3 普通决定的最大耗时为 **243.2021 ms**；这条长尾保留，不能把探索通过写成正式闭环通过。

### 2.1 来源与环境

- 项目源码基线 `54b01866`，采集工作树 HEAD 为计划提交 `7722dbdc`；`mc2p` 和既有轨迹实验源码与基线没有差异。
- 三方提交 `f5e43e7`，归档 SHA-256 为 `72ce0d2bfadc0e4ad697d1f0c36236ae1961432635c0e9a56dc96937fbd5959a`。26 个源码与输入文件均逐字节符合归档，表符合原冻结记录。
- Python 3.11.16（conda-forge），psutil 7.2.2；Windows 11 专业版 10.0.26200。Python 3.11 的原始平台字符串为 `Windows-10-10.0.26200-SP0`。
- CPU 为 Intel Core Ultra 7 270K Plus，24 核／24 线程。
- Windows 包装器先启动 psutil 的负载采样并等待 5.5 秒，再运行原脚本。两次运行的开始和结束读数均为实际 `(0.0, 0.0, 0.0)`；没有构造零值。该接口读数不单独证明机器空闲，原 summary 的 `idle_check` 如实保留。

包装器源码、三方全部代码和项目计算器源码哈希见 [manifest](../../../evidence/motion_navigation/incremental-motion-r98-v1/w0/manifest.json)。关键输入如下：

| 冻结输入 | SHA-256 |
|---|---|
| `m1/table.json` | `fc33cec9c5d610c8b8bfb1106f9255235b7f5c35252294bcf1af03b1319c3fc7` |
| `m1/accepted_candidates.jsonl` | `d3cb01d534ba43addd5a19b07be2b6f5ebbdf763a4552d5735be0d8f3646b380` |
| `m2/set_a_inputs.json` | `d5cb9bd493b9605d697583c0b80decd8cd465535300f03fb00ab30d1bf8f9a7d` |
| `m2/set_c_inputs.json` | `0c5abf409ca3a0560c9773bfdc8e1f9dff719a95b7933a015e3107046c73ed93` |

### 2.2 命令与规模

```text
.venv/python.exe scripts/measure_d098_trial_windows.py T2 --trial-dir .tmp/d098-review-f5e43e7/reviews/2026-10-10-d097a-trial --output-dir evidence/motion_navigation/incremental-motion-r98-v1/w0
.venv/python.exe scripts/measure_d098_trial_windows.py T3 --trial-dir .tmp/d098-review-f5e43e7/reviews/2026-10-10-d097a-trial --output-dir evidence/motion_navigation/incremental-motion-r98-v1/w0
```

包装器通过 runpy 原样调用 `run_m2.py perf_record --no-setd` 和 `run_m2.py perf_record --passes 1`。T2 为 A 23＋C 120 个候选，3 遍；T3 为 A 23＋C 120＋D 25,197 个候选，1 遍。各预热 10 个候选。包装器总墙钟分别为 76.6266 秒和 3,839.7220 秒；这些总时长包含准备和证据整理，不用于决定延迟判定。

### 2.3 F1 决定延迟与物理步数

F1 是原冻结的 60 步提前证明配置。下表采用 nearest-rank 分位数；判定 P95 取 pooled P95 和各遍 P95 中的最大值。

| 批次／F1 决定 | 样本数 | P50 ms | pooled P95 ms | P99 ms | 最大 ms | 各遍 P95 ms | 判定 P95 ms |
|---|---:|---:|---:|---:|---:|---|---:|
| T2／普通 | 8,349 | 0.0124 | 6.0973 | 6.6056 | 7.2456 | 5.9803／6.1298／6.1356 | 6.1356 |
| T2／承诺 | 291 | 0.0287 | 11.1908 | 15.7946 | 19.7161 | 10.7826／13.6474／13.5831 | 13.6474 |
| T3／普通 | 637,911 | 0.0105 | 5.2729 | 6.2184 | 243.2021 | 5.2729 | 5.2729 |
| T3／承诺 | 10,627 | 0.0275 | 0.0316 | 0.0495 | 19.6163 | 0.0316 | 0.0316 |

| 批次／F1 决定 | 物理步数 P50 | P95 | P99 | 最大 |
|---|---:|---:|---:|---:|
| T2／普通 | 0 | 76 | 84 | 87 |
| T2／承诺 | 0 | 180 | 266 | 266 |
| T3／普通 | 0 | 78 | 86 | 92 |
| T3／承诺 | 0 | 0 | 0 | 266 |

T2 普通／承诺决定的判定 P95 为 6.1356／13.6474 ms；T3 为 5.2729／0.0316 ms。两组普通样本均不少于 1,000，分别通过 10／50 ms 探索线。T3 的承诺决定多数复用提前取得的结果，计算已经计入此前决定；三个配置的逐候选总物理步数仍相同。

strict、F1、full_scan 的完整统计，包括 plan、final、候选总时长和物理步数，分别保存在 [T2 summary](../../../evidence/motion_navigation/incremental-motion-r98-v1/w0/T2-summary.json) 和 [T3 summary](../../../evidence/motion_navigation/incremental-motion-r98-v1/w0/T3-summary.json)。T3 的原始长尾没有删除，也没有归因于尚未证明的系统调度问题。

### 2.4 复现完整性与压缩证据

完整性结论现由 v2 检查从实际样本推导。检查读取 `columns`，把列名及除原 schema 的 `ms` 计时列以外的所有字段纳入签名；新增字段不会被位置切片漏掉。T2 的 19,395 条与 T3 的 1,423,766 条样本保留原顺序，列与非计时字段签名均符合三方冻结记录。

三种配置必须覆盖相同的完整 `(pass,cand)` 集合，`full_scan` 总行数必须等于候选数乘遍数。T2／T3 的实际 full_scan 行数为 429／25,340；三配置的候选运行出现次数也分别都是 429／25,340。原 C 集合存在 18 个重复候选名称，因此不同名称只有 125／25,322 个。检查还比较每个 `(pass,cand)` 的 `plan`／`full_scan` 计数，避免同名候选缺一次运行仍被集合比较放过。

原计时样本没有独立分类列。`all_candidates_verified_in_original_harness` 由完整覆盖、出现次数、原脚本代码哈希以及与冻结样本一致的签名共同推导，不再固定为 true。原脚本在生成这些样本前会断言候选为 `VERIFIED_CANDIDATE`。429／25,340 个候选运行的三配置总步数检查通过，各类步数分布也完全一致。

原 summary 字节直接保存，原样本使用固定 `mtime=0` 的 gzip 压缩。T2 的 1,158,680 字节压缩为 89,591 字节；T3 的 97,593,203 字节压缩为 6,515,127 字节。每组未压缩和压缩 SHA-256 记录在对应 result 与 manifest 中。

复审还修复旧输出复用：包装器调用前删除 `performance_record.json` 和 `performance_record_samples.json`，调用后要求两份文件都重新生成。0 退出但缺输出仍失败。这个保护适用于后续调用，不回写成首次采集已经具备该保护。

本次修正从原 summary 与 gzip 重算完整性，**没有重跑 T2／T3**。两组原 summary、gzip 和 execution 字节均与首次提交 `0ec6cd5d` 相同；原 execution 中的包装器哈希和内嵌结果继续表示首次采集。当前 result 文件为 v2 审计，重算命令及原字节核验见 [integrity-recheck](../../../evidence/motion_navigation/incremental-motion-r98-v1/w0/integrity-recheck.json)。

包装器 16／16、原 TP／D097 与导出 113／113、证据哈希核验均通过；检查命令与 TDD 红绿过程见 [verification](../../../evidence/motion_navigation/incremental-motion-r98-v1/w0/verification.md)。

W0 允许进入预登记的 P1。P1—P4 尚未实施，正式实验闭环仍要在 P3 按 8／15／30 ms 测量。D097 的 M0 失败不改写，本次没有新 Fabric 结果。

## 3. 语义等价表

P1／P2 对每个冻结候选记录：

- 完整结果类型和原因；
- 两个时序分支；
- 最后可放弃边界、首个承诺边界和恢复边界；
- 风险区间；
- 依赖格；
- 伤害；
- 主轨迹和输入哈希；
- physics step 数。

只要出现旧扫描拒绝而新证明许可，当前实现立即失败。

### 3.1 P1 结果

P1 没有把第三方试做目录复制进运行路径。项目导入的是冻结数据及其来源清单，代码位于 `experiments/motion_navigation/trajectory_proto/`。主要核对如下：

| 检查 | 结果 |
|---|---:|
| 冻结表 | 147／147 行，原 SHA-256 一致 |
| 控制器冻结样本 | 18／18 输入逐 tick 一致 |
| 独立批量扫描签名 | 8／8 状态、原因、step、状态、风险与依赖一致 |
| trace 伪造和篡改 | 全部拒绝复用 |
| 七类故障注入 | 7／7 被检查发现 |
| typed 风险／物理拒绝 | `UNRECOVERED_RISK`、`INVALID_PHYSICS` 均覆盖 |
| P1 直接检查 | 24／24 |
| 原 TP／D097 与导出 | 113／113 |

三方在 P1 开工前已经冻结 8 个样本的状态、原因、step、风险区间及等价结论；项目保存了对应的只读子集和原文件哈希。更细的状态与依赖哈希由**未修改的原批量扫描器**独立生成。项目提供 `scripts/generate_d098_reference_signatures.py --check`，签名文件记录输入哈希、scanner 源哈希、生成命令和基线提交；检查会从冻结输入重新运行 scanner。新旧比较没有让两边共同调用新的风险或伤害实现。

### 3.2 P1 Windows 未优化基线

命令：

```text
.venv/python.exe scripts/measure_incremental_motion_r98.py --limit 6 --rounds 20 --output evidence/motion_navigation/incremental-motion-r98-v1/p1/windows-decision-baseline.json
```

| 集合 | 决定数 | P50 | P95 | P99 | 最大值 |
|---|---:|---:|---:|---:|---:|
| A | 80 | 27.0582 ms | 31.9329 ms | 37.4973 ms | 37.4973 ms |
| C | 120 | 50.1567 ms | 55.1183 ms | 60.7872 ms | 63.4865 ms |
| D | 120 | 60.8228 ms | 66.6199 ms | 66.6890 ms | 66.7190 ms |
| 合计 | 320 | 53.8193 ms | 66.4301 ms | 66.6814 ms | 66.7190 ms |

每次只创建一个请求预算 owner。A／C 复用冻结输入，因此查表和控制器明确为 no-op；D 才运行真实查表和控制器。三组都执行 trace、证明和许可核对，`physics_steps` 是控制器、trace 与证明的总和。不能用其中一个局部耗时代替总耗时。本量尺仍一次完成整条候选的逐次证明，目的是冻结 P2 前的成本。它没有通过 8／15／30 ms，也没有按 W0 的逐 tick 样本口径判断 10／50 ms。

### 3.3 P2 结果

| 切片 | 结果 |
|---|---|
| 增量伤害 | 等价状态访问 3,668,780→185,480；数值继续覆盖 0／3／4／5／7 点和未完成下落 |
| 可信主轨迹 | 320 次复用均为 `VALID`；总 physics step 212,520→193,440；错误身份和篡改回到完整重算 |
| 固定分摊 | A＋C 选择 24 步；普通／承诺 P95 7.4811／19.6569 ms；D 验证 3.2616／13.4152 ms |

五档 A＋C 校准都保持同一语义签名和 94,720 个总 physics step。选择 24 步以后才读取 D，D 没有参与参数选择。D 的语义签名保持一致，总 physics step 为 98,720。

| 集合／决定 | 样本数 | P50 | P95 | P99 | 最大值 |
|---|---:|---:|---:|---:|---:|
| A＋C／普通 | 3,600 | 1.9664 ms | 7.4811 ms | 15.7879 ms | 31.9964 ms |
| A＋C／承诺 | 100 | 3.8533 ms | 19.6569 ms | 19.6838 ms | 19.6852 ms |
| D／普通 | 3,960 | 1.9507 ms | 3.2616 ms | 21.5821 ms | 36.8319 ms |
| D／承诺 | 40 | 0.0729 ms | 13.4152 ms | 28.4262 ms | 28.4262 ms |

P2 的 8／45 ms 校准线通过。A＋C 与 D 仍有 P99 或最大值超过后续闭环 15／30 ms，因此不能把 P2 写成正式期限通过。证据位于 `evidence/motion_navigation/incremental-motion-r98-v1/p2/`。本轮没有修改 `mc2p`，没有运行 Fabric，也没有实施内部状态构造优化。

### 3.4 P3 滚动闭环结果

命令：

```text
.venv/python.exe scripts/measure_incremental_motion_p3.py --rounds 3 --output evidence/motion_navigation/incremental-motion-r98-v1/p3/closed-loop-matrix.json
```

矩阵每轮 37 个场景。位置 0.01／0.03 格、朝向 2°／5°、无关世界格、相关格改变、相关格 UNKNOWN、横向移动 1 格的真实 `GoalState` 修订、取消、晚一 tick 和丢应用记录，分别在承诺前、承诺边界和承诺后注入。另有精确按时、精确晚一 tick、转向后上一格和跨隙后行走四项基线。

| 指标 | 三轮结果 |
|---|---:|
| 场景运行 | 111 |
| 决定样本 | 1,338 |
| physics step | 45,504 |
| P50／P95／P99／最大值 | 0.2521／15.8503／19.8099／20.0919 ms |
| 作废事件 | 54 |
| 作废场景占比 | 48.65% |
| 同步重证 | 69 |
| 最大连续重证 | 1 |
| 身体责任未解决 | 54 |
| 其他安全失败 | 6 次伤害超额 |
| 安全门槛失败／无界结果 | 60／0 |
| 承诺后恢复 | 0／27 |
| 已授权后端推进 | 1,227／1,227 |

精确路径全部完成，实际应用的输入哈希与冻结序列相同。跨隙后行走场景另外追加一条已证明的地面前进命令，直接记录落地边界、落地速度和 owner 连续性。每次模拟客户端推进都能追溯到已有应用记录或证明覆盖的在途时序分支；没有直接使用 `stop_input` 补空白 tick。

应用记录先于动作阶段计算。承诺命令生效后的下一观察明确进入 `POST_COMMIT`。应用窗口最后一个 tick 在回执摄取前仍是 pending；关闭该 tick 后，同一观察转为 missing。这个次序有专门检查，不能靠额外推进一 tick 才发现丢失。

P3 没有通过，原因有三项：

1. P95 和 P99 分别为 15.8503 和 19.8099 ms，超过 8／15 ms 门槛。最大值 20.0919 ms 通过 30 ms 门槛。
2. 承诺后 27 个需要恢复的运行，没有一个在作废旧许可后同步取得新恢复证明。54 个承诺边界或承诺后运行明确返回 `BODY_RESPONSIBILITY_UNRESOLVED`，作为安全失败计数，不能写成有界安全结束。
3. 晚到注入在承诺边界或承诺后造成 6 次 11 点伤害，超过任务额度。这 6 次也进入安全失败分母。

正常 proof 和四个专用反例都经过 `RollingProofOwner.submit_through_gate()`。四个变异分别注入 fault proof、permit 或错误命令；门禁全部拒绝，ledger 新增提交均为 0。伪造的 `AuthorizedAdvance` 也不能推进后端。显著移动的新目标在承诺前返回 `candidate_terminal_goal_mismatch`；0.01 格且旧终态仍满足的小修订保留重证正例。取消请求会持续保持到安全释放或明确的责任未解决终态。本轮没有承诺后取消重证成功的样本；6 个承诺边界或承诺后取消均明确返回 `BODY_RESPONSIBILITY_UNRESOLVED`，没有用旧后缀继续。P3 由此停止，P4 未授权。完整逐场结果见 `evidence/motion_navigation/incremental-motion-r98-v1/p3/closed-loop-matrix.json`。

## 4. 性能记录

分别报告：

- 查表；
- 控制器生成输入；
- 主轨迹模拟；
- 停车尾迹；
- 伤害累计；
- 许可构造；
- 偏离比较；
- 作废后的重算。

平均每 tick 耗时只作容量说明，不用于通过判定。

## 5. 偏离矩阵

| 类别 | 样本 | 必须报告 |
|---|---|---|
| 精确跟随 | 按时、晚一 tick | 复用率、决定延迟 |
| 位置偏差 | 横向 0.01／0.03 格 | 作废点、重算和停止 |
| 朝向偏差 | 2°／5° | 入口变化和重新取参 |
| 世界变化 | 无关、相关、UNKNOWN | 依赖判断和信息等待 |
| 任务变化 | 目标修订、取消 | 旧许可失效和身体责任 |
| 连续动作 | 转向→上一格、跨隙→行走 | 接续速度、停车和期限 |

每一类偏离都分别在承诺前、承诺边界和承诺后注入。承诺后旧后缀失效时，另行记录 owner 是否保留、恢复证明是否及时产生，以及未取得证明时是否有界失败。

## 6. 正式关闭条件

本验收只有同时满足以下条件才能记为通过：

- W0 的复现完整性通过；W0 性能作为基线单列；P1、P2、P3 依次通过；
- Windows 实验闭环决定 P95≤8 ms、P99≤15 ms、最大值<30 ms；
- 安全类违规为 0；
- 7 类变异全部被检查发现；
- 偏离后没有无界等待或无界重算；
- 原 TP、D097、D092—D094 的冻结结果没有被改写；
- `mc2p` 正式链保持未修改。

Fabric、任意几何和正式接入不属于本轮关闭条件，也不能由本轮结果推断。
