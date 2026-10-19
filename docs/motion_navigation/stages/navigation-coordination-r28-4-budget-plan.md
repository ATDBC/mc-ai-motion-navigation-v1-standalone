# R28-4：持续任务恢复预算实施计划

日期：2026-10-04。状态：A0—E 已完成，R28-4 按冻结范围关闭。下一阶段为 R28-3。

本计划落实 [D048](../decisions/0048-converge-recovery-by-risk-and-product-evidence.md) 已经确定的预算规则。它不改变动作物理、路径评分、终点接近或异步结果失效规则。R28-4 完成后才进入 R28-3。

## 1. 要解决的问题

当前 `RetryLedger` 同时维护按原因、按轮次和整项任务的失败次数。它适合会结束的一次性导航，但不适合持续目标：一次跟随可能运行几十分钟，即使每次恢复都成功，累计到第 13 次也会永久失败。

现有等待还有三个重复来源：Session、规划 owner 和动作 owner 都可能开始或结束等待。相同偏差也可能被不同模块重复计费。修订目标或重新进入流程后，部分等待会换身份，导致期限和责任难以核对。

R28-4 只统一下面三件事：

1. 一次恢复周期只计费一次；
2. 有限任务和持续任务使用不同的恢复额度；
3. 单次恢复和连续无进展都有明确上限。

伤害额度、单动作确认期限、输入生效窗口和任务总期限继续由原拥有者管理。

## 2. 采用的设计

### 2.1 仍由 `RetryLedger` 统一拥有任务预算

不新增跟随专用账本，也不让技能层保存恢复次数。任务开始时根据 `GoalReachPolicy` 固定一种预算策略：

| 到达策略 | 恢复额度 | 不会被什么刷新 |
|---|---|---|
| `COMPLETE_ON_REACH` | 整项任务最多开始 12 次恢复 | 目标修订、路线重建、真实进展 |
| `KEEP_ACTIVE_ON_REACH` | 60 秒滚动窗口内最多开始 12 次恢复 | 目标修订、接替、真实进展、重新创建驱动 |

持续任务还限制：

- 单次恢复最长 10 秒；
- 目标尚未满足时，连续 30 秒没有真实进展就结束本轮任务；
- 目标已满足且身体稳定时暂停无进展计时。目标再次移动后，从当时重新计时。

10 秒单次恢复和 30 秒连续无进展只用于 `KEEP_ACTIVE_ON_REACH`。有限任务继续依靠累计 12 次、原任务总期限和各 owner 的局部等待期限；R28-4 不把这两个持续任务期限顺手扩到有限任务。

这些数值沿用验收第 7.1 节已经冻结的首轮参数。时间统一使用任务的单调时钟。滚动窗口采用 `(now - window, now]`，恰好离开窗口的旧记录可以淘汰。

### 2.2 用“恢复周期”替代按原因批准流程

恢复周期从共同交接入口决定“需要从当前观察恢复”时开始。`RetryLedger` 只计费和检查期限，不根据进展自行宣布身体已经恢复。

只有共同交接入口取得以下任一证据后，才能结束恢复周期：

- 新路线或原路线已经由监督者合法接管，身体恢复正常执行；
- 身体已经安全静止，原控制者完成输入收尾并交出责任；
- 原任务恢复责任已经完成，剩余独立收尾转给另一个有自己更短期限的现有 owner。若新 owner 仍在恢复目标行动，原恢复周期继续。

任务取消或进入业务终态不代表恢复结束。空中落地、输入收尾或世界操作确认仍在进行时，原恢复起点继续约束收尾；不能靠先写终态逃过 10 秒上限。

同一恢复周期内，规划超时、旧回执、复核失败等可以继续记录原因，但不会再次扣额度。重复的恢复身份保持幂等。新恢复只能在上一周期结束后开始。

`RetryCause` 只用于诊断。它不再决定“同一原因最多两次”或“一轮最多六次”。有限任务只看整项任务的 12 次上限；持续任务只看滚动时间窗。

### 2.3 真实进展只影响无进展期限

继续沿用三种已有 `ProgressEvidence`：

- 到达路线中的新支撑点；
- 动作完成；
- 当前阻塞事实得到回答。

转头、重新提交相同规划、切换控制来源、在两个旧位置间往返、更新目标版本都不是进展。一次观察里的多个事实最多推进一次期限。

真实进展只重新开始连续无进展计时，不结束恢复周期。取得缺失事实、完成一个动作或跨过新支撑点以后，可能仍需规划、接管身体或消除输入尾迹；结束恢复必须由共同交接入口确认。

真实进展不会：

- 删除持续任务窗口内的恢复记录；
- 补回有限任务的累计恢复次数；
- 补回伤害额度。

