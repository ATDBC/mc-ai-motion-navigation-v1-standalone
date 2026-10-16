# D102 提前准备运动证明：正式验收

日期：2026-10-13。当前状态：**G0／G1 已完成；G2 FAILED／STOPPED。Task 10、G3 和 Fabric 未开始。**

设计依据：[D102 决定](../decisions/0102-prepare-and-register-motion-proof-ahead-of-handoff.md)、[架构 §2—13](../architecture/prepared-motion-proof-pipeline-v1.md#2-owner-与长期状态)。精确分母与容量以[冻结 manifest](../../../tests/sim/manifests/prepared-motion-proof-d102-v1.json)为准。D101 M0、D098 P3 均保持 **FAILED**，不重写原始证据。

## 1. 验收顺序和证据边界

顺序固定为 `G0 → G1 → G2 → G3 → F-TICK → F-CONTINUITY → G4`。后一步必须关联前一步的 index 和 SHA-256。G0 通过只表示五组真实行为缺口和证据检查已经冻结，不表示实现通过。Task 1 的 RED-only 提交是阶段内暂存状态，不能充当最终交付。

Windows 是正式平台。普通 unittest 的工作文件只写 `TemporaryDirectory`。只有显式 `--record-contracts --output UNIQUE_RUN` 才建立持久证据目录，目录已存在就拒绝。`--verify-red` 只采 G0 五组，保存完整 AssertionError、输入、源码、命令和单组退出码。G1 的 13 个行为 producer 由后续 owner 任务提供；Task 1 不制造这些成功记录。

G0 五组使用现有 `MotionResultInbox`、`MotionWorkerPort` 和正式 `PhysicsWorldView.snapshot` 的薄适配器。不得用 ImportError、skip、空断言或 unittest 文字日志替代真实 RED。

## 2. 固定矩阵

| 阶段 | 冻结分母 | 判定范围 |
|---|---:|---|
| G0 | 5 | 父侧独立输入、内部不可变图、控制 drain 与 receiver 分离、计算退场保留执行引用、超期保留已登记尾迹 |
| G1 | 13 | manifest 中 13 个固定直接契约 case ID；13 种 typed 拒绝是子事件，不增加分母 |
| G2 fast | 144 | 3 平台宽度 × 2 起点 × 3 独立入口偏差 × 4 方向 × 2 积压 |
| G2 代表负例 | 12 | 每类保留 typed 结果、零未授权身体推进及原因 |
| G2 spawn smoke | 7 | 6 个基础场景＋1 个积压场景 |
| G2 realtime | 60 | 3 平台 × 2 起点 × 2 积压 × 5 次重复；20 Hz 完整控制负载 |
| G2 fallback | 6 | 已登记安全收尾；不得计作两跳完成 |
| G3 | 24 | 正式 Python 链：3 平台 × 2 起点 × 4 方向 |
| F-TICK | 40 | 四方向各 10 场，记录真实发送、采样、应用和 movement tick 顺序 |
| F-CONTINUITY smoke | 1 | 实机单次入口检查 |
| F-CONTINUITY 正例 | 24 | 3 平台 × 4 方向 × 2 起点 |
| F-CONTINUITY fallback | 4 | 四方向各 1 场安全收尾 |
| G4 | 2000／528／1904／216／1448 | v7／F2／v8／v9／coordination 原分母，逐项旧成功退步为 0 |

平台宽度为 2／3／4 格。起点偏移为 0／1 tick。入口偏差为 −0.05／0／＋0.05 格，分别施加在声明的入口坐标上；不能把多个偏差合并为一个“精确入口”样本。方向为南／西／北／东，积压为 0／1 个前序作业。范围入口必须有完整证明覆盖；单点 proof 不能扩大为 ±0.05 的范围资格。

G2 的 12 类负例固定为身份、计算 scope、输入、规则、UNKNOWN、入口、依赖、发布前取消、发布后取消、迟交付、中段缺失和 worker 硬退出。范围之外的偏差另测 typed 拒绝，不能列入正常完成。realtime 只量朝南精确入口，使用 50 ms 固定节拍、一个真实持久 spawn 和一个 receiver；10 次预热单列，不计入 60 次正式分母。fallback 必须真实越过 movement-tick 交接期限，6／6 安全收尾且第二跳授权为 0。

G3 精确入口之外，还要分别记录第一跳空中修订／取消、校验中取消、第二跳过期、worker 死亡、后续实际缺输入、真实偏差及目标 revision 改变但输入不变的反例。Fabric 固定 seed 21001；tick 缓冲最多 1024 个事件，溢出结果为 `INCOMPLETE`，未知 ID 写 null。tick 分类只允许 `NEXT_MOVEMENT_TICK`／`LATER_MOVEMENT_TICK`／`INCOMPLETE`。

## 3. 容量和扫描预算

全服务未完成作业最多 2 个，包含排队、运行和接纳。请求 FIFO、结果 FIFO 各 2；第三个未完成作业 typed 返回 `QUEUE_FULL`。登记 bundle 最多 8、snapshot lease 最多 8、局部快照最多 16,384 格。单份封存请求和结果各最多 2 MiB；snapshot／request／proof／transport 去重后的总量最多 16 MiB。诊断历史上限 64。承担身体责任的引用不得因满容量而被驱逐。

单次扫描最多 65,536 physics steps、主轨迹 40 tick、起点分支 2 个、每个边界安全尾迹最多 80 tick。容量和节点预算不足必须明确拒绝，不能等待、覆盖旧工作或无限重试。

## 4. 性能、安全和连续性门槛

完整控制决定必须满足 Windows P95≤8 ms、P99≤15 ms、最大值<30 ms。分位数按 nearest-rank 从原始纳秒样本重算。完整决定包括真实观察接收、handle 读取、实时比较、账本与回执、关键节点、伤害余额、监督者交接、仲裁与发送，不能只量一个查询函数。

控制线程的 scan、搜索、完整 proof hash、序列化／反序列化、整份快照构造和长锁等待调用为 0。后台成本、排队、发布到可用的延迟、真实首次消费、唤醒迟到、deadline miss 和关闭成本独立记录。不能把分项 P99 相加，也不能用发布前的 ready_at 代替真实发布。

准备及时且真实入口匹配时，落地到后续有效输入空档≤1 movement tick；安全平台不产生无必要完整停车。安全违规、未授权身体推进和旧成功退步均为 0。`BODY_RESPONSIBILITY_UNRESOLVED` 不计通过；安全收尾与任务成功分别报告。

## 5. 激活和引用必须使用真实事实

准备阶段绑定 generation、ledger／session、expected inputs、requested start 和 effect window，不预占 Runtime 全局序号。首条运动提议赢得最终仲裁后，Runtime 唯一输入出口创建 `StreamActivationBinding`，绑定 proof token、身体 owner、实际首 sequence、提交 tick 和实际复合命令身份。激活事实必须先于首条实际应用回执到达 tracker。发送时不能填写尚未发生的 actual effect tick。

handle、激活绑定和监督者交接必须同时匹配。拒绝绑定或过期 generation 的该 bundle 身体推进为 0。首回执立即选择唯一 0／1 起点；中段输入缺失、晚到或内容变化按偏离处理，不重新选起点。

registry 分别记录 candidate／executing／safe-tail 引用。计算退场不得注销已承担责任的引用。最后引用释放之前保留必要快照、bytes 和尾迹。释放执行或尾迹要求命令终态齐全、责任解决及监督者确认；服务关闭不能代替身体收尾。

## 6. 原始证据 schema 与校验入口

索引 schema 为 `d102-stage-index-v1`。每个唯一 run 的 `index.json` 写一次，包含 stage、run ID、UTC、Windows／Python 环境、命令、源码 commit 与逐文件 SHA-256、冻结 manifest SHA-256、历史失败状态、完整 case ID、相对 raw 路径、raw SHA-256，以及线程／激活／引用／实际发布／消费事件。源码副本保存在本 run，后续实现不会改变 G0 原证据。

raw schema 为 `d102-contract-raw-v1`。每项绑定同一 run ID、case ID、命令和退出码。input、snapshot、proof 必须有原始内容和规范 JSON 摘要，索引摘要必须与 raw 重算一致。G0 的 proof 和快照使用现有对象的完整可检查表示，只证明旧端口缺口，不冒充新 bundle 的规范序列化。正式 G1 以后必须记录规范的不可变请求、快照和 proof，及实际 producer 事件。

每条 Runtime activation 至少含 manifest 的十个绑定字段，并记录 `producer=Runtime`、`preallocated=false`。线程事件记录线程 ID／名称和操作；发布／消费事件必须指向同一 proof token；引用事件记录用途、使用者、计算退场和责任释放事实。负例需完整 typed 拒绝理由和独立推进计数。G1 必须覆盖 manifest 的 13 个固定 case ID 与全部 13 种拒绝子事件。

G2 以后每项 raw 与索引还必须绑定 `group` 和独立矩阵坐标 `axes`。坐标字段为 `platform_width`、`start_offset`、`entry_offset`、`direction`、`backlog`、`repetition`，只填写本组适用字段；重复序号从 1 开始。分母正确但坐标重复仍拒绝。阶段报告须提供 `resource_high_water` 中的全部数值容量实测上界，以及为 0 的 `responsibility_reference_evictions`。

`verify_stage_report(document, stage=...)` 返回错误元组，空元组才表示证据合格。调用者读取 index 后提供 `_index_path`；CLI 自动加入。校验器只读该索引明确列出的文件，不扫描共享目录、不读取文字日志来补缺证据。缺少 raw、激活关联、版本／摘要、线程、引用或实际发布／消费时拒绝。命令失败返回 1，证据合格返回 0。

```powershell
& .\.venv\python.exe -m unittest tests.motion_nav.test_d102_evidence_contract -v
$d102RedRun = Join-Path '.tmp/d102-formal-v1/red' ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ') + '-' + [Guid]::NewGuid().ToString('N'))
& .\.venv\python.exe scripts/verify_prepared_motion_d102.py --record-contracts --verify-red --output $d102RedRun
& .\.venv\python.exe scripts/verify_prepared_motion_d102.py --stage G0 --input (Join-Path $d102RedRun 'index.json') --verify-red
```

## 7. G0 RED 的解释范围

父侧输入组把两个真实求解成功结果送到旧接收端，显示该接收端没有父侧 expected inputs 绑定。快照组实际改动 detached WorldView 的内部字典。线程组通过受控 `MotionWorkerPort` 做真实 pickle 解包，记录同步 drain 所在线程。它们不修改生产实现。

退场与尾迹两组显示旧 inbox 只有 job 身份，计算退场后无法通过同一结果服务查询独立执行／尾迹登记引用。夹具同时保留已取出的 Python candidate 对象。该 RED 只说明 D102 新登记契约缺失，不声称原执行器已经丢失 proof、释放身体或发生安全违规。具体 owner 的执行、超期和关闭行为 RED 仍由后续任务补齐。

本轮没有 G1—G4、正式 Python 连续两跳或 Fabric 新结果。D101 M0 的性能失败、D098 P3 的安全和性能失败、原始采样及原因全部保留。有限预排程要等待 F-TICK 的真实 tick 顺序证据。

## 9. G2 首轮与 G2-F1 的失败边界

Task 9 已建立 Windows G2 量尺，但没有通过 fast。首轮只运行 1 项，因量尺把 receiver 一次循环误当作本作业已经发布而停止，结果 0／1。唯一一次已授权修正 G2-F1 只改为等待该 job 的 typed 终态／实际发布；旧 raw 不改写。

G2-F1 在第 25 项停止，结果为 24／25。该项是平台 2、起点晚一 tick、入口偏差 −0.05、朝南、无积压。首段证明允许 tick 11／12，但量尺生成 Runtime intent 时没有携带窗口，账本按 11／11 记录，tick 12 的真实应用被判为 `APPLIED_OUTSIDE_WINDOW`。原 owner 没到交接边界，第二跳授权为 0，typed 结果为 `NOT_READY`，物理安全违规为 0。

同一批还记录 2,009 次控制热路径禁止调用：750 次观察请求 JSON 容量校验、1,178 次不可变对象图遍历和 81 次失败路径回放相关调用。fast 144／144、12 类负例、spawn smoke、realtime、fallback 和正式 8／15／30 ms 都未获得证据。两轮最终资源字段均为 0，但资源关闭通过不能代替功能和热路径门槛。

失败指针为 `evidence/motion_navigation/prepared-motion-proof-d102-v1/g2-failure-index.json`。G2 保持 **FAILED／STOPPED**，不能作为 G3 predecessor。[D103](../decisions/0103-repair-g2-window-and-control-hot-path.md) 只授权最后一轮 G2-F2 的设计范围；实施与唯一正式重跑按对应计划和门槛执行。

## 8. 修正轮 1：重新封存也不能补造语义

独立审查发现，原校验器只要求摘要一致，因而会接纳 null payload、空事件占位和缺少实际命令的重封存记录。这是验收校验缺陷，已按下列契约加固；原 G0 run 和五 RED 保持原字节。

manifest 的 `evidence_semantics_revision=2` 冻结了 G1 三种非空 payload：`d102-prepared-request-v1`、`d102-snapshot-v1`、`d102-motion-proof-v1`。请求分别保存计算 scope／owner、输入流 generation、身体 owner、job token、路线／动作、账本／会话、精确输入、入口／出口、伤害余额和请求预算。快照保留来源 revision、局部版本、覆盖范围、逐格版本与碰撞事实、完整 ruleset 和 lease。proof 必须与独立请求绑定相同身份、输入、预算、规则和快照摘要，保留两条起点分支、完整主轨迹及每个声明边界的有限安全尾迹。每个轨迹状态保留完整原生 `PhysicsState`，不能用一段说明文字或坐标列表替代。ruleset 使用完整原生 `PhysicsRuleset`，还须与正式运行规则相同。这里检查结构、绑定与原生类型，不另写或复制物理公式。

G0 继续使用已声明的旧对象表示，但三个 payload 都必须为非空对象，并与该 case 相符。控制 drain 必须记录与 control thread 匹配的真实 `deserialize`；两组退场必须记录同一计算 identity 的 result available／job retired，并保留 candidate 对象、用途和线程事实。其他 G0 case 的不适用事件允许为空；prepared activation／publication／consumption 仍全部为空。

G1 的 `g1_case_semantics` 按 13 个固定 case 指定实际操作、发布、激活、用途、首回执、交接、偏离、取消顺序、尾迹和关闭事实。必需事件由 case 决定，不由 `published`／`requires_activation` 等自报开关决定。任何存在的事件都检查非空 operation、时间及 proof token／generation／身体 owner／ledger／session；开关必须与实际事件一致。引用还要绑定 candidate／executing／safe-tail 用途、具体使用者及计算退场。释放时必须引用独立 Runtime 命令终态事件和 `ExecutionSupervisor` 释放事件的 ID，三项 true 不能代替这些关联。未解决责任的 close 事件保留只读 proof、snapshot lease 和 safe-tail，不得出现责任释放。

每项实际命令保存 `command_argv`、可重建的命令文字和整数退出码。G0 必须运行与 case ID 相同的独立 unittest。原 G0 没有 argv 时，仅按 Windows 原命令解析，仍检查确切的模块与 case。G1 使用 `tests.motion_nav.d102_contract_cases.<固定 case ID>` 作为 producer 映射，实际命令必须为直接 `--record-contracts` 模式，不能把 G0 的 unittest 命令改成 PASS。此处只冻结映射，13 个实际 producer 仍由后续任务接入。

Runtime activation 的 generation 是非负整数；proof token、身体 owner、ledger／session 和实际复合命令身份是非空字符串。sequence、tick、窗口和事件时间也有具体类型。它们分别与独立请求、登记记录、Runtime 实际命令及首回执比较。首回执不得先于激活，必须用实际首序号选择唯一 0／1 起点并剪掉另一分支。

原 G0 manifest 摘要只加入 G0 的明确历史白名单；G1 只接受当前冻结摘要。分母、容量、预算和 D101／D098 历史失败哈希没有改变。修正负例只在 `TemporaryDirectory` 的复制件中重封存，正式 G0 仅只读复验，不作为新性能采样。

修正轮 2 补齐同一项 proof 关联检查：主轨迹和每条安全尾迹统一核对请求会话、ruleset ID 和 state schema。`boundary_index` 必须为合法整数，不能用 bool 替代。尾迹首个完整状态及 movement tick 必须与所属 branch 的对应边界完全相同；另一世界、另一规则或脱离边界的尾迹不能作为收尾依据。检查仍只验证已记录事实，不重写物理公式或重跑轨迹。

## 10. D103 G2-F2 的止损结果

D103 的 Task A／B 已完成定向实现与独立审查。Task A 统一了动作决定到 Runtime intent 的执行窗口转换，关闭量尺遗漏 11／12 窗口的问题。Task B 移除了控制路径重复的观察请求容量 JSON 编码，同时保留公共校验和真实 backend 编码。这两项没有形成新的正式 G2 样本。

Task C 的前置 RED 证明，legacy 第一段没有 registry handle 或登记的安全尾迹。late delivery、fallback 和正常 closeout 都需要在控制线程现场运行 `verified_ground_rollout`。删除或改名这些调用会放宽安全门槛；候选第二段的尾迹也不能授权第一段身体。因此本轮按计划停止，没有实施可信引用优化，没有建立新 G1 predecessor，也没有运行正式 F2。

止损证据使用六个真实 spawn 单例。late delivery／fallback 各记录 2 次现场 rollout，正常 closeout 记录 3 次；中段缺失后 executing／safe-tail、lease 和 bytes 保留，live worker／receiver／feeder 为 0。独立审查批准的是失败收尾，不是 Task C 或 G2 通过。Task D、Task 10、G3 和 Fabric 仍未授权。
