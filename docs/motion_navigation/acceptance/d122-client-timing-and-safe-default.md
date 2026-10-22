# D122 验收：客户端首效窗口与预排安全默认输入

日期：2026-10-20。当前范围：**D122-0—D122-3 已完成；D122-4 停在 Fabric trial 前的观察装配门禁**。正式 v2 严格动作已经接入精确首效窗口和 proof-selected 安全默认；普通 v1／CraftGround wire 保持兼容。第二轮独立代码审查以及最终 Windows 源码树、standalone、Java 和 overlay 门禁均已通过。D121 历史仍保持 `PARTIAL／STOPPED`。

依据：[D122 决定](../decisions/0122-enforce-first-effect-window-and-prearm-safe-default.md)、[实施计划](../stages/d122-client-timing-and-safe-default-plan.md)。D122-0—D122-3 的分步证据位于 `evidence/motion_navigation/d122-client-timing-v1/`。旧 D121 证据没有改写。

## 1. 正式循环是否支持下一步

结论：源码中的正式部署循环支持“保持响应未就绪，同时继续客户端 movement”。本步没有启动 Minecraft，不把这项源码核实说成 Fabric 通过。

| 核实点 | 现行调用路径 | 结论 |
|---|---|---|
| pending 时能否继续推进 | `DeploymentObservationProbe.startTick()` 先执行 `executor.tick()`；`pending && !receiptReady()` 时仍调用 `executor.advance()` | 没有阻塞客户端主线程，也不在这里等待 Python |
| 响应能否暂不发出 | `endTick()` 在 `!receiptReady()` 时返回，不调用 `transport.offer()` | 只推迟响应；不会停住 vanilla 的 movement |
| 后继未送到时能否继续 | `transport.poll()` 为空后仍执行 `executor.advance()`；player 的 `tickMovement` 由独立 mixin 回调 | 不需要 Python 发空请求来推进物理 |
| 什么决定现行响应就绪 | `ClientBehaviorExecutor.receiptReady()` 当前只看 `pendingAttack == null` | 新窗口／默认的等待条件仍需 D122-2 实现 |
| 输入准备的位置 | `BehaviorPlayerMixin` 在 `tickMovement HEAD` 调用 `ClientBehaviorHooks.beforePlayerTick()` | 早于原版 `Input.tick`，可用于选择同拍输入和提前投影 sneak |
| 输入采样后 | mixin 在原版 `Input.tick(ZF)` 的 `AFTER` 调用 `afterInputSample()` | 现有回调只处理 sprint；不能替代采样前的选择 |

`preflight-current.json` 保存每个核实片段的文件、行号和 SHA-256。这里核实的是调用顺序与可用入口，不证明尚未实现的 pending/default 状态能工作。未来的 missed 响应必须沿此就绪入口有界持有，不能增加组件专用同步观察。

## 2. Java 动态核实

用现行 `ClientRequestGate`、`ClientBehaviorInput` 和已有的测试用 Minecraft `Input` 表面编译执行：

- `ClientRequestGateTest`：通过。
- `ClientBehaviorInputTest`：通过。
- `D122InputPhaseProbe`：通过。连续两次 prepare 不增加 sample，也不消耗一拍 lease；真正的 `Input.tick` 才消费一次。
- 现行严格 v1 decoder 三例：普通请求接受，未经协商直接加入窗口字段和重复身份键均拒绝。这里只确认旧边界，v2 decoder 尚未实现。

新 probe 同时记录一项需要在 D122-2 修正的现行事实：前一拍潜行、下一请求不潜行时，`prepareForPlayerTick()` 仍保留旧 `sneaking=true`，直到 `Input.tick()` 才清除。当前提前投影只会置 true。D122 同拍从默认切后继时，必须在 HEAD 同步覆盖 true 和 false。

这三项检查运行真实 gate/input 代码，没有用 Python 代写 Java。它们使用测试用 `Input` 表面，不证明 Minecraft 原版物理或 Fabric 构建兼容；本步没有 Java 生产改动。相位 probe 只记录旧 sneak，不把“必须保留旧 sneak”写成未来的断言。

## 3. 当前正式 Task 5 的 RED

运行 `scripts/d122_timing_preflight.py`，内部直接调用现行 `scripts/d121_transport_late_probe.py` 的 `run_matrix()`。Runtime、导航驱动、执行器、prepared 求解、账本、回执和物理路径均沿用 Task 5，没有修改生产函数。