### 2.4 owner 负责结束自己的等待

信息 owner、规划 owner、探边 owner 和动作 owner 继续使用同一个 `RetryLedger`，但只能结束自己建立的等待。Session 只按固定顺序推进这些 owner，并消费预算结果。

R28-4 删除或下放 Session 中重复的等待清理和计费。`end_wait()` 必须同时核对等待身份和 owner，不能只凭 `wait_id` 删除其他 owner 的等待。`end_owner_waits()` 只能在该 owner 确实退出时调用。活动等待保持有界；owner 退出后活动记录必须为零。历史只保留有界诊断，不占活动等待容量。

局部等待继续保留各自更短的 `WaitPolicy`。信息获取、探边停止或地面入口恢复先达到局部期限时，立即返回局部结果；任务级 10 秒恢复期限包住这些步骤，但不会替代或放宽它们。

预算耗尽后，不再开始新的目标动作。已经承担的空中落地、输入收尾或世界操作确认仍由原 owner 完成，业务结果不会因为收尾而恢复成活动状态。

### 2.5 持续任务的进展去重保持有界

当前实现永久保存所有支撑点、动作和事实身份，达到 4,096 条后会拒绝后续进展。持续任务不能沿用这种永久集合。

R28-4 只保留覆盖连续无进展期限所需的近期身份：

- 近期身份至少保留 30 秒，并在期限检查完成后才淘汰；
- 两个旧位置之间反复往返，不能靠身份刚过期逃过 30 秒期限；
- 目标已经满足并稳定等待后，正式观察确认目标再次不满足，才开始新的需求区间；新需求可以沿旧路线返回并产生合法进展；
- 历史总数改为整数诊断，不靠永久保存每个身份计算；
- 有界容量按 Minecraft 可达到的最大移动和事件频率留足余量，容量不足时明确失败，不能静默把重复项当作新进展。

长时检查增加两类反例：经过超过 4,096 个有效支撑点；在闭环路线中反复往返。前者不能耗尽账本，后者不能冒充持续接近目标。

## 3. 公共接口

### 3.1 预算策略

在 `retry_ledger.py` 增加一个小型不可变策略对象。它只表达两种现行策略及冻结阈值，不设计通用配额框架。

任务创建时由 `GoalReachPolicy` 选择策略。目标修订不能修改策略。测试可以显式传入较短期限，但正式代码只有一个创建入口。60／10／30 秒在 R28-4 完成后成为现行正式默认，不再只称为测试参数；以后调整需新增决定。

### 3.2 恢复和期限接口

`RetryLedger` 提供下面四类行为，具体名称可在实现时按现有命名调整：

- 由共同交接入口开始、完成或重复报告一个恢复周期；
- 记录已有的真实进展证据；
- 报告目标未满足、目标已满足或任务终止；
- 在当前任务时钟上检查恢复速率、单次恢复和连续无进展期限。

返回值必须是枚举或不可变结果，至少区分：允许继续、有限任务额度耗尽、持续任务速率耗尽、单次恢复超时、连续无进展超时。不得靠原因字符串选择流程。

现有 `record_failure()` 的调用者分四类迁移：

- 开始任务恢复：由共同交接入口购买一次恢复；
- 恢复内诊断：只记录本周期里的规划、复核或回执失败；
- 局部动作期限：继续使用动作或等待 owner 的原期限；
- 普通失败：返回原有类型结果，不购买恢复。

### 3.3 诊断

保留总恢复数、时间窗内恢复数、当前恢复起点、最后真实进展和原因统计。目标满足暂停无进展计时必须同时满足：正式观察为 `SATISFIED`、没有执行或停止中的身体责任、没有未处理的在途移动输入，并且监督者确认可以安全保持。目标修订本身不改变计时；只有正式观察确认目标再次不满足，才开始新的需求区间。

每帧顺序固定为：先吸收真实进展和正式目标状态，再检查期限，最后决定是否允许新目标动作。恰好在边界到达的真实进展不会被提前判成超时。

`tests/sim/product_metrics.py` 继续只读取外部可见的规划提交、零位移区间和控制者切换，不拿内部计数判断新旧行为等价。

## 4. 串行实施任务

### A0. 先冻结现有调用语义

主要文件：`retry_ledger.py`、`navigation_handoff.py`、`planning_coordinator.py`、`motion_coordination.py`、`navigation_owners.py`、`navigation_session.py`。

逐项登记全部 `record_failure()`、恢复等待和重规划入口。每项标明：属于开始恢复、恢复内诊断、局部动作期限还是普通失败；恢复身份由谁创建、谁结束；是否已经承担身体或效果责任。

