# D121 Task 4–5 审查，以及客户端应用前时序协议是否要单独决定（dfaa0b4f）

日期：2026-10-20。审查对象：`origin/main` dfaa0b4f（“Publish D121 timely handoff and transport stop evidence”），对照上一轮的 3889d10。

平台说明：

- 本轮只在 Linux 上运行项目函数、项目夹具和项目测试，项目树本身不改；唯一的对照树（E125）只把只有行尾变化的文件换回 3889d10 的原字节。
- 没有运行 Java 或 Fabric。Java 客户端的结论来自源码阅读，文中给出文件和行号。
- 脚本和输出在 [`2026-10-20-d121-transport-review/`](2026-10-20-d121-transport-review/)。

## 结论

1. **方向对。Task 4 的 PASS 在它声明的范围内成立，Task 5 停得对。**
   - Task 2–4 与上一轮讨论的方向一致：不新增联合消息，用紧凑可行性绑定，后继完整证明走 D117 路径，`L` 决定 `L+1`，默认潜行。
   - Task 4 在正式 Runtime 链上跑了 88 场。worker 晚到、never、dead 和四类拒绝都在 tick 27 进入 fallback。
   - Task 5 的 RED 是真实的。它命中了 D121 §6 自己登记的停止条件“`L` 帧选择不能稳定在 `L+1` 生效”，而且没有用事后的 `APPLIED_OUTSIDE_WINDOW` 冒充通过。
   - 我上一轮把“错过 `L+1` 就作废”写成了修订前提，却没有核对客户端能不能执行它。这是上一轮审查的遗漏，Task 5 把它补上了。
2. **需要单独写一份决定（下称 D122），不能算 D121 的局部修补。**
   - 它改的是输入出口的公共契约：`ActionSnapshotV1`、wire schema、Java 请求门禁和采样、`CalculatorBackend` 的租约语义，以及会话握手。
   - 这些都在 D121 的授权之外。按 AGENTS.md，公共契约变更要同步生产者、消费者、测试和四类文档。
   - D121 应按现状收口为 PARTIAL／STOPPED。D122 通过后，再用它重开 D121 的 S3／S4。
3. **只把精确 tick window 送进客户端不够。还必须在 `L` 之前，把安全默认放到客户端手里。**

   E123 用 D120 冻结的四方向 972 个落地入口，比较 `L+1`、`L+2` 两拍实际用了什么：

   | `L+1` 实际输入 | `L+2` 起 | 停在中间平台 | 到达目标 | 掉落 | 对应情况 |
   |---|---|---:|---:|---:|---|
   | 潜行 | 潜行 | 972 | 0 | 0 | 默认已在客户端，后继没到 |
   | 不按键 | 潜行 | 0 | 0 | 972 | 只加窗口：迟到后继被拒，但 `L+1` 是租约耗尽的 neutral |
   | 不按键 | 起跳 | 0 | 0 | 972 | 现行协议，迟到一拍补跳（Task 5 late） |
   | 不按键 | 不按键 | 0 | 0 | 972 | Task 5 never 实际发生的情况 |
   | 潜行 | 起跳 | 0 | 0 | 972 | 只有默认、没有窗口：过期后继仍被采样 |
   | 潜行 | 不按键 | 0 | 0 | 972 | 默认生效后，Python 又改发 neutral |
   | 前进+疾跑 | 潜行 | 0 | 0 | 972 | 默认改成保持动量，后继没到 |
   | 前进+疾跑 | 起跳 | 0 | 972 | 0 | D120 的 late1；只在后继一定会到时成立 |
   | 起跳 | — | 0 | 972 | 0 | 及时后继 |

   - **两样缺一不可。** 窗口挡住过期起跳，默认负责 `L+1` 那一拍。只有“潜行，潜行”一行能安全停下。
   - **潜行至少要保持 8 拍。** 潜行 k 拍后松键：测到的 k=1、2、4、6、7 都至少有 648 个入口掉落；k=8、9、10、12、16、20 时 972 个全部停稳。
   - **Task 4 的 fallback 本身也受这个限制。** 它和后继在同一个 `L` 帧（同一个 sequence 26）里发出，transport 迟到时两者一起迟到。
   - **Python 在 `L` 之后无法补救。** Python 最早在 tick 27 的观察后改发潜行，客户端最早在 tick 28 用上，这就是“不按键，潜行”一行。在 Fabric 的一问一答节奏里还更晚：`L` 帧没执行，客户端就不发下一次观察。