| 场景 | 目标窗口 | 真实首效 | 现行结果 |
|---|---|---|---|
| timely | `[27,27]` | 27 | 完成，安全着地 |
| late | `[27,27]` | 28 | 过期跳键先进入物理，随后账本才报告窗口外应用 |
| never | `[27,27]` | 无 | L+1 没有预排潜行；最后身体已经离开平台，仍在空中 |

late 最终位置约为 `(0.5,61.216,4.932)`，never 约为 `(0.5,61.305,2.866)`，两者 `on_ground=false`。采样截断时伤害字段虽为 0，也不能据此说安全；它们均未解决身体责任。RED 的依据是真实应用 tick 和身体状态，不是“新类型还不存在”。

新增证据只写 D122 目录。D121 Task 5 原始失败和发布修复结果保持原字节。

## 4. 父请求前提

冻结轨迹重新读取结果：

| tick | 输入 |
|---|---|
| 1—14 | 前进＋疾跑（FS） |
| 15 | 前进＋跳跃＋疾跑（FJS） |
| 16—26 | 前进＋疾跑（FS） |

命令共 26 条；release-safe 边界只有 `0—14`。第一跳最后请求 sequence 25 在 movement tick 26=`L` 应用。它已晚于最后可放弃点，因此 D122 不声称“承诺前就确认 armed”。

D122 只覆盖：最后父请求在采样 L 前已安装默认，正常 lease 覆盖到 L，L 响应确认 `fallback_armed` 后，后继按时、过期、被拒或失联的边界。父请求自身迟到或丢失沿用现有 input-loss 责任。

## 5. 测试侧合同

`tests/sim/d122_timing_oracle.py` 是有限转换表，没有物理、网络或客户端输入出口。它只给后续 Python／Java 实现同一把比较尺子。**oracle 通过不等于生产 v2 通过。**

已冻结 24 个向量：`L=26,duration=8` 和 `L=107,duration=11` 各 12 类事件。覆盖早到、按时、晚到、never、取消、reset、episode 切换、高 sequence 替换、普通更新、许可撤销、pending wall 过期和默认耗尽。另有默认墙钟从激活起算的单独检查。

关键要求：

- 接纳时核对并消费一次 sequence／observation。激活仅复核登记身份、episode/reset、许可、wall 和首效窗口。排队期间观察推进不能把请求变成 stale。
- early 不改变当前输入；按时激活才能替换默认；late 不补跳，保留默认。
- 默认每拍使用父请求身份；用完后中性，旧父输入不得复活。
- 普通更新、cancel、reset、episode 和强制许可撤销清旧安排。此类清空不能自动算安全收尾。
- 六类状态固定为 `fallback_armed`、`window_pending`、`window_applied`、`window_missed`、`default_active`、`default_exhausted`。
- wire 只加 `first_effect_window` 和 `safe_default` 两项；v2 必须握手确认，v1 保留旧的精确键集合。

完整字段、界限和逐拍向量在 `preflight-current.json` 的 `contract` 中。默认耗尽在下一个采样前检查；连续执行完 duration 拍后，最迟下一采样边界报告耗尽。它有明确上界，不无限持有响应。oracle 单元检查保持纯 Python；Java 编译运行由独立只读 probe 负责，避免给正式行为集合增加平台依赖。

## 6. 计时边界

本步不签署新的性能数字。8 拍默认在名义 20 Hz 下约占 400 ms，11 拍约占 550 ms；这是必须单列的合法等待，不是 Python 同步计算超时。

后续分别报告：Python 同步提交计算、生产控制准备计算、客户端响应持有的 tick／墙钟时长，以及端到端响应时长。同步门槛仍为提交 `<50 ms`、控制准备 P95 ≤8 ms／P99 ≤15 ms／最大 `<30 ms`。端到端等待仍受默认时长、wall deadline 和外层期限约束，不能从报告中删掉。

## 7. 检查与边界

命令：

```powershell
.venv/python.exe scripts/d122_timing_preflight.py --output evidence/motion_navigation/d122-client-timing-v1/d122-0/preflight-current.json
.venv/python.exe -m unittest tests.motion_nav.test_d122_timing_contract tests.motion_nav.test_d121_transport_late tests.motion_nav.test_d121_handoff_execution -v
```

相邻三模块 40／40 通过；oracle 最后一次修改后单独复查 4／4 通过。Java 三个 gate/input 类及 v1 decoder 三例均通过。实际结果见同目录检查日志。未运行完整 Windows、公开快照、Fabric、v2 decoder 或生产 v2 三态。D122-1 必须等待本步合同复核；本步没有跨入下一实现阶段。