同时核对 `spawn_successor()`。它只表示开始真正的新任务：可以复用 worker，但必须建立新的目标、恢复和风险账本。若同一任务只是因为资源生命周期而重建 Session，这不叫 successor；需要另一个明确保留 `task_id`、到达策略和原账本的任务内重建入口。当前没有这样的正式入口。持续目标不得通过反复 successor 刷新额度，正式跟随后续只在同一任务中修订目标。

放行条件：所有生产调用都有唯一分类；文档、代码和测试对 successor 的任务身份一致；分类不依赖原因字符串。

### A. 固定契约和失败检查

主要文件：`retry_ledger.py`、`test_retry_ledger.py`、`test_goal_reach_policy.py`。

先写当前代码会失败的检查，覆盖两种策略、窗口边界、重复身份、修订不刷新、真实进展不补额度、目标满足暂停和伤害额度不受影响。有限任务原有 12 次累计上限保持；删除原因／轮次门槛属于有意修改，必须另测原第三次同原因失败和第七次同轮失败的结果、时间与安全收尾。

补充持续任务通过 `update_goal` 修订时保留窗口、真正新任务的 successor 建立新账本、业务终态后身体收尾继续受恢复期限约束，以及进展不会提前结束恢复周期。测试中不再出现“同任务 successor”。

在删除 F1—F6、F10 的任务扣费前，先写三个跨 work identity 的反例：worker 永不返回、每个新 candidate revision 都要求复核、每个新 planning attempt 都超时。首版保持现行可观察上界：同一 motion connection 或同一 planning retry chain 允许前两次重建，第三次失败必须返回有类型的动作／规划结果；若此时确认现实已经偏离并需从当前观察重建路线，再进入 F8，且只购买一次任务恢复。新 candidate revision、attempt revision 或 async work window 不能刷新这项累计局部上界。有限任务和持续任务都执行同一门槛。

放行条件：组件检查能明确区分五种预算结果；没有生产代码修改前，新增目标行为检查按预期失败。

### B. 实现唯一任务账本

主要文件：`retry_ledger.py`，以及最少量构造入口。

实现有界的恢复历史、当前恢复周期、近期进展身份和无进展时钟。原因统计只作诊断。使用注入的任务单调时钟，使 Windows、Linux 和快慢机器得到相同结果。任务接纳时建立初始无进展起点，不等到第一次恢复才开始。

放行条件：A 的组件检查通过；窗口内历史有固定容量；时钟倒退、身份冲突和容量耗尽有明确结果；不依赖墙钟跑到哪一步来决定行为。

### C. 迁移恢复与等待调用者

主要文件：`navigation_handoff.py`、`planning_coordinator.py`、`motion_coordination.py`、`navigation_owners.py`、`navigation_session.py`、`moving_melee_driver.py`。

按 A0 清单逐一迁移。真正偏离只开始一个恢复周期；周期内的规划或动作复核失败只登记原因。共同交接入口取得正式交接证据后结束周期。等待由建立它的 owner 结束。Session 删除重复清理、按原因批准和刷新期限的分支。

`spawn_successor()` 改为真正的新任务入口，只复用 worker，并新建目标、恢复和风险账本。`MovingMeleeDriver` 在同一战斗任务里重新接近时，改用命名明确的任务内重建入口；该入口沿用原 `task_id`、到达策略、`RetryLedger` 和 `TaskRiskLedger`。持续任务只通过 `update_goal` 修订位置。测试必须同时核对恢复窗口和累计伤害，不能只证明其中一本账没有刷新。

`MotionRouteCoordinator` 的正式构造改为必须显式接收 `RetryLedger`。组件测试需要独立账本时自行创建。连续两条正式路线必须断言使用相同 `task_id` 和同一个账本对象，禁止每条路线走缺省构造重新获得额度。

放行条件：同一帧的多个通知只扣一次；目标修订、接替和重新规划不补额度；所有活动等待在 owner 退出后清零；任务终态不可被预算结果重新激活。

### D. 正式链和长时模拟

主要文件：现有 `tests/sim` runner、场景清单和 R28 专项测试。只扩展现有模拟器，不另建一套。

冻结并运行：

- 30 分钟持续任务，每 90 秒恢复一次，累计超过 12 次仍活动；
- 60 秒内第 13 次恢复，返回速率耗尽；
- 单次恢复超过 10 秒；
- 目标未满足且 30 秒没有真实进展；
- 目标满足后长时间保持，再次移动；
- 经过超过 4,096 个有效支撑点后继续产生真实进展；
- 闭环路线反复往返不能持续刷新无进展期限；
- 每 3／5／8 tick 修订目标，不刷新窗口；
- 取消、改目标、晚到和丢回执发生在恢复期间；
- 预算耗尽后，原身体 owner 仍完成有界安全收尾；
- 整项任务的伤害额度没有补回。