4. **“等价的撤销信息”并不等价。** 撤销和它要撤销的请求走同一条有序队列。
   - **队列顺序：** Task 5 late 场里，tick 28 的 neutral 就排在 sequence 26 后面。
   - **Java 的 cancel：** 只撤销当前 owner；租约从到达时开始算。
   - **`stale_observation`：** 比较的是“交换代数”，不是 tick。即使改成每 tick 观察，它也只能拒绝，`L+1` 还是 neutral。

   只有在客户端采样点自行判断过期的窗口，才不依赖消息先后。
5. **D122 的最小范围是两个可选字段，加一项握手能力（§3）。**
   - **两个字段：** 首效窗口；租约用完后的一个预排默认（跳拍时保持什么）。
   - **不需要：** 通用排程器、多个默认，或把 generation 放进 wire。
   - **兼容问题必须先解决：** 现行 Java 解码要求键集合完全一致，解码失败会让整个部署会话进入失败。新字段只能在握手确认后发送。
6. **这次发布本身有几处要先修。**
   - **全量分片有 7 个模块新报错，都在 3889d10 上通过。**
     - 4 个由行尾改写造成，恢复原行尾后通过（E125）：R28 v8 冻结清单、F2REC 工具来源记录和 B01 参照场景 S04 的字节变了。
     - 另 3 个是两处代码回归（E126）。
     - 这命中 D121 §6 的停止条件“出现旧成功退步”。
   - **行尾统一改写了 1614 个文件：** 其中 787 个是历史证据，另外打断了 11 条“证据引用证据”的哈希。这命中 D121 §6 的另一条停止条件“无关行尾变化”。
   - **入口文档状态不一致：** AGENTS.md 和 README 仍写 `AUTHORIZED REVISION／IN PROGRESS`。
   - **复现命令失效：** S2 的复现命令指向已删除的脚本。

## 1. D121 Task 2–5 复核

### 1.1 改动规模

行尾之外的内容改动（`git diff --ignore-cr-at-eol 3889d10 dfaa0b4f`）：

| 类别 | 文件 | 行 |
|---|---:|---|
| 生产代码 `mc2p/` | 11 | +1202／−84 |
| 测试 | 12 | +1942／−115 |
| 脚本 | 7 | +1137／−345 |
| 文档（含 AGENTS.md、README） | 6 | +326／−30 |
| 新证据 | 98 | 全部新增，位于 `d121-momentum-handoff-v1/` |

生产改动集中在 `motion_chain.py`、`motion_candidate.py`、`motion_solver.py`、`execution_supervisor.py` 和 `player_runtime_v1.py`。没有新增 Session 状态、owner、账本或输入出口。Runtime 在 `L` 只提交一个动作；激活被拒时，用本地的 `IncumbentFallbackDispatch` 替换（`player_runtime_v1.py:455-472`）。这与 D121 的约束一致。

### 1.2 Task 4 的结论成立，但范围要写清楚

Task 4 的 88 场全部经过 `RuntimeNavigationDriver → Session → supervisor → route executor → coordinator → CalculatorBackend`，READY 来自原 producer／receiver／registry，结论可信。

需要写清的是：它验证的“迟到”只有 worker 迟到。到 `L` 仍未 READY 时，Python 在 `L` 选 fallback，fallback 在 tick 27 及时生效。transport 是准时的。

fallback 和后继一样，都是在 `L` 帧里发出的。所以 Task 4 的“fallback 安全”也以 `L` 帧准时到达为前提。Task 5 的 never 场就是这种情况：tick 27 是 sequence 25 的 `lease_exhausted`，之后 Python 一直发 neutral，身体在 z≈2.87、y≈61.3 处下落，只因夹具到了已知世界边界才没有记伤害。

### 1.3 Task 5 复核