## 8. D122-1 Python 切片（2026-10-20）

实现了 typed safe default、精确首效窗口投影、显式 v2 协商和 v4 时序事实。Fabric 仍默认 v1；只有显式请求 v2 且首个合法 v4 sample 成功，才记录会话确认。v1 普通行为的 wire 字节保持不变，CraftGround 未被声明支持 v2。

CalculatorBackend 先按当前／pending／default 转换表选输入，再推进物理。future 持有响应到首效；missed 保持默认到有界结束。never 用 free tick 推进，不增加观察 sequence。父默认的实际输入仍归父请求，账本不会在父主输入已应用后提前丢掉默认责任。没有修改 Session、planner、物理规则、支撑门槛或 D121 producer。

| 检查 | Windows 结果 |
|---|---|
| 新 Python 行为检查 | 23／23 |
| 与 action、回执、Runtime、Fabric Python backend、transport、在线账本、D122 oracle 和设计量尺合并 | 204／204，零失败／错误／跳过 |
| 六种 mutation | 6／6 检出，均为断言失败，没有用导入错误代替 |
| v1 calculator 四族配对 | 120／120 请求的状态、观察、回执、实际输入逐项一致 |
| 原仲裁器 RED | 同一输入合同下，精确窗口仍为 None，断言失败 |

证据：`evidence/motion_navigation/d122-client-timing-v1/d122-1/`。该目录 README 给出字段、命令和边界。组件单列名义 waited tick／ns；本步没有新的同步控制或 Fabric 性能签署。没有运行 Java 生产 v2、D121 正式三态、完整 Windows 或独立公开快照；不能由本结果宣布 D122 完成。

## 9. D122-1 独立审查后的限定修正

保留 `423b5bd1` 和上一节的原证据。本次另写 `d122-1-repair/`，不进入 Java 或 D121 producer 接入。

- 默认 wall budget 至少覆盖 `duration × 50 ms`。成功 arm 后，wall 或父响应期限先到不能截短证明要求的真实采样；响应等待仍有界。
- 默认耗尽只有在合法 tick 且已收到完整默认采样区间时才结束责任。提前耗尽、缺失采样、单独 inactive sample 都不能结束父默认。
- 普通替换必须有真实输入采样；accepted cancel 由已提交取消请求和同身份 v4 执行回执确认，清掉旧默认、pending 和取消自身的记录。内部结果为 `CANCELLED`，不冒充拒绝，也不增加 wire status。

Windows 相邻 214／214 通过；新增的未来事件边界检查随后通过，最终修正模块 11／11 通过，合计覆盖 215 个不同检查。14／14 mutation 被断言检出，错误数为 0；v1 四族 120／120 请求继续逐项一致。原 RED 为 7 项中 5 项断言失败；取消分类 RED 为 10 项中 2 项断言失败。

正式 Fabric host／probe 的 v2 构造点迁移明确留在 D122-3。本切片只提供合同和组件支持，不启用正式严格动作。Java、Fabric、完整 Windows 与独立公开快照仍未运行。

额外运行导出单元模块 28 项，26 通过、2 项失败：D121 current-preparation 的 Runtime 字节指纹和 F2 current semantic 的在线账本指纹尚未迁移。它们不表示历史证据失效；本次保留旧记录，当前指纹迁移与正式构造点一起留 D122-3。此中间切片不能作为全绿公开发布，结果见 `export-checks.json`。

## 10. D122-1 响应和安排成员的第二次限定修正

保留 `6696e57f` 和上节证据。纠正上次修订中“wall 到期可以先结束响应等待”的说法：missed／未激活的 future 必须持有响应到默认真实 duration 完成。否则 Python 下一条普通请求仍会清掉未完成默认。无 movement tick 时物理不推进；外层硬终止须走 reset／许可撤销，不能作为普通控制继续。

取消现在只结束由 v4 事件和真实采样确认的 current／pending／default 成员，旧的无关 ambiguous 历史逐字段保留。更高 pending 确认替换时，旧 pending 以内部 `CANCELLED` 结束；新 pending 后续 miss 不影响这个结论。默认耗尽同拍的中性采样不会成为新的父输入或重新登记默认。