长时检查使用虚拟单调时钟，不真实等待 30 分钟。

放行条件：安全不变量零违规；所有任务有明确终态或持续活动状态；不出现无 owner 的输入、永久活动等待或靠换实例补额度。

### E. 回归、文档和整理

先运行直接相关的预算、目标策略、交接、信息和规划测试。稳定后运行完整 `tests/motion_nav`、1,448 项协调集合、四个补充故障入口和 v7 产品清单。

R28-4 不主动改变一次性任务的正常成功路线和输入。删除按原因／轮次批准会有意改变部分有限任务失败路径；这些场景逐项比较结束时间、规划次数和安全收尾。持续任务按新规则单列，不与旧累计预算强求逐帧相同。没有输入或 Fabric 接口变化时不重跑无关实机场景；若正式输入路径发生变化，只跑受影响的代表性 Fabric 对照。

完成后更新：

- 本阶段文件的状态和删除清单；
- `navigation-coordination-v1.md` 的最终接口名称；
- `navigation-coordination-convergence.md` 的命令、结果和证据边界；
- `AGENTS.md` 的当前阶段。

## 5. fail fast 顺序

每个任务先跑 3—8 个最容易暴露错误的检查。发现未预期失败就停止扩大矩阵，保留最短反例并检查共同规则。只有直接相关检查稳定后，才运行完整回归和产品清单。

子 agent 按 A0→A→B→C→D→E 串行工作，不并行修改共享代码。每个实现任务完成后，由另一个子 agent 只做审查；发现问题交回原实现 agent 修复，再进行定向复审。最终再做一次跨任务审查。

## 6. 明确不做

- 不实现正式跟随技能；
- 不修改终点贴墙、边缘接近和自动转身；
- 不改变物理、路径评分、动作证明和风险计算；
- 不提前实施 R28-3 的异步世代规则；
- 不增加通用工作流、插件系统或第二本恢复账本；
- 不用更多计数器掩盖没有结束条件的等待。

## 7. 完成标准

R28-4 只有同时满足下面条件才关闭：

1. 有限任务仍按整项任务最多 12 次恢复；
2. 持续任务的速率、单次恢复和连续无进展期限全部由同一本账本执行；
3. 修订、接替、真实进展和驱动重建不能补回恢复额度；
4. 目标满足等待不误算无进展，目标再次移动后期限重新开始；
5. 预算耗尽后仍完成已经承担的安全收尾；
6. 等待 owner 退出后活动记录为零；
7. 完整回归没有未解释退步，安全不变量零违规；
8. 旧的按原因流程分支和重复等待清理已删除或有明确保留依据。

## 8. A0：现行调用语义清单

本节以基线 `5536dea` 为准。生产代码共有 **31 个**直接账本调用：`record_failure()` 10 个，等待 API 21 个。分类只看有类型的调用位置、工作身份和交接目的，不读取 `reason` 文案。

表中的“局部动作／等待期限”表示该调用限制一个规划、动作或取证 owner。它不能消费任务恢复额度。“恢复内诊断”只记录已经开始的恢复中发生了什么，也不能再次消费任务额度。“开始任务恢复”才可以在 `RetryLedger` 里购买一次恢复。

### 8.1 `record_failure()` 的十个调用