从 `task-5-transport-late/matrix-final-audited.json` 读出 `L` 前后的提交和采样（F=前进，J=跳，S=疾跑，- 为不按键）：

| 场 | sequence 26（`L` 帧） | tick 27 采样 | tick 28 采样 | 之后 Python 提交 | 终态 |
|---|---|---|---|---|---|
| timely | 到达 tick 27，FJS | seq 26，FJS | seq 27，FS | 空中 FS | 目标平台 |
| late_l2 | 到达 tick 28，FJS | seq 25，`lease_exhausted` | seq 26，FJS | 全部 neutral | z≈4.93、y≈61.2，下落 |
| never | 未到达 | seq 25，`lease_exhausted` | seq 27，neutral | 全部 neutral | z≈2.87、y≈61.3，下落 |

三点可以确认：

- 过期起跳在物理里真实发生，不是账本分类问题；
- `L+1` 两场都是租约耗尽后的 neutral，按 E123 这一拍已经决定了掉落；
- 发现错拍之后，Python 发的是 neutral，不是 fallback 的潜行。按 E123，即使 `L+1` 是潜行，之后改发 neutral 也会 972/972 掉落。

两处小问题：

- never 场的 `protocol_prevented_expired_application` 记为 `True`，但它根本没有送达，不是被协议拦住的。验收汇总已用新门槛排除了这种冒充；建议原始字段也改名，或改为 `not_applicable`。
- Task 5 的 late 模型让 Python 在 sequence 26 未执行时，仍在 tick 27、28 收到观察并继续提交。Fabric 探针不是这样（§2.4）。这不影响 RED，但 D122 的计算器模型应与真实节奏一致。

## 2. 为什么只加窗口不够（E123）

### 2.1 物理事实

E123 只调用 `scripts/d120_momentum_entry_calibration` 的夹具、名义入口和扰动函数，以及项目的 `physics_1_21.step`。入口是 D120 冻结的四方向 972 个落地 post-state，清单哈希 `89c5ee26…`，与 D120 证据一致。结果见结论 3 的表。

结论只针对这 972 个有限样本，不扩大成连续安全包络。

### 2.2 两种修法各缺什么

- **只加窗口：** sequence 26 在 tick 28 被拒绝，不再补跳。但 tick 27 已经是 sequence 25 租约耗尽后的 neutral（`ClientBehaviorInput.java:150-155`，不活跃就 `clear()`）。“不按键”之后无论接什么都掉落。
- **只加默认：** tick 27 是潜行，但迟到的起跳仍在 tick 28 被采样。“潜行，起跳”972/972 掉落。
- **默认改为保持动量（前进+疾跑），换取 `[L+1, L+2]` 的两拍窗口：** 后继按时就能跨过（“前进+疾跑，起跳”972/972），但后继不来时“前进+疾跑，潜行”972/972 掉落。这个方案不安全。

### 2.3 默认要保持多久

| 从 `L+1` 起潜行的拍数 k | 1 | 2 | 4 | 6 | 7 | 8 | 9、10、12、16、20 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 停在中间平台 | 0 | 0 | 0 | 324 | 324 | 972 | 972 |
| 掉落 | 972 | 972 | 972 | 648 | 648 | 0 | 0 |

k=7 时有 648 个入口速度已降到阈值以下，但中心越过平台边缘 0.3 格，下一拍失去支撑。S0 的潜行 fallback 是 8 步停稳，Task 4 的释放见证在 tick 36。两者与这里一致。

因此预排默认至少覆盖 8 个采样拍，再加释放见证的余量。现行 `valid_for_ticks` 上限 20 足够。

它的墙钟预算也要覆盖这些拍。现行 `leaseActive` 同时检查拍数和 `now - leaseStart < leaseBudget`（`ClientRequestGate.java:52-55`）。墙钟预算如果只按一拍给，Python 停顿时默认会在第 2 拍被清成 neutral，正好落在 k<8 的掉落区。

### 2.4 Python 为什么来不及补救