证据在 `d122-1-repair2/`。两组 RED 各捕获 2 项断言失败，错误为 0。最终 Windows 相邻 219／219 通过，19／19 mutation 均为断言检出，v1 120／120 请求逐项一致。未改 Java、Session、规划、物理或历史证据。前节两项当前导出指纹迁移仍留 D122-3，本切片不能作为全绿发布。

## 11. D122-2 Java 采样门禁

Java 客户端现在严格区分 action v1／v2。部署 session v3 协商 action v2；session v2 继续使用 action v1。v1 的旧键集合保持不变。v2 解码器拒绝超界 tick、反向窗口、递归默认和不足以覆盖“等到激活点＋完整默认”的父 wall budget。

`ClientTimingSchedule` 是 pending 和 default 的唯一生命周期 owner。Executor 不保存第二份 pending。窗口请求激活成功后才清旧默认；激活失败继续执行旧默认。后继自身携带的默认也只在后继成功激活后安装。普通替换、合法 cancel、reset 和许可撤销走同一个清理入口。

本阶段只支持 movement＋look 的窗口请求。`operation` 非空时，以 `windowed_operation_unsupported` 在排队前拒绝。原因是一拍 operation 仍依赖旧的 `beforeSample` 顺序；接受后延迟执行会使用错误朝向或错过采样边界。普通非窗口 operation 不受影响。

真实采样规则如下：

- `prepareForPlayerTick` 在原版读取 sneak 前选择 current／pending／default，并覆盖 sneak 的 true 和 false；
- `Input.tick` 每拍只消费一次，late jump 不进入物理；
- default 在绝对 tick 激活，完整执行 duration；wall 或父期限不能提前截断；
- `default_exhausted` 在完整 duration 后的下一采样边界报告；future／missed 响应在此之前保持未就绪；
- 同拍 successor 优先于 default；successor 激活拒绝时，同拍回到 default。

| 检查 | Windows 结果 |
|---|---|
| Java／Python 相邻动态检查 | 89／89 |
| Fabric overlay `compileJava --rerun-tasks` | 通过 |
| Java 变异 | 11／11 检出 |
| action／回执上限 | 16 KiB／64 项，越界拒绝 |

证据位于 `evidence/motion_navigation/d122-client-timing-v1/d122-2/`。`red-before-java.txt` 保留实现前的 decoder、session 和 schedule 编译失败。没有运行完整 Windows 分片、独立公开快照、D122-3 正式三态、性能签署或 Fabric；本切片不能宣布端到端能力完成。

## 12. D122-2 独立审查后的限定修正

保留 `090c6eb9` 和 `d122-2/` 原证据。本次修正提交为 `7ded5743`，新证据写入 `d122-2-repair/`，没有进入 D122-3。

修正三项合同：

- Java 现在执行与 Python 相同的精确默认关系。普通父请求要求 `activation = next movement tick + lease ticks`；携带默认的窗口请求必须是单 tick 窗口，并在真实首效拍再次检查 `activation = first effect tick + lease ticks`。不满足时有类型拒绝并保留旧默认。
- 物理许可撤销时，pending 收到 `window_missed`，顶层回执为 `rejected／permission_revoked`。已激活父请求留下父身份的 `disallowed` 真实采样。Java 动态结果经过 v4 decoder 送入 Python `InputApplicationLedger` 后，父请求和 pending 都有界结束为 `REJECTED`。
- 极大 `activation_tick` 的预算计算改为饱和结果，随后走有类型拒绝，不再抛出 `ArithmeticException` 终止会话。

Python 和 Java 共读 `tests/fixtures/d122_default_relation_vectors.tsv`。八个向量覆盖普通／窗口请求、1／3 tick lease、非精确窗口、错位激活和 `Long.MAX_VALUE`。实现前四项检查全部暴露真实缺口；修正后相邻动态检查 91／91、Java mutation 15／15、Fabric overlay `compileJava --rerun-tasks` 通过。

没有运行完整 Windows 分片、公开快照、D122-3 三态、性能签署或 Fabric。原 D122-2 证据不回写。

## 13. D122-2 接纳顺序的第二次限定修正

保留 `7ded5743`、`35b784c6` 和前两份证据。本次生产修正提交为 `91034d29`，仍未进入 D122-3。

时序请求现在统一经过 `ClientTimingAdmission`：先由 `ClientRequestGate.admitPending()` 消费 episode、sequence 和 observation，再检查默认精确关系或窗口 operation。语义拒绝不会安装 lease 或 pending，但同一 sequence 已经不可重用。