| 编号 | 生产调用 | 冻结分类 | 谁建立身份；谁可以结束 | 身体／效果责任 | 现有检查 | Task C 处理 |
|---|---|---|---|---|---|---|
| F1 | `motion_coordination.py:776`，动作结果要求重新验证 | 局部动作／等待期限 | `MotionRouteCoordinator` 以稳定 motion connection 建立累计重试链；candidate revision 只标记单次尝试。证明安装、动作取消、第三次失败的有类型结果或转入 F8 后结束 | 路线 executor 已持有或准备接管身体；没有世界操作效果 | 没有直接命名断言；B10 动作候选回归只覆盖相邻的候选重验证失败 | 停止购买任务恢复；新增跨 candidate revision 的累计局部上界，单个工作窗口不算上界 |
| F2 | `motion_coordination.py:795`，预计的下一动作入口失效 | 局部动作／等待期限 | motion owner 以稳定 motion connection 建立累计重试链；仍在执行的地面段继续。证明安装、路线结束、第三次失败或转入 F8 后结束 | 当前地面段仍持有身体 | 没有针对该分支的直接断言 | 停止购买任务恢复；入口重新准备也必须累计在同一局部链，不能因 revision 变化清零 |
| F3 | `motion_coordination.py:804`，当前动作候选准备失败 | 局部动作／等待期限 | motion owner 以稳定 motion connection 建立累计重试链；证明安装、动作取消、第三次失败或转入 F8 后结束 | 当前路线持有身体；严格动作尚未获准起步 | `test_coordinator_bounds_identical_revalidation_retries` | 停止购买任务恢复；保留现有“第三次失败有类型结束”的可观察上界 |
| F4 | `motion_coordination.py:864`，已送达的 motion 结果过期 | 局部动作／等待期限 | motion owner 以稳定 motion connection 建立累计重试链；work identity 只标记一次后台工作。新结果获准、第三次失败或转入 F8 后结束 | 已有路线仍可能持有身体；过期结果本身没有权限 | 没有直接断言；异步 admission 测试覆盖迟到结果丢弃，但没有单独钉住此计费行 | 停止购买任务恢复；过期后重发不能用新 work identity 刷新累计上界 |
| F5 | `motion_coordination.py:1210`，等待中的 motion 工作过期 | 局部动作／等待期限 | motion owner 以稳定 motion connection 建立累计重试链；单次 work window 到期只结束一个尝试。新结果获准、第三次失败或转入 F8 后结束整链 | 已有路线／落地 owner 可能持有身体 | `test_motion_backpressure_uses_fixed_deadline_and_shared_retry_limit`、`test_online_coordinator_retries_expired_solver_request_with_shared_budget` | 停止购买任务恢复前先新增跨 work identity 上界；worker 永不返回时第三次必须 typed 结束或进入 F8 |
| F6 | `motion_coordination.py:1259`，已验证入口在提交前变化 | 局部动作／等待期限 | motion owner 以稳定 motion connection 建立累计重试链；candidate revision 不改变整链身份。重新求证成功、第三次失败或转入 F8 后结束 | 路线持有身体；未启动的严格动作尚未获得新许可 | `test_entry_state_must_still_fit_the_verified_entry`覆盖入口拒绝，未直接断言此账本扣费 | 停止购买任务恢复；丢弃未启动证明后的重算仍累计在同一局部上界 |
| F7 | `motion_coordination.py:1318`，着地后的已验证入口恢复 | 局部动作／等待期限 | `motion-route/<route_id>` 的 grounded-entry wait 已经跨帧保持同一开始时间；回到入口、确认安全停止、取消工作或局部 wait 到期结束 | route owner 正在施加安全恢复输入，持有身体 | `test_i4_detects_motion_without_body_control_progress`只检查外部不变量；没有直接钉住扣费行 | 停止购买任务恢复；保留现有跨帧 wait。它不需要 F1—F6 那种新的跨 revision 上界 |
| F8 | `navigation_handoff.py:264`，`request_recovery(... REPLAN ...)` | 开始任务恢复 | `NavigationHandoffCoordinator` 以稳定 `request_id` 建立恢复身份；共同交接取得安全释放及重新规划去向后结束 | 一般已有 incumbent／probe 身体责任；重复请求不得重复收费 | `test_recovery_is_charged_once_and_waits_for_current_release`、`test_true_retry_keeps_its_permission_when_latest_goal_is_staged`、`test_paid_recovery_keeps_its_existing_priority` | **保留为唯一购买入口**；Task C 把其他恢复入口汇入这里 |
| F9 | `planning_coordinator.py:534`，`retry_from_current()` | 恢复内诊断；但现行上游语义混合，见 8.3 的阻塞项 | 目标契约要求恢复身份先由 handoff 建立；规划 owner 只能记录该恢复中的新规划尝试，路线接管或共同恢复结束时关闭 | 可能有 incumbent 身体责任，也可能只是尚未接纳的候选 | `test_runtime_bridge_keeps_landing_owner_when_dependency_changes`覆盖其中一个入口；其余三个入口没有逐项检查 | 该行必须停止购买任务恢复；四个上游先按有类型事件拆分，真正偏离先经过 F8，纯候选失效走 R28-3／普通失败 |
| F10 | `planning_coordinator.py:1719`，规划尝试超时、worker 死亡、依赖变化或 admission 失败后的 `_retry_or_fail()` | 局部动作／等待期限 | planning owner 以稳定 planning retry chain 建立累计上界；attempt revision 和单次 deadline 只标记一次尝试。候选交付、第三次失败的 typed `PlanningFailure` 或转入 F8 后结束 | 通常不持有身体；incumbent 可独立继续持有身体 | `test_dependency_changes_consume_shared_retry_limit`、`test_planning_timeout_uses_shared_limit_and_has_typed_terminal`、`test_snapshot_building_uses_attempt_deadline_despite_restarts`、`test_no_route_result_is_rebased_when_snapshot_scope_changed` | 停止购买任务恢复前先新增跨 planning attempt 上界；每次 attempt 都超时时不能永久重建窗口 |