- **计算器模型里：** Python 在 tick 27 的观察后才知道 `L` 帧没生效，改发的潜行最早在 tick 28 采样，对应“不按键，潜行”，972/972 掉落。
- **Fabric 探针里还更晚：** 探针是一问一答。
  - tick 开始时，只有上一个请求的回执已出，才取下一帧（`DeploymentObservationProbe.java:118-137`）；
  - tick 结束时，只有本 tick 执行过请求，才发观察（`:156` 的 `if (failed || !pending) return`）。
  - 所以 `L` 帧迟到时，tick 27 结束不会有观察。Python 下一次看到身体，是 sequence 26 在 tick 28 执行之后。

所以“`L+1` 用什么”只能由 `L` 之前已到客户端的请求决定。在 Task 5 的序列里，这个请求是 sequence 25：它在 tick 26 执行，内容是第一跳的最后一拍前进+疾跑。它必须同时带上“租约用完后保持潜行”。这条潜行就是 S1 在精确出口已经证明过的 fallback，Python 在 `L-1` 已经有它，不需要新计算。

### 2.5 为什么撤销、cancel、`stale_observation` 都不等价

| 机制 | 现行实现 | 为什么不行 |
|---|---|---|
| 后发撤销 | 与被撤销请求同队列、按序处理 | 被撤销的请求先到；Task 5 late 的 neutral 就排在 sequence 26 后面 |
| `cancel_request_sequence_id` | 只在 `leaseOwner == sequence` 时生效（`ClientRequestGate.java:68-73`） | 只能撤当前 owner；过期后继到达时立即成为 owner，并在同一 tick 被采样 |
| `stale_observation` | 比较请求的 observation id 与 `latestObservation`（`ClientRequestGate.java:46`）；后者是交换代数，在发观察时更新（`ClientBehaviorExecutor.java:392`） | tick 27 没有发观察，sequence 26 到达时代数仍匹配；即使改成每 tick 观察，也只是拒绝，`L+1` 仍是 neutral |
| 租约 | 到达时开始（`ClientRequestGate.java:28-31`），按采样拍消耗 | 没有“只能在第几拍首次生效”的概念 |

窗口在客户端采样点比较本拍编号，不依赖任何后续消息，所以不怕有序队列。这是它与撤销的根本区别。

## 3. 建议的 D122：客户端应用前时序

### 3.1 范围

只做两件事，都由客户端在 `Input.tick` 采样点执行：

1. **首效窗口。** 请求可带 `movement_tick_window = {earliest, latest}`，编号与观察的 `movement_tick_id` 同一个计数器（Java 的输入采样计数）。
   - 本拍编号 < `earliest`：不生效，保持原状态；
   - 在窗口内：生效，租约从这一拍开始计；
   - > `latest`：在物理前拒绝，回执给出有类型的 `movement_window_missed` 和本拍编号。
2. **一个预排默认。** 请求可带 `then_hold = {movement, ticks, budget_ns}`。本请求的租约用完后，若没有新请求在窗口内接管，就保持该输入 `ticks` 拍。
   - 采样记录标为 `scheduled_default`，归属本请求的 sequence；
   - `budget_ns` 必须覆盖 `ticks` 拍；
   - 默认结束后才回到现行的 neutral。

不做的事：

- 通用排程器、多个默认、按 tick 列表的输入序列；
- 在 wire 上放 prepared generation（Python 在 `L` 已核对，客户端只需要 tick）；
- 改动不带新字段的请求的任何语义。

字段缺省时，行为与现在逐字节相同。

### 3.2 Python 侧语义

- **默认随承诺前最后一个请求一起发出：** 第一跳最后一个主输入的请求（Task 5 中的 sequence 25）带 `then_hold = 潜行`，`ticks ≥ 8` 再加见证余量。它就是当前证明的精确出口 fallback，归属现有的 incumbent fallback owner，不新增 owner。
- **后继请求带窗口：** `L` 帧的后继请求带 `[L+1, L+1]`。被接纳就覆盖默认；被拒则默认继续。
- **账本：**
  - `scheduled_default` 采样计入 fallback owner；
  - `movement_window_missed` 是应用前拒绝，不能记成 `APPLIED_OUTSIDE_WINDOW`；
  - 未送达也不能冒充应用前拒绝。