新增动态 RED 分别覆盖普通父请求的 `safe_default_activation_mismatch` 和窗口请求的 `windowed_operation_unsupported`。两条请求被拒后，使用同一 sequence 提交普通修正请求都必须得到 `duplicate_request`，同时 gate 没有活动 lease。修正后相邻动态检查 92／92、Java mutation 16／16、Fabric overlay `compileJava --rerun-tasks` 通过。

证据位于 `d122-2-repair2/`。没有运行完整 Windows 分片、公开快照、D122-3、性能签署或 Fabric。

## 14. D122-2 拒绝清理的第三次限定修正

保留前述提交和证据。本次生产修正提交为 `96e8f2cc`，仍未进入 D122-3。

通用拒绝不再根据 `lastRequest.safeDefault != null` 清空整份时序安排。`ClientTimingSchedule.rejectInstalled(sequence)` 只移除该 sequence 实际拥有的 pending、applied pending 或 fallback。尚未经过 `stageWindowed` 的后继没有时序成员，因此额度不足、screen conflict、no world 或 player dead 等拒绝不会删除父 fallback。

组合 RED 先安装父 fallback，再接纳一个 exact-window 且携带自己默认的后继，并让它在 stage 前因额度不足被拒绝。最终断言：拒绝原因有类型、同 sequence 已是 duplicate、没有后继 lease／pending，父 fallback 在目标拍仍以父身份进入 `DEFAULT`。现有激活失败保留 fallback 的门禁继续通过。

最终相邻动态检查 93／93、Java mutation 17／17、Fabric overlay `compileJava --rerun-tasks` 通过。证据位于 `d122-2-repair3/`；未运行完整 Windows、公开快照、D122-3、性能签署或 Fabric。

## 15. D122-2 被拒响应等待的第四次限定修正

保留前述提交和证据。本次生产修正提交为 `3eb13576`，仍未进入 D122-3。

窗口后继在 stage 前被拒绝且父 fallback 尚未完成时，`ClientTimingSchedule` 现在持有“拒绝响应等待 fallback”的同一份 `holdResponse` 状态。响应不会立即返回；父默认完成全部真实采样，并在下一采样边界报告 `default_exhausted` 后才就绪。没有 fallback 时仍立即就绪。

生命周期分支使用 typed `RejectionDisposition`：普通 stage 前拒绝为 `HOLD_INCUMBENT`；screen conflict、no world、player dead 和 permission revoke 为 `HARD_RELEASE`；非窗口请求为 `NONE`。代码不再通过 reason 字符串决定是否等待。

完整组合检查覆盖额度不足后立即 `receiptReady=false`、父默认 8 个真实 sample、tick 10 exhaustion 后 ready，以及同一 v4 receipt 进入 Python ledger 后 successor 为 `REJECTED`、parent 有 8 个默认 sample 且责任结束。语义拒绝、duplicate 和 stale 共用 typed hold 分支；硬许可释放由原有 permission bridge 的 `disallowed` 父事实有界结束。

最终相邻动态检查 94／94、Java mutation 19／19、Fabric overlay `compileJava --rerun-tasks` 通过。证据位于 `d122-2-repair4/`；未运行完整 Windows、公开快照、D122-3、性能签署或 Fabric。

## 16. D122-2 拒绝事务隔离的第五次限定修正

保留前述提交和证据。本次生产修正提交为 `eaf6db3a`，仍未进入 D122-3。

拒绝路径不再只凭 sequence 调用 `rejectInstalled`。Executor 增加单次同步调用的事务标记：每次 `execute` 开始为 false，只有本次调用成功安装 pending 或 fallback 后才变为 true；`finally` 和 reset 都会清零。它不持有时序状态，`ClientTimingSchedule` 仍是唯一生命周期 owner。异步激活失败继续由 schedule 自己收尾。

动态 RED 覆盖两个相邻冲突：重复 sequence 不能删除已存在的 pending；与父 fallback 相同 sequence 的重复请求也不能删除 fallback，拒绝响应继续等待 8 个真实默认采样和 exhaustion。stale 与 episode 拒绝沿用同一原则。mutation 故意忽略事务标记并按 sequence 删除旧成员，检查会失败。

最终相邻动态检查 94／94、Java mutation 20／20、Fabric overlay `compileJava --rerun-tasks` 通过。证据位于 `d122-2-repair5/`；未运行完整 Windows、公开快照、D122-3、性能签署或 Fabric。

## 17. D122-3 正式组件结果