结论：十个直接扣费点中，只有 F8 可以继续购买任务恢复。F1—F6、F10 只有在跨 revision 的累计局部上界生效后才能退出任务恢复额度；F7 已有跨帧 wait，可以直接退出；F9 必须先消除上游混合语义，再改为恢复内诊断。

### 8.2 等待 API 的二十一个调用

| 编号 | 生产调用 | 冻结分类 | 谁建立；什么证据可以结束 | 身体／效果责任 | 现有检查 | Task C 处理 |
|---|---|---|---|---|---|---|
| W1 | `motion_coordination.py:1294` `begin_wait`；`motion_coordination.py:1301` `check_wait`；`motion_coordination.py:629` `end_wait` | 局部动作／等待期限（3 个调用） | `MotionRouteCoordinator` 以 `motion-route/<route_id>` 建立；入口恢复、确认安全停止、不再处于 grounded recovery、取消 route work 或期限耗尽结束 | route owner 持有身体 | B10 motion 候选回归和协调 I4 间接覆盖；缺少 wait 身份与退出的直接断言 | 保留局部期限；结束接口必须带 owner，不能只传 wait id |
| W2 | `navigation_owners.py:243` `begin_wait`；`navigation_owners.py:246` `check_wait`；`navigation_owners.py:201` `end_wait` | 局部动作／等待期限（3 个调用） | `InformationAcquisitionState` 使用 Session 提供的 information owner id；当前 blocker 被正式观察、信息需求替换／退出或期限耗尽结束 | information wait 本身不持有身体；incumbent route 可并存 | `test_repeat_query_does_not_restart_information_wait`、三个 `test_*information_wait*` | 由 information owner 统一结束；Session 不再直接删同名 wait |
| W3 | `navigation_owners.py:289` `begin_wait`；`navigation_owners.py:290` `check_wait` | 局部动作／等待期限（2 个调用） | `LandingEdgeProbe.owner_id` 建立 acquisition wait；probe 获得证据、超时、被替换、完成安全退出或 owner 退休时结束 | probe 持有边缘潜行与观察身体责任 | `test_releasing_view_or_pose_keeps_original_acquisition_deadline`、`test_edge_probe_timeout_keeps_body_until_safe`、中断矩阵 | 由 probe／information owner 返回结束事实，监督者退休 owner 后关闭；不计任务恢复 |
| W4 | `navigation_session.py:538` `end_owner_waits` | 局部动作／等待期限（1 个调用） | Session 当前代替 probe owner 清理；正确证据是监督者已退休该 quiescent probe | 清理时 probe 身体责任必须已经交接或安全释放 | `test_cancel_failure_or_revision_stops_probe_and_suspended_route`、`test_repeated_reachable_goal_revisions_do_not_leak_probe_waits` | 保留按 owner 清理，但调用应随 probe 退休事实发生，不由 reason 或终态推断 |
| W5 | `navigation_session.py:552` `end_owner_waits` | 局部动作／等待期限（1 个调用） | Session 关闭时清理 Session information/recovery owner 及当前 probe owner；证据应是对应 owner 已退出 | 可能仍有身体／效果收尾；清 wait 不能冒充责任释放 | `test_terminal_session_retires_owned_waits_before_successor_reuses_ledger`、I14 终态等待不变量 | Task C 改为各 owner 退出时关闭；最终 Session 资源清理只作兜底并核对零活动 wait |
| W6 | `navigation_session.py:1930`、`navigation_session.py:2087`、`navigation_session.py:2121`、`navigation_session.py:3683`、`navigation_session.py:4014` 的 `end_wait("information")` | 局部动作／等待期限（5 个调用） | 当前由 Session 在开始探边、规划信息失败／取得、切换探边或超时时删除；正确结束者是 information/planning owner 提交的 acquired、failed、superseded 或 timed-out 事实 | information wait 不拥有身体；切换探边后 probe 可能立即持有身体 | `test_r28_information_planning_updates` 全组、`test_information_wait_bounds_missing_query_responses`、`test_occluded_landing_requests_probe_without_turning` | 五处都下放给 owner；`end_wait` 增加 owner 核对，不能跨 owner 删除 |
| W7 | `navigation_session.py:3293`、`navigation_session.py:3491` 的 `begin_wait`；`navigation_session.py:3500` `check_wait` | 局部动作／等待期限（3 个调用） | probe owner 或 `navigation-session/<id>/recovery` 建立停止收尾 wait；安全释放、probe handoff 完成或局部期限耗尽结束 | 明确持有 probe／route 的身体收尾责任 | `test_stop_deadline_reports_unresolved_without_releasing_body`、`test_probe_pushed_off_tall_ledge_retires_on_lower_safe_support` | 这是停止收尾期限，不是任务恢复周期；改名或有类型区分，避免与 F8 的 recovery 混为一谈 |
| W8 | `navigation_session.py:2715`、`navigation_session.py:3215` 的 `end_wait("recovery")` | 局部动作／等待期限（2 个调用） | 当前由 Session 在终态分支或 probe handoff 后删除；正确证据是对应 stop owner 已安全释放并由监督者退休 | 结束前通常仍有身体责任；终态本身不是结束证据 | `test_stop_deadline_reports_unresolved_without_releasing_body`、probe handoff／取消中断组 | 移到监督者确认的 owner 退出路径；禁止仅因业务终态结束 wait |
| W9 | `navigation_session.py:3265` 的 `end_wait(acquisition_id)` | 局部动作／等待期限（1 个调用） | probe owner 建立；继续路线前，正式 route command 被选择且 probe 可以退出时结束 | probe 与 route 正在交接身体 | `test_edge_probe_handoff_finishes_with_constant_one_tick_latency`、`test_quiescent_terminal_predecessor_waits_for_pending_route_command` | 由 probe handoff 结果结束并核对 owner；Session 只路由结果 |