- **错拍后继续潜行：** 在释放见证之前，只能继续发同一潜行（或不发覆盖它的请求），不能发 neutral。E123 显示“潜行，不按键”972/972 掉落。Task 5 里 Python 进入停止后发 neutral，这条路径要改。

### 3.3 兼容

现行 Java 对动作 JSON 要求键集合完全一致：`ClientActionRequest.java:36-39` 列出键，`:129-131` 不等就抛异常。异常经 `DeploymentObservationProbe` 的 `catch (RuntimeException)` 进入 `fail()`，整个会话失败，玩家输入随之释放。

所以向旧客户端发新字段，结果不是一次有类型的拒绝，而是会话中断，身体同样变成 neutral。

建议：

- 动作 schema 升为 `mc2p.client_action.v2`，在现有会话握手（`DeploymentSession.java:39-48`，已有 `observation_schema_version`）里同时声明动作 schema。
- 旧客户端在握手阶段就拒绝，这时身体还没开始任何动作。
- 没有该能力时，Python 不进入依赖精确首效的动量链，返回有类型的 `UNSUPPORTED`，由规划选择可停下的路线。
- `CalculatorBackend`（`tests/sim/backend.py:454-566` 的租约实现）同步实现同一语义，并用同一组语义测试对照 Java。

### 3.4 验收

- **Java 单元测试：** 窗口早到、命中、错过，默认保持与到期，墙钟到期，cancel，新旧 schema 握手。
- **计算器组件矩阵：** 四方向 × {timely、late 1 拍、late 2 拍、never、`L` 后 Python 停顿 K 拍}。
  - 指标：`L+1` 实际采样输入、过期首效次数（必须为 0）、终态、伤害、单一 winner、owner 无空档。
  - transport 模型要符合一问一答：没有执行请求的 tick 不产生观察。
- **Fabric：** 在 D120 固定课程上用固定重放输入，不经规划器。
  - 四方向 × {timely、受控延迟 1 拍、never}，每格多次。
  - 延迟只在测试发送端注入：`L` 帧推迟到客户端已开始 tick `L+1` 后再发，参照 0018 的受控扰动做法。
- **回归：** 全量分片和现有 Fabric 门槛全绿。没有新字段的普通请求行为不变。

### 3.5 停止条件

- 需要第二个预排项、通用排程或第二个输入出口；
- 旧客户端不能在握手阶段拒绝；
- 计算器与 Java 语义一轮限定修正后仍不一致；
- Fabric late 场仍出现过期首效，或 `L+1` 仍是 neutral；
- 普通请求行为变化或旧成功退步；
- 无关行尾变化。

### 3.6 不采用的方案

| 方案 | 不采用的原因 |
|---|---|
| 只加窗口 | `L+1` 是 neutral，972/972 掉落 |
| 后发撤销／cancel | 同队列有序，被撤销的请求先到；cancel 只撤当前 owner |
| 每 tick 观察加 `stale_observation` | 只能拒绝，`L+1` 仍是 neutral |
| `L-1` 就决定后继 | 只多 1 拍余量，仍需要窗口和默认，还要用预测入口代替实测入口 |
| 默认保持动量、窗口放宽到 `[L+1, L+2]` | 后继不来时 972/972 掉落 |
| 默认沿用最后一拍输入（hold） | 同上；最后一拍是前进+疾跑 |

### 3.7 先决条件

D122 开工前，先把 D121 收口：

- 修正 §4 的代码回归；
- 恢复被改写的历史字节；
- 入口文档状态改为 PARTIAL／STOPPED；
- S2 复现命令改为指向 3889d10。

这些不属于 D122 的范围，也不应混进 D122 的提交。

## 4. 这次发布的问题

### 4.1 全量分片

项目自己的分片器跑完整 `tests/motion_nav`，201 个模块：193 个通过，8 个报错，2631 项测试中 1 个 failure、18 个 error（`output/linux-forward-summary.txt`）。

与 3889d10 的分片相比：

- **已修好：** 上一轮报告的 `test_navigation_design_metrics` 两项失败已修好。
- **新增模块：** D121 的三个新模块（handoff execution、successor binding、transport late）通过。
- **D101：** `test_d101_measurement` 只能在 Windows 上跑，两轮相同。
- **新报错：** 新出现 7 个报错模块，都在 3889d10 上通过。