D122-3 只启用已经协商 `mc2p.client_action.v2` 的严格动作链。Runtime reset 后把协商能力固化在新建的输入账本；普通 v1／CraftGround wire 仍不带首效窗口或安全默认，既有内部执行窗口继续用于本地账本和仲裁。

正式 producer 位于 `NavigationSession._proposal()`。Session 只转发执行器选出的值，不判断哪种动作安全。默认来自已接纳 `VerifiedMotionExecutor` 的出口 fallback proof：terminal mode 必须是 `SNEAK_EDGE_STABLE`，出口边界和恢复边界必须等于当前主轨迹终点，输入必须是同一个纯潜行 movement，并带完整 release witness。本次 proof 选择 9 tick；代码没有把 8 写成通用规则。携带 default 的 owner deadline 还要留出传输和计算余量，因此 D122 probe 显式使用 1 s；普通驱动窗口没有全局放宽。

初始四方向 × 三态矩阵曾记录 12／12。独立审查确认其中 never 4 场没有关闭 driver 生命周期：source 没释放、prepared frame 没清、driver 停在 stopping。旧 `formal-matrix.json` 现作为生命周期误判的失败证据保留，不回写。

修正后的正式 Runtime 矩阵覆盖四方向 × 五个可达状态，共 20／20：

- timely：4／4 在 `L+1` 真实采样一次后继跳跃并完成；
- late：4／4 在 `L+2` 进入物理前得到 `window_missed`，后继跳跃采样为 0，父 default 完整执行 9 tick 后停住；
- never：4／4 在请求通过 Runtime 唯一 backend 出口后丢失，客户端没有新的 Python 同步观察，自主执行 9 tick default；typed transport rejection 返回后，driver 完成收尾并释放 source；
- 明确拒绝：4／4 在客户端接纳边界消费身份但不安装后继，父 default 完整执行并有界失败；
- 取消：4／4 走 driver 的正式取消与身体责任收尾，安全停住并释放 source。

正式 producer 只在 `L=26` 的落地观察后产生后继，不存在 `L` 之前的 future-pending formal 场景。early 继续由同一 ClientTimingSampler、v4 receipt 和正式 ledger 转换表覆盖，不伪造 proof 或提前生成后继。20 场全部无异常、source 已释放、prepared frame 已清空、driver 有界终结、零伤害。同步性能共 708 个样本：提交最大 11.3745 ms；生产控制 P95／P99／最大为 0.6061／3.9415／4.4735 ms。default 导致的合法响应等待单列，没有混入同步门槛。

扩展转换矩阵复用同一 ClientTimingSampler、v4 receipt 和 InputApplicationLedger，覆盖 future pending、higher-sequence pending 替换、reset、已接纳 cancel、未接纳 reject、替换后新 pending miss，以及 pending／armed 不算应用，6／6 通过。Java 的 reject／cancel／reset 动态门禁沿用 D122-2 已签署的相邻检查，本阶段没有另建生命周期 owner。

D120 的 972 个有限入口以物理尾迹重放验证：972／972 都在第 8 个潜行物理样本达到 D120 的停稳条件。这是“D120 物理尾迹重放＋D122 代表客户端采样”的组合证据，不是 972 条完整 Runtime，也不构成连续入口许可。正式链采用已接纳 proof 中更保守的 9 个潜行输入，并在下一边界用独立中性 release witness 结束责任；代码没有从 D120 数字硬编码时长。三项 mutation 全部检出：允许 `L+2`、把默认改为中性、把 pending／armed 当作应用。

第一次完整正序分片按 fail-fast 停在旧输入失联恢复检查。逐字段核对确认，第 69 项／movement tick 71 只有账本 `input_window` 变化：旧值为 requested 71、latest 72、last 71，新值一度为 requested／latest／last 71；位置、速度、着地、实际输入和动作种类保持逐项相同。该结果说明兼容门设得过早。最终实现保留 ActionIntent 和 Runtime ledger 的既有内部窗口，只在 `_backend_step` 前去掉 v1 wire 新字段；冻结前缀因此恢复逐项一致，D122 专项另证 backend 实收 v1 action 没有窗口或 default。

第二次完整正序分片在 62 个模块后保留为失败记录。它暴露两处同源边界错误：Session 从具体 executor 读取 capability，使取消测试替身触发未初始化 prepared 私有字段；同一过早 gate 删除普通已证明静止步行的一拍内部 slack。最终实现改为只从本帧正式 `self._control_ledger` 读取 timing capability，并沿用上述内部窗口／wire 分界。两个失败模块和 D122 定向检查共 134／134 通过。两份失败 JSON 和旧证据都不回写。