等待 API 的 21 个调用全部属于局部期限。它们都不能购买任务恢复。Task C 只统一 owner 核对和退出路径，不把单动作、信息获取或停止收尾期限合并成 10 秒任务恢复期限。

### 8.3 上游恢复、重规划与 successor 契约

| 入口 | 分类 | 冻结结论与检查 |
|---|---|---|
| `_consume_route_decision()` 收到 `NEEDS_REPLAN`，调用 `request_recovery(... REPLAN ...)` | 开始任务恢复 | 这是 F8 的正式入口。稳定 event id 幂等扣费；已有身体 owner 先完成交接。继续用 handoff 单元检查和中断矩阵。 |
| `_consume_route_decision()` 收到 `INPUT_LOST`，目标为 `FAIL` | 普通有类型失败 | 不购买恢复；原路线仍须安全释放。`test_input_lost_returns_a_terminal_result_without_replanning`、取消／空中中断检查覆盖。 |
| `_request_probe_stop(... terminal=...)` 与 `_request_ending()` | 普通有类型失败或任务结束 | `CANCEL`、`FAIL`、`COMPLETE`、`CLOSE` 都不购买恢复；已承担的 probe、route 和效果责任继续收尾。probe 中断、B11 终态停止和空中取消检查覆盖。 |
| `_reissue_request_from_current(... retry_cause=...)` → `PlanningCoordinator.retry_from_current()` | **阻塞：当前接口混合了两类语义** | 四个上游分别是世界操作依赖变化、活动路线依赖变化、successor 候选入口变化、候选伤害依据变化。前两项通常是现实偏离；后两项可能只是未接纳候选失效。现有 `retry_cause` 不能证明是否已有恢复周期，也不能证明是否持有身体。Task C 前必须把它们改成有类型的“共同恢复事件”或“候选失效／局部规划失败”，然后 F9 才能固定为恢复内诊断。不得按 reason 文案区分。 |
| `PlanningCoordinator._retry_or_fail()` | 局部规划期限 | 规划超时、worker 死亡、快照／路线依赖变化和 admission 失败在一个 planning attempt 内有界处理；失败后返回 typed `PlanningFailure`，不直接购买任务恢复。 |
| `stage_reanchor()`、`restart_from_current(... PROGRESS/TASK_UPDATE ...)` | 普通任务更新／已有进展 | 不购买恢复。目标修订、已有路线前移和信息取得不能补回恢复额度。 |

`spawn_successor()` 的现行文档和代码不一致，必须在 R28-4 集成前修正：

- 架构第 17.7 节把它定义为**真正的新任务**。新任务复用 worker，但新建 `GoalRequestLedger`、`RetryLedger` 和 `TaskRiskLedger`。
- 现行 `NavigationSession.spawn_successor()` 把旧 `_retry_ledger` 和 `_risk_ledger` 传给新 Session；`test_terminal_session_retires_owned_waits_before_successor_reuses_ledger` 还明确断言复用旧账本。
- `MovingMeleeDriver._start_approach()` 在旧 Session 终态后调用 successor，随后又用原 `task_id` 启动。这是“同一战斗任务重新接近”与“真正新导航任务”混在一个 API 里的现存用法。

R28-4 以架构语义为准，但 A0 不改代码。后续集成必须分别检查：