E125 把这 7 个模块的原因分开。它在 dfaa0b4f 上建一棵对照树：把 1614 个只有行尾变化的文件全部换回 3889d10 的原字节，忽略行尾后与 dfaa0b4f 没有任何差异，再在两棵树上重跑。

| 模块 | dfaa0b4f 原样 | 只换回行尾 | 原因 |
|---|---|---|---|
| `test_action_entry_handoff_repair` | 6 error | OK | 行尾 |
| `test_f2rec_r0_baseline` | 1 failure、1 error | OK | 行尾 |
| `test_f2rec_r1_region_geometry` | 5 error | OK | 行尾 |
| `test_reference_baselines` | 1 error | OK | 行尾 |
| `test_action_continuity_solver_limits` | 1 error | 1 error | 代码 |
| `test_d058_runtime_validation` | 1 error | 1 error | 代码 |
| `test_navigation_session` | 1 error | 1 error | 代码（与上一行是同一个用例） |

所以 18 个 error 中，13 个和那 1 个 failure 由行尾改写造成，3 个是两处代码回归，2 个是 Windows 专用的 D101。

验收文档自己写明“完整 Windows 和独立公开快照矩阵尚未重跑”。这些问题在 Windows 上同样会出现：前四项是字节哈希，后两项是纯 Python 逻辑。

### 4.2 回归明细

行尾造成的四项，各自卡在一个被改写的冻结文件上：

| 冻结文件 | 校验位置 | 受影响模块 |
|---|---|---|
| `tests/sim/manifests/navigation-product-r28-v8.json`（记录 `c29e9158…`，现为 `418a330e…`） | `tests/sim/f2s_cases.py:159` | `test_action_entry_handoff_repair`、`test_f2rec_r1_region_geometry`、`test_f2rec_r0_baseline` 的 v9 输入用例 |
| `evidence/motion_navigation/F2REC-recovery-v1/r0/tool-provenance.json`（测试固定为 `57da7698…`，现为 `cdccd4f3…`） | `test_f2rec_r0_baseline.py:139-142` | `test_f2rec_r0_baseline` |
| `experiments/navigation-floating-wall-mazes-v1/layouts/maze_03_floating.json`（B01 场景 S04，记录 `99c144d6…`，现为 `39145f39…`） | `mc2p/motion_nav/evidence/reference_baselines.py:177-178` | `test_reference_baselines`；AGENTS.md 里的 `motion_navigation_references.py verify` 读同一份场景注册表 |

B01 参照场景、F2REC 的工具来源记录和 R28 v8 清单都是早已冻结的历史基线。恢复原字节即可，不应改动记录的哈希。

代码造成的两处：

| 模块 | 原因 | 建议 |
|---|---|---|
| `test_action_continuity_solver_limits` | 新的 `VerifiedMotionResult` 构造校验（`motion_solver.py:748-756`）让测试里的 `replace(proof, recovery_horizon_ticks=1)` 直接抛 `ContractViolation`，测试要检查的重验证拒绝路径走不到 | 行为更严格，属于测试没随契约更新；改测试，保留“求解和重验证都拒绝”的意图 |
| `test_d058_runtime_validation`／`test_navigation_session` 的 `test_cancel_keeps_executor_until_its_landing_responsibility_finishes` | 新的 `_release_completed_prepared_uses`（`execution_supervisor.py:924` 起）在 cancel 路径读 `control.activation_registry`；它读执行器私有字段 `_prepared_result_inbox`（`route_body_controller.py:193`），测试替身没有这个字段 | 执行器提供只读公开访问，不跨模块读私有字段；执行器自己在 `action_route_executor.py:591` 也用 `getattr` 兜底，说明这里本该是接口 |

### 4.3 行尾统一

dfaa0b4f 改了 1753 个文件，其中 1614 个只有行尾变化：