审查修复前的 Windows 源码树正序、逆序均为 204／204 个模块、2675 项检查通过；审查修复改变了 driver 和组件代码，因此这些结果只保留为中间记录，不能关闭最终门禁。修正后的 D122 定向集合为 64／64 通过。第二轮独立代码审查随后通过。

当前最终字节的完整门禁结果如下：

| 门禁 | 结果 |
|---|---|
| Windows 源码树正序 | 204／204 模块，2680 项通过 |
| Windows 源码树逆序 | 204／204 模块，2680 项通过 |
| Java／Python 相邻动态门禁 | 94／94 通过 |
| Fabric overlay `compileJava --rerun-tasks` | 通过 |
| standalone 文件与哈希校验 | 最终索引 2969 个文件，通过 |
| standalone 正序 | 203／203 模块，2656 项通过 |
| standalone 逆序 | 203／203 模块，2656 项通过 |

四份完整分片报告的失败、错误、超时、跳过、预期失败、意外成功和清理失败均为 0。首次 Java 发现命令因当前 PowerShell 没有注入项目 JDK 而在编译前停止，保留为 `INVALID_ENVIRONMENT`；首次 overlay 命令又因从子目录解析了错误的相对 JDK／Gradle 路径而在 Gradle 前停止，保留为 `INVALID_ENVIRONMENT_COMMAND`。两次都没有进入代码检查。使用项目已知 `.venv/Library/lib/jvm` 和固定 Gradle 8.8 后，上表 Java 门禁与 overlay 编译通过。

证据在 `evidence/motion_navigation/d122-client-timing-v1/d122-3/`。D121 Task 5 动态测试已经改为读取冻结的历史 RED；旧 late／never 失败没有被现行 v2 行为重写。Fabric D122-4 未运行。

## 18. D122-4 Fabric 前置与装配预检

D122-4 先让组件和 Fabric 共用 trial 组装、复位、起点硬检查、汇总和断言。组件 timely／L+2／never 为 3／3 通过。部署环境另加了显式验收缓存入口；默认仍使用 `ROOT/.gradle`，Junction 会被拒绝。显式缓存必须位于仓库内、同盘、存在且自身和祖先都不是 reparse point。独立 build＋artifact inspect 验证了 109 个真实 classpath JAR；build、launch、sandbox 定向检查 25／25 通过。

前四次启动均作为历史失败保留：v1 缺项目 JDK，v2 缺 Windows native，v3 的旧构建入口仍使用默认缓存，v4 的默认 `.gradle` 是 Junction。v5 进一步发现 server fixture 仍硬编码默认缓存；同一 selector 贯穿 server JAR 后，相关检查通过。v6 进入真实 server／client 后发现两处惰性导入错误；随后增加不启动 Minecraft 的完整依赖解析门禁，并同时修正 `NavigationObservationAdapter` 和 `PreparedMotionJob` 的正式模块归属。v7 前置 92／92 通过。

v7 已建立真实 server、client 和 fixture，但在第一场 proof replay 前以 `NEEDS_WORLD` 停止。缺失格为 `(25,2,18)` 和 `(25,2,19)`。只读归因确认：

- 两格已经由 fixture 明确清成 AIR；
- 坐标旋转和平移正确；
- 旧观察请求只覆盖 42 个中心线格，没有请求这两个 proof 依赖；
- 首跳、后继和 fallback 的联合 proof 依赖每个方向都是 67 格，旧请求每个方向漏 25 格。

夹具现在直接从冻结 proof 的 parent、successor 和 fallback dependencies 生成观察请求。trial 开始前逐格报告 known／unknown／mismatch；UNKNOWN 不会当作 AIR。离线四方向均为 67／67 known、0 unknown、0 mismatch，且全部依赖都位于已声明 fixture 体积内。故意把一个 AIR 事实改成 UNKNOWN 时，门禁会准确报告未知格。D122 相邻门禁 69／69 通过。

这一步完成时没有重跑 Fabric。v7 的 timely／L+2／never 实际样本均为 0，36 场矩阵未运行。原始失败位于 `artifacts/fabric-deployment/20261020T113835759757Z-d5255eb7`；公开预检结果位于 `evidence/motion_navigation/d122-client-timing-v1/d122-4/dependency-precheck.json`。随后单独授权的 append-only v8 结果见下一节。

### D122-4 v8 真实观察止损