1. 真正新任务调用 successor 后，恢复与伤害账本都是新对象，旧任务的工作身份不能接纳到新任务；
2. 同一持续任务的目标修订只调用 `update_goal`，不会创建 successor，窗口内恢复记录和累计伤害保持不变；
3. 如果资源生命周期确实要求重建同一任务，Task C 新增命名明确的任务内重建入口，并证明它沿用原 `task_id`、到达策略、恢复账本和风险账本；没有这个入口时不得用 successor 冒充；
4. Task C 同时迁移 `MovingMeleeDriver._start_approach()`。同一战斗任务重新接近必须走任务内重建或任务修订，并同时保留恢复窗口与累计伤害；不能只修 `RetryLedger`。

这项矛盾会阻塞 Task A 中的 successor 预算检查。它不阻塞先为 `RetryLedger` 写纯组件失败检查，但在契约修正前不能宣布 R28-4 集成完成。

### 8.4 可绕过的账本创建入口

31 个调用点之外还有一个创建入口需要收口：`MotionRouteCoordinator.__init__()` 在没有传入 `retry_ledger` 时执行 `RetryLedger(route.goal_id)`。正式 Session 当前会显式传入任务账本，但生产类的缺省值允许其他调用者为每条路线获得新额度。

Task C 采用单一做法：正式构造必须显式传入 `RetryLedger`，删除生产 fallback；需要独立运动协调器的组件测试显式创建测试账本。新增正式链检查连续执行两条路线，断言两个 `MotionRouteCoordinator` 的 `retry_ledger` 是同一个对象，且 `task_id` 与 Session 任务一致。新路线、route revision 和 motion connection 都不能创建第二本任务恢复账本。

## 9. 实施结果

R28-4 已按 A0—E 的顺序完成，并通过独立复审。最终实现以 `RecoveryBudgetPolicy` 和 `RecoveryBudgetKind` 区分有限任务与持续任务；`RecoveryIdentity` 保证一次恢复只扣费一次；`RecoveryLimitStatus` 提供有类型的额度与期限结果。

每个观察帧先收集任务需求和真实进展，再由 `finalize_task_activity()` 作一次期限判断。只有本帧的一次性 `RecoveryActivityPermit` 仍有效时，F8 才能调用 `begin_recovery()` 购买任务恢复。规划和运动内部重建改由 `LocalAttemptChain` 限制，candidate、attempt、work revision 和失败原因都不能刷新局部次数。

迁移清单已经核销：

- 生产代码中的 `record_failure()` 调用已经全部删除；
- 直接调用 `RetryLedger.end_wait()` 的生产路径已经全部删除，等待通过 owner 校验的入口结束；
- F8 是唯一任务恢复购买入口，F1—F7、F9、F10 都归入 owner 局部链、局部等待或恢复内诊断；
- `spawn_successor()` 只开始真正的新任务并建立新账本；同一任务换 Session 壳使用 `same_task_continuation_evidence()` 和 `rebuild_same_task()`，保留恢复、风险和伤害历史；
- `MotionRouteCoordinator` 正式构造必须显式取得任务账本，不能按路线创建新额度。

最终门槛为：新增正式链场景 8/8，连同复用证据覆盖 11 组要求；聚焦检查 80/80；完整运动导航 1,124/1,124；协调集合 1,448/1,448；补充故障 4/4。新的 R28-4 v7 运行是 1,710/2,000，其中 1,995 项与冻结 v7 完全一致，另有 5 项既有晚到下降由失败转为成功；没有成功转失败或安全违规。冻结 v7 的 1,705/2,000 保持不变，详细证据与边界见[验收第 18 节](../acceptance/navigation-coordination-convergence.md#18-r28-4-持续任务恢复预算)。

最终整体审查又修正三个边界：结束恢复前先检查 10 秒期限；Session 诊断改为只读；I5 改读作出速率耗尽决定时的窗口快照。修正后的直接受影响检查为 242/242。

当前代码的 Fabric 批次 `20261003T203156813297Z-ff291252` 覆盖平视、方向 0 的二格下降，正常和首条晚一 tick 为 2/2；两场零伤害、来源释放，迟到实际生效，窗口外和无人负责输入为空。测试工具没有稳定触发 F8 世界偏离，证据边界保留。

R28-4 没有达到 R28 总体的代码净减少目标。与 `5536dea` 比较，9 个核心协调／driver 文件由 11,169 行增至 12,419 行，净增 1,250 行；其中 Session +411、handoff +317、ledger +444。R28-3 和 R28-5 继续承担结构收敛。

本阶段没有实现正式跟随、室内终点接近、R28-3、R28-5 或可选 R28-2。`spawn_successor()` 的新 `task_id` 仍由调用者保证，显式接口约束登记到 R28-3 或正式跟随探针。下一阶段按既定顺序进入 R28-3。