| 目录 | 只有行尾变化 | 有内容变化 |
|---|---:|---:|
| `evidence/` | 787 | 98（全部为新文件） |
| `tests/` | 261 | 12 |
| `docs/` | 254 | 4 |
| `mc2p/` | 145 | 11 |
| `scripts/` | 102 | 7 |
| `experiments/`、`deployment/` | 各 15 | 0 |

影响（E124、E124b）：

- **冻结文件：** 三个冻结文件的哈希变了，4 个测试模块因此报错（§4.1、§4.2）。
- **证据引用证据：** 共 48 条，统一前 41 条对得上，统一后 33 条。新断的 11 条全部记录在 `F2REC-recovery-v1/r0/reused-baselines.json`，指向 `F2R-piecewise-completion-v1` 和 `r28-baseline-v7` 的 index、summary 等文件；换回 CRLF 即可对上。另有 3 条（D106 引用 D105）反而由此对上。
- **证据记录的源文件哈希：** 共有记录 24,852 条。统一前对上、统一后对不上的有 1,721 条，其中 1,709 条换回 CRLF 即可对上；反方向由此对上的有 10,042 条。源文件哈希随代码修改而失配属于正常；这里只说明行尾统一不是“无副作用”。

这与 D121 §6 的停止条件“无关行尾变化”和 AGENTS.md“不能为了符合说明而删除失败证据”的精神都冲突。

公开仓库的 `.gitattributes` 两个提交完全相同（`* -text`），所以这次改写来自生成快照的内容本身。我无法区分它发生在主工作区还是导出步骤。

建议：

- 历史证据和冻结清单恢复为 3889d10 的原字节；
- 源码若要统一 LF，单独写决定，并在同一提交里处理所有依赖原字节的清单。

### 4.4 文档状态不一致

- **仍写 `AUTHORIZED REVISION／IN PROGRESS`、“不新开 D122”：** AGENTS.md 第 3 行，README 第 5 行。
- **已改为 PARTIAL／STOPPED：** 决定、计划和验收，并写明“修改 action contract／wire／Java 的后续工作须另行决定”。

入口文件是“先读什么”的依据，应先改一致。

### 4.5 S2 复现命令失效

dfaa0b4f 删除了 `scripts/d121_joint_worker_stop_probe.py`。测试改为只核对 `s2/stop.json` 的原字节，这是合理的。但验收文档第 128 行仍要求运行这个脚本，证据里的 `source_sha256` 也指向它。

建议把复现命令改为“在 3889d10 检出上运行”。

## 5. 证据边界

- 全部数字来自 Linux。E123 是纯计算器物理，与平台无关；哈希和测试错误与平台无关。
- 没有运行 Java 或 Fabric。§2.4、§2.5 和 §3.3 关于客户端的结论来自源码阅读。
- E123 只覆盖 D120 冻结的 972 个入口，不是连续安全包络。
- 我没有实现 D122 的原型。§3 是建议的范围和门槛，不是已验证的设计。

## 附：实验清单

| 编号 | 脚本或命令 | 输出 | 内容 |
|---|---|---|---|
| E123 | `e123_late_tick_physics.py` | `output/e123_late_tick_physics.json` | `L+1`／`L+2` 九种输入组合，以及潜行 k 拍后松键，各 972 个入口 |
| E124 | `e124_evidence_hash_references.py` | `output/e124_evidence_hash_references.txt` | 证据中“路径 → sha256”记录，在两个提交上各有多少对得上 |
| E124b | `e124b_evidence_reference_diff.py` | `output/e124b_evidence_reference_diff.json` | 同一记录在行尾统一前后的变化，以及证据引用证据的逐条状态 |
| E125 | `run_all.sh` 内联 | `output/e125_line_ending_attribution.txt` | 7 个新报错模块：dfaa0b4f 原样，与只把 1614 个文件的行尾换回 3889d10 的对照树 |
| E126 | `run_all.sh` 内联 | `output/e126_suite_regressions.txt` | 两处代码回归在两个提交上的错误信息 |
| 全量分片 | `scripts/run_motion_nav_shards.py` | `output/linux-forward-summary.txt` | dfaa0b4f 完整 `tests/motion_nav` |

复现：`PROJECT=<dfaa0b4f 检出> BASE=<3889d10 检出> sh run_all.sh`。