获得单独授权后，最终前置按当前字节重跑：11 个模块 95／95、overlay `compileJava --rerun-tasks` 通过。`v8-preflight.json` 绑定 42 个正式链源码和四份前置证据，SHA-256 为 `2b1de4cd419822203b369f19c46b622700c6bedb9472fe55960a09011a65cb12`；v8 `summary.json` 已记录同一摘要。

v8 进入真实 server／client 和 fixture 后，trial 前观察门禁得到 67 个 proof 依赖中的 55 个 known、12 个 UNKNOWN、0 mismatch。12 格均位于 `y=-2`；客户端的显式 air-query 对其中 10 格返回 `occluded`，2 格返回 `outside_view`。fixture 已把它们清成 AIR，但这不是 actor 可用的观察证据，因此门禁保持 UNKNOWN 并停止。没有 timely／L+2／never 协议样本，36 场矩阵未启动。

原始运行位于 `artifacts/fabric-deployment/20261020T115158638012Z-bc79106d`，停止摘要位于 `d122-4/fabric-smoke-v8/observation-stop.json`。D122-4 当前结论为 **PARTIAL／STOPPED**：环境和装配前置已经完成，但真实协议样本为 0／3，36 场矩阵没有运行。

### D122-4 收尾复核

独立复核发现，最初的共用 validator 对 `raw_case_passed`、异常、事件归属和生命周期检查不够严格。修正后，组件 `raw_case_passed=false`、timely／late 任意异常、错误 request sequence、非正整数默认时长、错误 Runtime failure、旧身体观察和缺失健康证据都会被拒绝。never 的未送达请求会先保存 sequence、精确 `L+1` 窗口和注入原因；它没有到达客户端，也没有被标记为 applied。

真实 Runtime 等价检查确认：Fabric backend 抛出的 `TimeoutError` 会由 `PlayerRuntimeV1.step()` 转成 `DEADLINE_EXCEEDED` 结果，Runtime 进入 `failed`，outer error 保持为空。driver 随后停在 `stopping`，source 未释放。父请求 physics sidecar 可以证明默认结束时的身体位置、速度和着地状态，但旧 Runtime observation 不能证明这段默认之后的健康。当前 validator 因此会报告生命周期或健康证据缺失，不会把它包装成通过。

这次只修了验收夹具和门禁，没有新增 Fabric 协议样本。v8 仍为 0／3，36 场仍未运行。后续需另立决定，区分“单请求丢失、通道仍活”和“整个通道失联”；前者需要 typed rejection 和新观察，后者只能执行默认后报告身体责任未解决或对应 typed 终态。

下一次 Fabric 前置还必须绑定 D122-3 的 27 个生产语义文件。历史 v8 preflight 只证明当时的观察门禁，不能代替这项新要求。v1—v8 自检同时核对固定状态、source run 和真实 trial 数；重新计算一张哈希表不能掩盖历史边界被替换。

收尾第一次组合复跑保留为 92／94：一项是当前 D122 harness 指纹尚未刷新，另一项使用了仓库外的全局 Python，该解释器没有 NumPy。复核随后指出，后者同时暴露了一个真实导出边界：`d122_fabric_timing` 顶层导入组件计算夹具，会让最小 standalone 间接依赖 NumPy。修正只把组件夹具导入移到 `component_trial()`，没有给 standalone 增加 NumPy；D120 历史哈希保持不动，当前 D122 harness 指纹单列刷新。原 92／94 日志不回写。

原 `component-preflight.json` 生成于共用 validator 加固之前，只保留为审查前历史证据。追加的 `component-preflight-review-fixes.json` 由当前代码重新执行及时、`L+2` 和 never，3／3 通过；final self-check 会把三行重新交给当前 validator，而不是只核文件哈希。

后续有两条明确路线。第一条只改夹具／观察准备，通过新观察点或分阶段观察，让这 12 个格子在 trial 前真实可见并稳定。第二条另立 proof 语义决定，证明外围支撑已知时这些下层格不影响动作，再缩小查询范围。第二条不属于本阶段，也不能作为夹具修正混入。无论选择哪条，`UNKNOWN` 都不能当作 `AIR`。

## D126 纠偏记录

上述两条路线不再属于当前运动验收。D126 使用已有 `TEST_ORACLE` 将有限 fixture 地图写入 Runtime owner；它既不把 `UNKNOWN` 当空气，也不声称获得生产感知能力。D122-4 的原始结果与证据保持不变。
