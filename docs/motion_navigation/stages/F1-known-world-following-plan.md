# F1：已知开阔地正式跟随

日期：2026-10-06。状态：F1-A 至 F1-E 已完成检查。正式跟随产品能力和薄调用方边界通过；原“十二个核心文件零修改”门槛失败，F1 整体不写成全部完成。下一步进入独立、行为不变的结构整理。

## 1. 要解决的问题

项目已经能持续接收同一目标的新修订，但还没有一个正式跟随调用方。旧 `RuleFollower` 和 `PlaygroundFollower` 自己处理移动、搜索和恢复，不能证明新导航架构容易扩展。

F1 建立第一个正式跟随纵向切片。目标是在已知、开阔、普通地面上，持续跟随一个合法可见且身份固定的玩家。跟随层只管理目标，不参与约 20 Hz 的运动控制。

## 2. 范围

本阶段包括：

- 从正式 Observation V3 读取一个已绑定玩家的最新合法位置；
- 以 `KEEP_ACTIVE_ON_REACH` 开始一个持续导航任务；
- 在目标位置产生有意义变化时提交同一目标的新修订；
- 目标停止时在合理距离内待命；
- 目标再次移动时继续同一任务；
- 用户取消、目标身份失效或会话变化时有界结束；
- 模拟器、正式 Runtime 链和代表性 Fabric 实机证据。

本阶段不包括：

- 未知区域探索；
- 主动搜索暂时看不见的目标；
- 记忆或推测目标位置；
- 室内贴墙、墙角和走廊尽头接近；
- 疾跑、游泳、攀爬、载具和新运动方式；
- 自动选择多个玩家中的一个；
- 修改寻路、运动计算器、碰撞或感知语义。

目标暂时不可见时，首版返回有类型的“需要目标观察”，不自行转头搜索，也不把旧位置持续当成实时事实。

## 3. 设计

### 3.1 跟随层

新增一个薄的正式跟随驱动，暂定为：

- `mc2p/skills/known_world_follow_driver.py`。

它拥有：

- 跟随任务 ID；
- 已绑定的玩家 `track_id`；
- 当前目标修订号；
- 上一次提交给导航的目标位置和时间；
- 跟随状态与有类型结果。

它不拥有：

- 规划请求或计算世代；
- 路线、动作或输入账本；
- 恢复、风险和伤害额度；
- 后台结果接纳；
- 身体控制和交接。

它只调用 `RuntimeNavigationDriver.start()`、`replace_goal()` 和 `release()`／`stop()`。逐帧提案和 Runtime 推进继续由现有正式导航驱动完成。

### 3.2 目标位置和目标区域

开始时由任务层显式提供玩家 `track_id`。跟随层只接受同一世界会话中、合法可见、类型为 `minecraft:player`、未死亡的对应实体。

目标区域以玩家当前位置为中心，保留一个冻结的待命距离。首版不预测玩家未来位置，不把跟随目标放到障碍物内部，也不重新实现支撑面选择。目标几何继续交给现有 `GoalState` 和规划入口。

为避免每个采样 tick 都重规划，只有满足下面任一条件才提交修订：

- 目标中心相对上次提交位置移动达到冻结距离；
- 当前目标已经满足后，玩家离开保持区域；
- 冻结的最长修订间隔到期，而且位置确实变化。

阈值必须在验收清单中冻结，不能根据运行结果临时调整。节流属于跟随目标管理，不进入导航协调核心。

### 3.3 正常待命不是卡死

模拟器 I4 继续检查无界等待，但下面的情况不累计静止时间：

- 任务使用 `KEEP_ACTIVE_ON_REACH`；
- 当前目标状态已经满足；
- 没有活动恢复、取证、严格动作或身体收尾；
- 没有尚未满足的移动需求。

目标再次移动后，I4 必须从新需求出现的 tick 重新计时。该豁免只修改测试监视器，不改变正式导航。

### 3.4 结构限制

正式跟随实现不得修改下面十二个核心协调文件：

- `navigation_session.py`；
- `navigation_handoff.py`；
- `navigation_owners.py`；
- `planning_coordinator.py`；
- `motion_coordination.py`；
- `execution_supervisor.py`；
- `retry_ledger.py`；
- `async_work.py`；
- `route_body_controller.py`；
- `probe_body_controller.py`；
- `route_admission.py`；
- `planner_worker.py`。

如果现有接口无法完成首版跟随，先停止实现并记录缺少的通用契约。不能在核心文件中增加 `follow` 分支来通过验收。

首轮 F1-C 已触发这条规则：持续目标满足后仍有规划工作超时，移动中接纳路线会回到已越过的格心。它们按[D057](../decisions/0057-close-satisfied-work-and-remove-regressive-route-prefix.md)作为通用前置修正处理。初始“核心文件零修改”门槛如实记为未通过；D057 完成后重新冻结 F1 基线，后续跟随层提交继续执行零修改限制。

## 4. 实施顺序

### F1-A：修正量尺

先写失败检查，证明持续任务满足目标并静止超过 100 tick 时，I4 现在会误报。随后只修改 `tests/sim/monitor.py` 和必要的 `TickEvidence` 生产者，让正常待命不计为等待；目标重新移动后仍能发现真正卡死。

检查至少覆盖：

- 满足后静止 800 tick，无 I4；
- 静止后目标移动，任务继续；
- 未满足目标时静止 100 tick，仍报告 I4；
- 活动恢复或身体收尾不能借待命规则逃过上限。

### F1-B：薄跟随调用方

先写组件和正式 Runtime 链失败检查，再实现跟随驱动。至少覆盖：

- 唯一合法玩家绑定；
- 同一 `track_id` 的位置更新；
- 旧观察、重复修订、目标死亡、换世界和取消；
- 达到节流阈值才调用 `replace_goal()`；
- 跟随层不读取或修改导航私有状态；
- 导航终态后修订返回有类型结果，不抛异常；
- 空中取消仍由原导航完成安全收尾。

### F1-C：正式链模拟

新增固定清单，不改 v7：

- 目标速度 2.0、3.3、4.0 格／秒；
- 直线与缓慢横向移动；
- 移动、停止 40 秒、再次移动；
- 高频采样但目标修订被节流；
- 正常取消；
- 一帧输入晚到和一次目标观察缺失。

记录：

- 平均、P95 和最大跟随距离；
- 目标更新到首个有效移动的 tick；
- 接受／拒绝的修订数；
- 规划提交数和修订数之比；
- 任务恢复数、控制者切换、零位移区间；
- 安全不变量和终态。

不得把 5 格／秒目标追不上写成缺陷；本阶段未提供疾跑。但目标停止后是否能追上要单独记录。

### F1-D：Fabric 代表场景

使用独立 Fabric 和真实玩家实体，至少覆盖：

- 直线步行目标；
- 横向缓慢移动；
- 停止后再次移动；
- 用户取消；
- 首条输入晚一 tick。

机器人、目标玩家、场景种子、目标轨迹和起始距离事先冻结。记录实际目标位置、目标修订、规划提交、真实输入应用、身体轨迹和结束原因。

Fabric 结果只证明已知开阔地首版，不能外推到室内、未知地形或疾跑。

F1-C 固定清单未通过时不能进入本节。D057 的两个通用修正和 F1-C 量尺分组修正必须先分别通过审查，并重新运行完整固定清单。

#### 2026-10-05 启动前实施记录

本轮复用现有 `FollowRuntimeHost`。它启动一个隔离的本地服务端和两个独立的无头 Fabric 客户端。机器人固定为 `MC2PFollower`，目标玩家固定为 `MC2PLeader`。两者分别拥有自己的 `PlayerRuntimeV1`、IPC 端口、Observation V3 和输入仲裁。机器人只从自己的合法观察中绑定目标 `track_id`；评测可以读取两个客户端的实际位置，但不能把目标真值送给机器人。

场景使用种子 `21001` 和 `static` 开阔草地方案。机器人起点是 `(.5, -60, .5)`，目标玩家起点是 `(.5, -60, 6.5)`，初始水平距离为 6 格。每个场景都从新生成的世界开始。测量期间不传送玩家，也不直接修改世界。

目标速度固定为约 2.0 格／秒。目标客户端按 11 tick 一周期的 `11001100100` 节奏提交普通单 tick 步行输入，实机结果仍以目标客户端实际位置计算速度，允许范围为 1.8—2.2 格／秒。冻结场景各运行一次：

| 场景 | 目标轨迹与边界 |
|---|---|
| `straight_2_0` | 面向保持不变，沿前方移动 66 tick，随后静止 |
| `lateral_2_0` | 面向保持不变，向左横移 66 tick，随后静止 |
| `move_stop_800_resume_2_0` | 原冻结场景：移动 33 tick，静止 800 tick（40 秒），再移动 33 tick；D065 在后续章节另行修订恢复段 |
| `normal_cancel_2_0` | 目标移动时在第 44 tick 取消跟随，检查安全收尾与来源释放 |
| `first_input_late_one_tick_2_0` | 首个非空机器人移动提案准备完成后延迟 55 ms，再用真实应用 tick 核对晚一 tick |

能力判定沿用 F1-C：稳定移动窗口的超出保持区距离平均值不高于 0.75 格、P95 不高于 1.5 格；规划提交／接受修订不高于 1.25；修订响应 P95 不高于 5 tick；安全违规为 0；目标停止后进入 2.5 格保持区。`normal_cancel_2_0` 仍报告距离，只以取消、安全、来源释放和有界终态判定。每个场景只有一个种子、一次运行，因此结果只算代表性实机证据，不算统计证明。

正式链固定为 `PlayerRuntimeV1 -> RuntimeNavigationDriver -> KnownWorldFollowDriver`。目标玩家也只通过自己的 Runtime 输入出口移动。实现不得导入或调用旧跟随器。两个客户端分别保存分段 Runtime 轨迹、诊断、时间事件和真实输入应用；场景摘要记录目标位置、修订、规划工作、身体位置、距离、控制来源和结束原因。

启动前最终报告写入 `.tmp/f1-d-preflight-final.json`。正式原始批次写入 `artifacts/f1-known-world-following/<batch-id>/`，每个场景一个新目录，已存在目录一律拒绝覆盖。审查通过后，再把摘要、清单和原始文件哈希精简发布到 `evidence/motion_navigation/f1-known-world-following-v1/`；不把世界、普通日志或完整原始轨迹提交到 Git。

最小 smoke 原始批次为 `artifacts/f1-known-world-following/20261004T1636035336397Z-smoke/`。两个真实客户端均连接成功；22 个样本中，目标玩家有 10 tick 实际移动输入，机器人有 9 tick 实际移动输入；跟随层接受修订 2—5，取消后来源释放，三个端口和自有进程全部清理。

该批次的原 `smoke-result.json` 写成通过，但不能作为通过证据。共用分段时间读取器当时把当前 V3 回执硬按 V2 解析，两个客户端的原 `time-report.json` 都失败；smoke 汇总又遗漏了 host 的阻塞检查。原文件保持不变。修正读取器后，只离线重读原分段文件，两端各 30 个观察、29 个区间全部通过，错误为 0，结果保存在 `.tmp/f1-d-time-parser-fixed/`。这次离线重读证明原始时间记录完整，不替代一次修正后重新运行，也不关闭 F1-D。

#### F1-D.1：D058 共享前置修复

`straight_2_0` 原批次保留在 `artifacts/f1-known-world-following/20261004T1655145691341Z-straight-2-0/`。稳定窗超出保持区距离的平均值／P95 为 1.686361／1.886092 格，规划提交／接受修订为 23／17，均未达到冻结门槛。四次 `cancel_braking` 都由普通同高 Walk 的前方空气从 `UNKNOWN` 更新为 `AIR` 后直接作废旧路线引起。详细数字见 [F1 验收第 4—5 节](../acceptance/F1-known-world-following.md#4-straight_2_0-原始结果)。

D058 只允许可精确映射并重放现有两类几何证明的普通同高 Walk 在依赖变化后有界重验。实现已按下面四批完成并分别审查：

1. **最终路线计划与两类查询重放。** RouteAdmitter 完成最终 ActionRoute 后生成 `ActiveRouteValidationPlan`。每个 Walk action 有独立 action index；每条腿绑定 fixed route id、point indices、start/end progress 和实际 ground capability identity；initial connection 单列 retire progress。recipe 用 typed query kind 区分 `SURFACE_EDGE` 与 `STANDABLE_CONNECTION`，分别严格重放现有四点支撑查询和现有 0.1 格采样／endpoint region 查询。共同外围只加入 profile、材质、trait、identity 与 cache，不合并几何语义。不能精确重放的腿没有继续资格。
2. **每条 RouteControl 拥有 tracker。** plan 保存 typed `DependencyOwner` 表；每个 owner 明确 kind、action index、可选 fixed route id 与 recipe ref，provenance 保存 `position -> owner refs`。`ActiveRouteTracker` 按这些字段退役和验证，不解析 owner id，也不从 ActionRoute 重猜。受影响格的所有 owner 都可重验且各自查询通过时才继续；strict／non-recipe 任一命中就停止。每个控制者最多重验 2 条 recipe。成功只替换对应 owner 的依赖引用；缺信息、阻塞、不支持、capability／身份不符和超上限都返回 `STOP`。`RouteControl` 创建时必须同时得到 tracker；不增加全局 registry。
3. **监督者分别处理 incumbent 与 pending。** `ExecutionSupervisor` 在身体推进前按 incumbent、pending 顺序验证，整帧最多重放 4 条 recipe 并共享一个 `WorldQueryCache`。incumbent 失败走原 dependency stop；pending 失败只丢弃 pending。Session 删除直接扫描 active route dependencies 的决定分支，只消费 supervisor 的有类型汇总并路由到现有恢复或重新规划入口。
4. **回归与实机。** 先完成组件、正式链模拟和性能门槛，再从新世界、新目录只运行 `straight_2_0`。它通过后才逐场运行剩余四个场景。旧失败批次和原 `scenario-result.json` 不改写。

组件、正式链模拟和性能部分现已完成。最终相关检查 **226/226**，覆盖 D058 plan／runtime、路线推进、监督中断、闭环、性能工具和 F1-C。正式 v2 性能证据位于 `evidence/motion_navigation/d058-route-revalidation-v2/`，来源提交 `295fd942`，运行前工作树干净。六组重验 P95 为 0.0056—0.3278 ms；完整 F1-C prepare 丢弃前 100 次后保留 3,413 个样本，P95／P99／最大值为 4.5990／7.6267／9.8917 ms，全部通过冻结门槛。v1 原字节保留，但它没有记录项目标准 launcher，只算历史证据。

D058 组件收口时没有启动 Fabric，第 4 批当时只完成了回归与性能。随后已从新世界、新目录单独复跑 `straight_2_0`，结果仍未通过，见 F1-D.2。F1-D 继续打开，也不批准连续运行剩余四场。

首版硬边界如下：

- `WorldKnowledge` 继续报告 `UNKNOWN -> AIR`；
- Step、JumpUp、JumpGap、ControlledDrop、连续高度 Walk、带 traversal plan 的 Walk、非 WALK 模式和无法映射的路线保持原安全停止；
- 无 changed 交集的帧不做几何查询；
- 单 RouteControl 最多重放 2 条 recipe、每帧 incumbent + pending 最多重放 4 条；
- 新增重验段在 100 次预热后的至少 1,000 个受影响样本中报告 P95／P99／最大值，P95 不高于 1 ms；完整控制准备继续满足 P95≤8 ms、P99≤15 ms、名义最大值<30 ms；
- 不调整跟随保持距离、修订阈值、目标速度、投影阈值、恢复预算或规划期限。

测试矩阵必须至少覆盖：

| 组 | 必须证明的行为 |
|---|---|
| 世界事实 | `UNKNOWN -> AIR` 仍进入 `changed_cells`；重复 AIR 不产生新变化 |
| final plan | 最终 Walk→strict→Walk 形成两个 Walk action plan；terminal 替换后 indices/progress 对齐；D057 跳首边后没有旧边 recipe；initial connection 单列并按 connection length 退役；tail 可映射与不可映射分别处理 |
| query kind 与 capability | `SURFACE_EDGE` 逐项等同现有四点 surface edge 查询；`STANDABLE_CONNECTION` 逐项等同现有 0.1 格采样和 endpoint region 查询；plan 绑定实际 GroundMotionProfile／catalog identity；共同外围核对当前材质、trait、identity 和 cache，任一身份或资格变化都 `STOP` |
| 白名单 | 无 traversal plan 的普通 WALK 可重验；Step、JumpUp、JumpGap、ControlledDrop、连续高度、非 WALK 和无法映射路线全部 `STOP` |
| 查询结果 | `FEASIBLE` 刷新依赖并继续；`NEEDS_INFORMATION`、`BLOCKED`、`UNSUPPORTED` 和预算耗尽都失败关闭 |
| provenance 与进度 | typed owner 表包含 owner id、kind、action index、fixed route id、recipe ref；共享格列出全部 owner refs；Walk + strict/non-recipe 共享依赖必须 `STOP`；成功只替换对应 owner 引用；initial connection、已走过 recipe 和前 action 的全部 owner 按 typed 字段退役；不透明 id 证明实现没有解析字符串或重猜 ActionRoute |
| 身份 | `world_session`、`route_id`、`route_revision`、`source_request_id`、`goal_id`、`goal_revision`、`planning_generation`、`work_identity`、`action_index` 逐项错配都 `STOP`；`fixed_route_id` 单列检查并同样 `STOP` |
| 监督者 | incumbent 与 pending 分别验证；pending 失败不刹停合法 incumbent；incumbent 失败仍安全制动；两者失败不重复购买恢复 |
| 真实世界反例 | 用 WorldKnowledge 真正移除支撑、插入 full cube、换成不支持材质；覆盖共享依赖、Walk-Step-Walk、D057 边界和 tail；STOP 帧 incumbent 不再向前，失败 pending 同帧丢弃 |
| 原动作 | 空中动作继续负责落地；严格输入和 continuous-height proof 不借用普通 Walk 快路径 |
| 性能 | 分别报告无交集墙钟、SURFACE_EDGE、STANDABLE_CONNECTION、复杂形状、单 incumbent 和四次重放上限的 P95／P99／最大值；新增段 P95≤1 ms；完整准备 P95≤8/P99≤15/max<30 ms |

新的 `straight_2_0` 批次必须继续满足原 F1-D 全部门槛，并增加针对性检查。仍属于 execution proof 且继续被正式请求的原位置，在观察到达后必须进入 `changed_cells` 并通过重验；selection-only 位置不再由此门槛强制出现，允许保持 `UNKNOWN`。如果它们由其他正式观察来源出现，必须 `UNAFFECTED/NO_INTERSECTION`、零查询，并且不产生 `cancel_braking` 或 task recovery。规划提交／接受修订仍不高于 1.25。距离、修订响应、安全、最终保持、终态、来源释放、time attribution 和进程清理继续按原门槛判断，不能只看停车消失。

F1 初始“十二个核心协调文件零修改”门槛已在 D057 时失败。D058 同样是由 F1 暴露的共享导航缺口，已按职责修改路线接纳、路线控制、执行监督和 Session 消费入口。它不能写成跟随层零改核心，也不能用重新冻结基线抹掉初始失败；从 D058 完成后的提交起，只对后续跟随层差异重新执行零修改限制。

#### F1-D.2：区分站位选择依赖与路线执行依赖

D058 后的新 `straight_2_0` 批次位于：

`artifacts/f1-known-world-following/20261004T2147526847180Z-straight-2-0-d058/`

该批仍未通过。接受修订为 17，规划提交为 22，比例为 1.294118；任务恢复为 4。稳定窗超出 2.5 格保持区的距离平均值为 2.090606 格，P95 为 2.570081 格。修订响应 P95 为 5 tick，最终水平距离为 0.707793 格，安全、终态、来源释放和 host 清理通过。

四次恢复仍与 z=10、11、12、13 的 `UNKNOWN -> AIR` 一一对应。Observation 45、56、62、73 分别在场景 tick 42、53、59、70 写入；supervisor 在下一控制帧 tick 43、54、60、71 消费变化并启动恢复。D058 的运行入口返回了 incumbent `STOP`，所以 Session 沿现有 `active_route_dependency_changed` 路径制动和购买依赖恢复。原批次没有保存 typed validation reason、owner kind 或 query kind，不能把推断写成实机直接证据，也不能预先断定修复后四格都应进入查询重验。

[D059](../decisions/0059-separate-terminal-selection-and-route-execution-dependencies.md)先补永久 typed 帧诊断，再用组件红测确认根因。当前高置信解释是：`standable_point_in_region()` 的整包区域扫描依赖被复制进活动路线，未选站位因没有 exact recipe 而成为 `NON_RECIPE` owner。目标区域中的未选格后来确认是空气，仍会让路线失败关闭。

实施顺序冻结为：

1. `BodyRouteValidation` 绑定 observation sequence，并把 incumbent／pending 的原 `ActiveRouteValidation` 通过 Session diagnostics 携带；`tests/sim/runner.py` 手工新增 `route_validation` JSON 字段，F1-D runner 继续整体 `asdict(diagnostics)`，两者都做 round-trip 检查；
2. 用大 `GoalState` 复现未选 `UNKNOWN` 格变成 `AIR` 时的 typed `NON_RECIPE` 停止，并按 validation plan 的 typed owner/provenance 对 z=10、11、12、13 逐格分类；四次正式链组件重放分别接受真实的新 goal revision 和新 route／revision，各自创建 tracker；
3. 只有已选终点存在 exact `STANDABLE_CONNECTION` proof 时，才从活动执行依赖中移除本次追加的 terminal selection dependencies。此前所有 SurfaceWalkEdge、surface node 和 initial connection 依赖继续保留，再并入 exact proof dependencies；只有确实到达终点的 corridor 做同样处理，validation provenance 保留前腿 owner。没有 exact proof 继续失败关闭；
4. 用真实 RouteAdmitter builder 覆盖 pre-terminal `NON_RECIPE` 与 exact recipe 共享格、exact recipe 与 strict owner 共享格；原 owner 都必须保留并 `STOP`。另覆盖 terminal 与最后图节点重合、ground traversal proof 后另加 tail、第二次 direct query 非 `FEASIBLE` 后 append tail 三个无 exact proof 分支；三者保留完整 selection dependencies，未被既有 recipe 覆盖的部分进入 `NON_RECIPE`，命中时同帧 `STOP`；
5. 检查 `NavigationSession.observation_request()` 和 `WorldKnowledge.set_protection()`：exact support／clearance／sweep／0.1 格采样支撑仍被请求和保护，selection-only 格不再出现；直接断言 corridor 到达与未到终点两种集合公式；
6. 已选终点支撑移除和头部阻塞必须同帧停止；`world_session`、`route_id`、`route_revision`、`source_request_id`、`goal_id`、`goal_revision`、`planning_generation`、`work_identity`、`action_index` 逐项错配都 `STOP`，`fixed_route_id` 单列检查并同样 `STOP`；
7. 组件通过显式合法 `WorldKnowledge` 更新顺序注入四个原变化：selection-only 格应为 `UNAFFECTED/NO_INTERSECTION`、affected 空且零查询；同时属于保留执行 proof 的格应为 `CONTINUE/REVALIDATED`；各格以第 2 步冻结的分类为准。共同要求 0 task recovery、0 `cancel_braking`、同帧 Walk 继续，并保持 D058 查询上限；
8. Fabric 只对仍属于 execution proof 且继续被正式请求的格要求 `changed_cells` 和 `CONTINUE/REVALIDATED`。selection-only 格允许保持 `UNKNOWN` 或不出现；若由其他正式观察来源出现，必须 `UNAFFECTED`、零查询、零恢复；
9. 跑受影响回归、F1-C、性能和独立复审；全部通过后只复跑 `straight_2_0`。

D059-A 至 D059-D 的组件实现现已完成。消费者检查确认 observation request 和 world protection 使用修正后的 effective dependencies；corridor 只在到达终点时加入 exact proof。九个 `ActiveRouteValidationIdentity` 字段和单列的 `fixed_route_id` 已逐项检查，选定支撑、净空和共享 owner 的同帧停止仍保留。复审又补上 RouteControl 的真实 owner 边界：tracker、executor 和可选 coordinator 必须与同一 ActiveRoute 对象一致，旧 executor 不能借新路线生成 validation identity。复审修正后的聚焦组合检查为 **282/282**。

复审后的持久化性能证据位于 `evidence/motion_navigation/d059-route-revalidation-v4/`，来源提交 `6645780`，运行前工作树干净，并把 `route_body_controller.py` 纳入 source identity。六组重验各 1,000 个样本，P95 为 0.0056—0.2951 ms；完整 prepare 保留 3,413 个样本，P95／P99／最大值为 6.3571／10.2462／13.3886 ms，均通过冻结门槛。v3 原字节保留为复审前历史证据。完整 motion_nav 的前次 1,408 项检查仍有 5 项失败：4 项在干净的 `ced67be` 上同样稳定失败，1 项单独复跑通过。本批只登记这些范围外结果，没有顺手修改。

复审整改完成后，已经按 fail-fast 运行一次新的 `straight_2_0`。它仍未通过，结果见 F1-D.3。D059 的 exact-terminal 依赖分离在已覆盖分支内成立，但实机进入了 terminal 等于末图节点的 no-exact 分支。F1-D 继续打开；通过前不运行另外四场。

新 `straight_2_0` 仍沿用 F1-D.1 的完整门槛。任何业务门槛失败都停止，不接着运行 `lateral_2_0`、`move_stop_800_resume_2_0`、`normal_cancel_2_0` 或 `first_input_late_one_tick_2_0`。

#### F1-D.3：终点等于末图节点时补精确证明

D059 修后的新 `straight_2_0` 批次位于：

`artifacts/f1-known-world-following/20261005T0035326046513Z-straight-2-0-d059/`

该批使用 `40cafce8`，启动前工作树干净。接受修订 19 次，规划提交 25 次，比例为 1.315789；任务恢复为 4。稳定窗 excess mean／P95 为 1.887882／2.136090 格。目标实际稳定速度为 1.873496 格／秒，修订响应 P95 为 4 tick，最终距离为 0.741732 格。安全、终态、来源释放、host、时间归属和清理通过。距离、规划比例和恢复三项失败，因此场景未通过，另外四场没有运行。

本批已经持久化 typed validation。四次停车分别发生在 goal revision 9、11、13、15 的四条新路线。Observation 43、53、63、72 写入 z=10、11、12、13 的两格净空后，incumbent 都返回 `STOP/NON_RECIPE_OWNER_CHANGED`，`queries_used=0`，并各购买一次 dependency recovery。正式 observation request 在写入前确实请求了这些格。

原始轨迹直接证明 typed 结果、身份、变化格、请求、停车和恢复。它没有保存完整 validation plan 或单独的 protection 调用参数。用原始 Observation 重建 `WorldKnowledge` 后，正式目标选择和 RouteAdmitter 进一步确认：四个已选终点都等于各自末图节点中心；触发格只来自 `terminal_target.dependencies`，不属于 candidate、末节点、普通 Walk edge 或 initial connection。现有代码因为终点位移为零而跳过 direct exact query。用同一个 `query_standable_connection()` 从最后 Walk 的前一点重放，四组都为 `FEASIBLE`，精确依赖不含触发格。重建使用不同测试身份，只用于确认依赖来源，不冒充实机 route 对象。

[D060](../decisions/0060-prove-terminal-node-execution-without-selection-leakage.md)只补这个分支。普通同高、无 traversal plan 的最终 Walk 至少有两个 fixed-route points 时，即使 terminal 等于末图节点，也用 `fixed_route.points[-2]` 重放现有精确连接查询。只有 `FEASIBLE` 才分别使用 `final walk 原依赖 ∪ exact terminal dependencies` 和 `full corridor 原 prefix 依赖 ∪ exact terminal dependencies`。早期 action、strict action 和其他 Walk 不复制到最后 Walk。ground traversal tail、direct query 非 `FEASIBLE`、无法绑定最后一腿和无法映射的路线继续失败关闭。shared pre-terminal `NON_RECIPE`／strict owner 仍优先停止。

实施顺序冻结为：

1. **D060-A 红测。** 用仓库内合法 fixture 冻结原批的关键参数和 z=10—13 四条独立 route identity，先复现 equality/no-exact 的 `NON_RECIPE` 停止；另覆盖 body connection 后只有单 node、无法形成最后一腿的失败关闭；
2. **D060-B builder。** 只在冻结白名单内以最终 Walk 的 `fixed_route.points[-2]` 调用现有 `query_standable_connection()`，recipe 必须绑定同 action index、fixed route id 和最后 leg；复用 D059 的 exact proof 和依赖来源，不改 planner、目标选择或 tracker；
3. **D060-C consumers／安全。** selection-only 变化应为 `UNAFFECTED/NO_INTERSECTION`、0 query、0 recovery、同帧继续；选定支撑、净空、shared owner 和身份错配仍同帧停止且无正向输入；observation request、protection、final Walk 和 full/prefix corridor 保持各自集合公式；多 action 的早期独占格不复制到最后 Walk，并按原 owner 退役；
4. **D060-D 回归／性能。** 先聚焦，再跑完整 motion_nav。旧 1,408 项集合中的 5 项失败逐项对照；不得把相同旧失败写成全绿，也不得忽略新增或变化的失败。新建 v5 性能证据，保持新增段 P95≤1 ms、完整 prepare P95≤8 ms／P99≤15 ms／max<30 ms；
5. **D060-E 独立复审。** 检查最终 action 映射、保守 no-proof 分支、shared owner、九字段身份、fixed route identity 和性能 source identity；
6. **D060-F 单场实机。** 独立复审通过后，只从新世界、新目录复跑 `straight_2_0`。它满足全部原 F1-D 门槛后，才允许进入另外四场。

D060 不改变 `standable_point_in_region()` 的选择顺序、GoalState、跟随阈值、恢复预算或规划期限。它仍是 F1 暴露的共享导航前置修复，不能写成跟随层零改核心。

### F1-D.4：D061 性能量尺修正

D060-A 至 D060-C 和回归已经完成。聚焦检查 250/250；完整 motion_nav 为 1,417 项，仍是既有 5 项失败，没有新增失败。D060-D 的性能部分没有通过：v5、v6、v8 的 prepare 最大值分别为 34.7251、51.8439 和 48.8184 ms。v8 确认 captured sample 3242 包含一次 36.9770 ms 的 generation 2 扫描。v7 改变了分配和 GC 相位，只算诊断。

[D061](../decisions/0061-separate-independent-scenario-and-long-session-performance.md)把后续性能收口分成两步：

1. 原十个 F1-C 场景逐场完整关闭；场间在 prepare 计时外清缓存并完整 GC，保留全局 warmup 100、场景 ordinal 和原 P95／P99／max 门槛；
2. 在独立子进程里用同一个 task／Session／world 连续运行，先走 `move_stop_800_resume`，再继续到 retained 至少 4,096；若尚未观察 gen2，最多延长到 8,192，使用正式 capacity 2,048 的异步分段 trace。

两条都不得在场内手动 GC、修改 threshold 或扣除 GC 时间。任一失败都停止，不运行 Fabric。两条通过并完成独立复审后，才进入 D060-F 的 `straight_2_0` 实机复跑。

独立场景正式批次 `d061-independent-scenarios-v1` 已从干净提交 `36986a4` 运行一次。十场业务全部通过；完整 prepare 的 P95／P99／最大值为 6.8668／10.9606／14.4872 ms，六组路线重验 P95 为 0.0060—0.5180 ms。

长 Session v1 同样来自 `36986a4`，运行到 8,192 个 retained 样本。旧工具把样本上限误作 gen2 覆盖完成，该结果按量尺缺陷处理。v2 从 `179d30c` 运行一次并退出 1：116 次 gen2 全部在 prepare 外，诊断数组又写满 32,768 个槽位。两批原始目录都不改写。

v3 同时检查 prepare 与正式产品控制路径。产品控制路径从当帧目标更新开始，经过 `RuntimeNavigationDriver.tick()`，到后端接受输入为止；监视器和证据整理不计时。prepare 继续使用 8／15／30 ms 门槛，每个产品控制路径必须小于 50 ms，input deadline miss 为 0，minimum slack 大于 0。retained 窗口内产品路径或 trace 线程自然出现 gen2 后，还要完成至少 128 个正式控制帧。诊断容量至少 65,536；溢出或回调不配对失败关闭。

v3 已从干净提交 `a6d823b` 只运行一次。prepare 和控制路径墙钟、gen2、GC、身份、行为、安全、取消和 trace 结果有效；retained 控制路径 P95／P99／最大值为 11.2050／30.5680／41.2163 ms。但是 v3 的 450 ms slack 来自 500 ms 逻辑 lease，不能判断 50 ms 控制周期是否错过。

v4 用同一对 `perf_counter_ns()` 起止点计算 `slack=50 ms-control duration`；duration 达到 50 ms 就记 miss。控制路径最大值、miss 和 minimum slack 都只用去掉前 100 帧的 retained 数据，captured 与 retained 分开输出。v4 的 COMMAND 保存了实际标准 conda 外层命令。

v4 已从干净提交 `4f194da` 只运行一次。prepare retained P95／P99／最大值为 4.3106／4.4078／5.7910 ms；正式控制路径 retained P95／P99／最大值为 9.9315／25.3232／39.7818 ms，deadline miss 为 0，最小周期余量为 10.2182 ms。首个有效 retained gen2 位于 ordinal 23，随后继续完成至少 128 帧；行为、安全、身份、取消、source 释放和 trace 同时通过。该性能批完成 D060-D，没有运行 Fabric；随后完成的复审和单场结果见下一节。

### F1-D.5：D060-F 失败与 D062

D060-E 独立复审完成后，从干净提交 `6ff9ad1` 只运行一次新的 `straight_2_0`。原始目录为：

`artifacts/f1-known-world-following/20261005T0408072740471Z-straight-2-0-d060/`

目标速度、response P95、stable mean、规划比例、最终保持、安全、终态、来源、host、时间与清理均通过。stable excess P95 为 1.541509 格，超过 1.5 格；task recovery 为 2。因此本批失败，另外四场没有运行。

D060 已消除旧 D059 批次 tick 41、51 的前两次 graph equality 停车。更快前进后出现两条新路径：same-support local direct Walk 没有 validation plan，z=10—12 更新得到 `STOP/PLAN_UNAVAILABLE`；D057 forward-entry 后只剩一个 graph node，已有 initial exact proof 未被复用为 terminal execution，z=13 更新得到 `STOP/NON_RECIPE_OWNER_CHANGED`。正式修正见 [D062](../decisions/0062-unify-direct-walk-validation-proof.md)。

D062 分 A 测试、B RouteAdmitter 私有 builder、C 两入口接入、D 聚焦与 D061 性能、E 独立复审五步实施。local 只通过 `admit_local_direct(typed request, frame, profile, capability identity)` 进入 RouteAdmitter，并建立唯一 `WALK_LEG` owner；Session 不再查询支撑或拼 plan。forward-entry single-node 只保留覆盖完整两点 fixed leg 的 `INITIAL_CONNECTION` owner，退役点等于完整 connection length，不创建 terminal leg／owner。私有 proof context 只在同次 admission 栈内消费。D062 不运行 Fabric。稳定 P95 是独立的冷启动问题：两批都在 tick 12 首次前进，tick 39／40 持续 forward 且没有 recovery。不得在 D062 中改速度、保持距离、稳定窗或 P95 口径。

D062-A 至 D 已完成。专项 6/6、聚焦 219/219；D058／D061 两条性能工具的低成本检查 5/5。完整 motion_nav 共 1,427 项，其中 1,422 项通过，失败仍是此前登记的同 5 项，没有新增失败或签名变化。本批没有重跑 D061 长 Session，也没有生成新的性能证据。实现提交为 `1edb748`；零长度 local goal 和正式 Session 正例经 `deeeec2` 修正后，D062-E 独立复审确认 P0／P1／P2 为零。

### F1-D.6：D063 首次完整请求目标面信息

D060-F 首次目标面查询分两轮返回 missing：先 25 格，再 73 格。两组合计 98 格，原本可以放进 Observation V3 的 128 格上限。额外一轮把首次合法 Walk 推迟到 tick 12，并留下稳定窗开头的追赶尾部。

[D063](../decisions/0063-request-complete-bounded-goal-surface-information.md)只让当前 goal revision 的首次目标面 `NEEDS_INFORMATION` 返回同一查询可确定的完整有界 missing。`support_surfaces` 复用现有候选枚举，不在 Session 写死盒子。UNKNOWN、每帧 128 格预算、请求优先级、节流、分页、信息 owner 和规划图边界全部不变。新 revision 替换 `_snapshot_missing`；旧观察只更新 world，不能复用旧否定或完成新目标查询。

D063 先冻结 Observation V3 的 98 格一次回复、>128 分页、occluded／outside、目标跳变、旧通知和取消红测。产品门槛为首个 Walk≤11 tick、目标面轮次1、首个 Walk 前 planning≤5、全场 20／16 不退、response P95≤5 tick、stable excess P95≤1.5 格。D061 的 control deadline 和 query payload 门槛同时适用。

D062 已完成独立复审，D062 与 D063 之间没有运行 Fabric。D063-A 至 D 已实施：首次返回完整 98 格，超过 128 格时继续沿用分页和请求优先级。补强后的正式 Runtime 回放使用 D060-F 的 6 格起距、66 tick 移动期和 11 tick 目标输入循环；首个 Walk 位于 tick 6，目标面等待一轮，图前沿信息两轮，首个 Walk 前 planning submission 为 5。D063 专项 8/8，D059—D063 证明合同 39/39；完整 motion_nav 1,437 项中 1,432 项通过，仍是此前登记的同 5 项失败。27 个稳定样本的模拟 P95 为 1.534463 格，只作诊断，不替代下一次 Fabric。D063-E 独立复审通过后，才允许一次新的 `straight_2_0` fail-fast。

D063-E 复审先发现回放夹具不等价，`6d8baca` 补强后再次复审，P0／P1／P2 为零。随后按 fail-fast 只运行一次新 `straight_2_0`。目标面 98 格一次返回，但首个后台作业只展开 1 个节点并在约 8 tick 后才交付缺失信息；第一条 Walk 到 tick 14。执行期又在 tick 26、41、65 因新目标缺事实进入退场，事实请求到 tick 29、44、68 才发出。最终 stable excess P95=4.000742，场景失败。

### F1-D.7：D064 普通地面局部直达

[D064](../decisions/0064-use-proved-ground-direct-before-background-planning.md)不再修改信息批大小或跟随门槛。它为普通同高 Walk 增加一个有正式 sweep、支撑和依赖证明的 multi-block direct 候选；候选不适用时仍回落现有后台规划。活动路线收到新目标但缺候选事实时，旧路线继续执行并并行取证，事实到齐后通过现有 Supervisor 交接 successor。严格、空中、高差和带伤害动作保持原路径。

规划入口由 typed `GoalPlanningPolicy` 决定。默认值是 `BACKGROUND_PLANNER`，既有调用方保持原行为；只有任务在开始时显式选择 `PROVED_LOCAL_DIRECT_THEN_BACKGROUND`，Session 才尝试 D064 直达和并行取证。策略归 `GoalRequestLedger` 唯一拥有，同一任务的 revision 不能改变，same-task continuation 和重锚继续携带。`KnownWorldFollowDriver` 只在 `start()` 时声明一次，不增加自己的导航状态或交接逻辑。

D064 A—D 已完成。RouteAdmitter、Session 和 Runtime 的首批实现提交为 `da9787d`、`72920ae`、`f71a36c`。`ffbe30a` 补齐中间支撑材质检查；`b6894a9` 收口 owned planning、direct route、pending revision 和局部尝试链的生命周期；`546e6e6` 加强三轮正式跟随检查。最终聚焦集合 140/140。最近一次完整 motion_nav 仍是 typed policy 修正后的 1,442/1,447，失败为此前登记的同 5 项；生命周期补强后没有重跑完整集合，不能把聚焦结果写成新的完整通过。

干净提交 `546e6e6` 的 direct query 预热 100 次后记录 1,000 次，P95／最大值为 1.6753／1.6999 ms。D061 冻结短参数回归的 prepare P95／P99／最大值为 3.9066／3.9725／3.9725 ms；正式控制路径 P95／P99／最大值为 21.5256／23.2795／23.2795 ms，deadline miss 为 0，最小余量为 26.7205 ms。两份公开记录分别位于 `evidence/motion_navigation/d064-direct-v1/` 和 `evidence/motion_navigation/d064-control-short-v1/`。D061 短跑 18 项 gate 全部通过，但只证明本轮没有立即打破性能边界；长期结论仍引用 D061 v4。一次更短的 16-frame 诊断因没有 retained gen2 整体退出 1，失败摘要原样保留。

独立复审确认没有未关闭的 P0／P1／P2。随后只运行一次新的 `straight_2_0`，原始目录为：

`artifacts/f1-known-world-following/20261005T2003536561761Z-straight-2-0-d064/`

该批第一条 Walk 位于 tick 4；全场 `STOPPING=0`，保持区外中性输入空档为 0，task recovery 为 0。planning submission／accepted revision 为 1／11，revision response P95 为 3 tick，payload 最大 98 格。稳定窗 excess lag 的 mean／P95／max 都是 0；目标均速为 1.822656 格／秒。安全、终态、source 释放、host、time attribution 和 cleanup 全部通过。

D063 后的失败批次继续保留。`straight_2_0` 通过后，又从新世界、新目录单独运行了 `lateral_2_0`：

`artifacts/f1-known-world-following/20261005T2021024037283Z-lateral-2-0-d064/`

该批第一条 Walk 位于 tick 4，目标均速为 1.873410 格／秒。稳定窗 excess lag 的 mean／P95／max 为 0.133949／0.513506／0.527817 格；revision response P95 为 2 tick；planning submission／accepted revision 为 4／13。task recovery 和 `STOPPING` 都是 0，payload 最大 98 格。安全、终态、source 释放、host、time attribution 和 cleanup 全部通过；分段证据与 SHA 已复核。

轨迹在 tick 34—47 有一段中性输入。这里旧路线已经正常完成，目标当时仍满足旧 revision，或新的 revision 尚未产生；rev9 到达后，下一 tick 就恢复移动。因此这段不属于取证期间丢失 incumbent，也没有形成 recovery 或 `STOPPING`。

`lateral_2_0` 通过后，只运行了 `move_stop_800_resume_2_0`：

`artifacts/f1-known-world-following/20261005T2031097906144Z-move-stop-800-resume-2-0/`

pause 的 tick 33—832 全程正常，terminal、`STOPPING` 和 task recovery 都是 0。tick 853 接受 rev12 后，Session 在同一帧进入 `FAILED/same_support_local_path_unavailable`。当时已经得到 42 个 missing，但失败发生得太早，这些格子没有获得 Observation 请求机会；rev12 最终记为 unanswered，随后 source 安全释放。该批是 P1 产品缺陷，不是夹具失败。

根因是目标已满足并进入待命后已经没有 incumbent。目标重新移出保持区且目标面缺信息时，missing 分支复用了 ordinary direct helper；该 helper 为 initial/direct 资格保留了 `incumbent=None` 的合法语义，却被这一特殊分支误读成“旧 Walk 可以继续”。同帧旧 `SAME_SUPPORT` request 随后使用新 goal state，最终错误失败。

最小修复只在这个特殊 missing 分支显式要求 `incumbent_route` 非空，不改变 initial/direct 资格。Session 公共行为测试在旧逻辑上稳定红，修复后转绿。新增正式 `Runtime → Driver → Session → Follow` 的 33+800+33 partial-observation 回归；旧逻辑在恢复修订上没有进入 typed `NEEDS_INFORMATION`，修复后会等待信息，本场 missing 集合为 14 格、unanswered 为 0，并最终正常 cancel 和释放 source。请求不超过 128 格的通用门槛继续由 D063／D064 专项覆盖。

当前 D064 聚焦检查 20/20。实施 agent 先前运行 F1 文件 8/8；根代理补强断言后，正式链与 D064 合计 20/20。只有修复形成提交后，才允许从新世界、新目录重跑 `move_stop_800_resume_2_0`。`normal_cancel_2_0` 和 `first_input_late_one_tick_2_0` 继续未批准。

第一次失败和上面的修复记录保持不变。提交 `3999d36` 修正了 `goal_node=None` 时相邻路径误用旧 `SAME_SUPPORT` request 的问题。随后只从新世界、新目录复跑本场，原始目录为：

`artifacts/f1-known-world-following/20261005T2117132926377Z-move-stop-800-resume-2-0-fix/`

第二批的 pause 仍然健康。tick 852 接受 rev11 后，Session 同帧进入 `FAILED/same_support_local_path_unavailable`；当时仍有 42 个 residual missing，rev11 最终记为 unanswered，source 随后安全释放。批次早期出现过 8 次 `needs_target_observation`，都发生在近距离目标区域重叠期间，并且有界结束，不是最终失败原因。

这次实机没有进入 `goal_node=None` 分支。系统已经选出 goal node，随后进入 `SAME_SUPPORT` local 接纳。精确重建得到 typed reason `CURRENT_BODY_CANNOT_CONNECT`：local 接纳把大 `GoalState` 区域中心硬当作终点；中心已经落在当前 surface 外，但目标区域边缘仍有符合支撑、净空和连接规则的合法站位。

最终修复先调用带 `connection_from=body` 的 standable selector，在目标区域和当前 surface 的交集中选择身体可连接的安全终点。选中后再用正式 connection query 精确重放。只有这次精确重放得到的 dependencies 才进入材质检查、action、validation recipe 和 provenance；selector 扫描过但未选中的依赖不会冒充执行证明。任何 selector 或精确重放失败仍沿原 typed 出口关闭，没有增加后台 fallback，也没有放宽 D059 的“站位选择依赖与路线执行依赖分开”边界。

修后检查为 D062 13/13、D064 19/19、D059／D060 23/23，以及 F1 800 tick 正式链回归 1/1。local direct 性能 P95 为 0.7115 ms，最大值为 0.8471 ms。独立复审确认 P0／P1／P2 为零。该修复形成提交后，只批准从新世界、新目录重跑 `move_stop_800_resume_2_0`；`normal_cancel_2_0` 和 `first_input_late_one_tick_2_0` 仍未批准。

### F1-D.8：第三次失败与 local admission 只读诊断

endpoint 修复提交后，只从新世界、新目录再次运行 `move_stop_800_resume_2_0`。前两次失败记录保持不变。第三批原始目录为：

`artifacts/f1-known-world-following/20261005T2205268396025Z-move-stop-800-resume-2-0-endpoint-fix/`

来源为 `12a2f3a`。pause 仍然健康。tick 851 接受 rev12 后，Session 同帧进入 `FAILED/same_support_local_path_unavailable`。当时 request generation 为 14，request 为 14，仍有 42 个 residual missing；route、incumbent、action 和 route validation 全部为空。rev12 最终记为 unanswered，source 随后安全释放。

本批证明 endpoint 修复已经进入源码，但没有生成 local route。现有 `same_support_local_path_unavailable` 只能说明 local 接纳失败，无法区分失败发生在 `BODY_SURFACE`、`SURFACE_IDENTITY`、`GOAL_SELECTION`、`EXACT_CONNECTION`、`MATERIAL_CAPABILITY` 还是 `ROUTE_BUILD`。因此这次结果不支持继续猜测几何、材质或路线构造中的某一个分支。

为下一次单场运行补充只读 `LocalDirectAdmissionEvidence`。证据使用 typed phase：`REQUEST`、`BODY_SURFACE`、`SURFACE_IDENTITY`、`GOAL_SELECTION`、`EXACT_CONNECTION`、`MATERIAL_CAPABILITY` 和 `ROUTE_BUILD`。每次 local 尝试只保存以下有界事实：request id、goal revision、start／goal node、goal region、body position、当前 phase 及其 `QueryStatus`、最终 `AdmissionStatus`／`AdmissionReason`、实际 body surface node、selected position、selector／exact status、missing 数量和 exact dependency 数量。

Session 只保存最后一次 local 尝试。新尝试原子替换旧证据；新的非 local request 会清理旧证据，避免跨 revision 误读。`NavigationDiagnostics` 只读暴露该值，Fabric sample 通过 `asdict(diagnostics)` 写入；每行同时保存 `KnownWorldFollowResult.submitted_target_position`。模拟器只读取该诊断，不改变控制。任何接纳、授权、失败分类和状态转换都不得读取这份证据。

当前聚焦检查 34/34，`git diff --check` 通过；独立复审确认 P0／P1／P2 为零。该诊断形成提交后，只批准从新世界、新目录重跑 `move_stop_800_resume_2_0`，用于取得真实失败 phase 和 typed 字段。当前不能声称产品缺陷已修复。`normal_cancel_2_0` 和 `first_input_late_one_tick_2_0` 继续冻结。

### D065：修正不可测的恢复移动窗口

typed 诊断后的新目录为：

`artifacts/f1-known-world-following/20261005T2300158044696Z-move-stop-800-resume-2-0-typed-evidence/`

本批保存了 986 个逐帧样本；整体距离统计另含起始点，共 987 项。16 个修订全部生效，响应 P95 为 3 tick；planning submission／accepted revision 为 3／16，task recovery、`STOPPING` 和安全违规均为 0，最终距离为 0.963785 格，任务正常取消并释放输入源。tick 856—859 的 typed evidence 为 `ROUTE_BUILD / ACCEPTED / CANDIDATE_ADMITTED`，selector 和 exact query 均为 `FEASIBLE`，证明 endpoint 修复已经命中正式 local route。

该批仍不能判为通过。原场景首段和恢复段都只有 33 tick，而稳定量尺跳过每段开头 40 tick，所以稳定速度和距离样本必然都是 0。D065 不修改量尺和阈值，只把恢复段延长到 66 tick，并增加预检，固定要求本场产生 27 个稳定样本。旧目录及 `passed=false` 保持不变；形成干净提交后，只重跑修订后的本场。

### D066：持续目标收尾后恢复活动待命

D065 修订后的实机目录为：

`artifacts/f1-known-world-following/20261005T1525428380773Z-move-stop-800-resume-2-0-d065/`

原有 gate 全部通过：稳定样本 27，目标均速 1.815892 格／秒，stable excess mean／P95 为 0.109873／0.511040 格；20 个修订全部生效，响应 P95 为 4 tick；planning submission／accepted revision 为 2／20，task recovery 和安全违规均为 0，最终距离为 0.979653 格，终态、source、host、time、cleanup 和分段哈希均通过。

独立逐帧复核没有放行该批。tick 919—924 的 `STOPPING` 是仍有路线和 `RETAIN` 证据的合法制动；tick 925 起 handoff 已为 `QUIESCENT`，路线、控制者、身体活动和等待均为空，但 Session 到 tick 1018 仍保持 `STOPPING / goal_state_satisfied`，共 94 帧。

D066 增加专用 typed transition，只允许当前帧 `QUIESCENT` 证据把满足的持续任务从 `STOPPING` 带回 `EXECUTING` 活动待命。Fabric 摘要同步增加 `satisfied_idle_lifecycle` gate。正式链红测在修复前复现，修复后通过；相邻生命周期、持续目标、F1 摘要和正式链检查共 51/51。完整 motion_nav 1,466/1,471，失败仍是第 12.5 节冻结的同 5 项。独立复审没有未关闭的 P0／P1／P2。当前尚未再次运行 Fabric。

### D067：pending goal 阻止旧局部入口激活

D066 后只重跑本场，原始目录为：

`artifacts/f1-known-world-following/20261005T1607326063433Z-move-stop-800-resume-2-0-d066/`

场景在 tick 852 接受 rev11 后进入 `FAILED/same_support_local_path_unavailable`。当时 pending revision 为 11，仍缺 42 个格子，typed local evidence 为 `GOAL_SELECTION / BLOCKED / CURRENT_BODY_CANNOT_CONNECT`；rev11 unanswered，稳定窗口尚未开始。原 `scenario-result.json` 保持 `passed=false`。这次没有再次出现 D066 修正的活动待命状态缺口。

根因是 `update_goal()` 已把 rev11 交给 pending owner 等待事实，但旧 revision 的 local activation id 仍在。同一 `propose()` 随后误用“旧节点 + 新 GoalState”执行 local admission。D067 在 local 和 ground-direct 的共同激活位置统一要求没有 pending goal。它不清除 incumbent，不新增重绑路径；事实到达后仍由 `_resume_pending_goal()` 从当前观察重算节点。

新增公共时间线测试分别保留旧 local id 和 ground-direct id。缺事实的同帧两个 admitter 都必须零调用，并产生正式 Observation 请求；补齐请求事实的下一帧，pending 清除，目标节点按当前目标重算。相关集合 86/86；独立复审没有未关闭的 P0／P1／P2。

修复后的实机目录为：

`artifacts/f1-known-world-following/20261005T1647399858283Z-move-stop-800-resume-2-0-d067/`

来源提交为 `45eb73f`。tick 852 的 rev11 保持 `NEEDS_INFORMATION`，请求 42 个事实且没有 route／owner／非中性输入；tick 853 事实到齐后才接纳 `request-13-direct`。20 个修订全部生效，响应 P95 为 4 tick，规划比为 4/20；稳定速度和距离、最终保持、终态、source、host、time、cleanup 全部通过，恢复和安全违规为 0。独立复审重算 1,019 帧和 11 组分段哈希，没有未关闭的 P0／P1／P2。该场正式通过，只批准下一场 `normal_cancel_2_0`。

### `normal_cancel_2_0` 实机结果

本场从新世界、新目录运行：

`artifacts/f1-known-world-following/20261005T1701295216057Z-normal-cancel-2-0/`

来源提交为 `ecd8b4f`。tick 44 接受取消并进入 `STOPPING/cancel_braking`。tick 44 的反向输入由当前 request 10 route owner 发出，用于合法制动；tick 45—46 仍保留同一身体责任但输入已经中性，tick 47 清除路线、控制者和身体活动，进入 `CANCELLED`。从请求取消到终态为 3 个控制帧，`cancel_tail_ticks=0`，task recovery 和安全违规均为 0。

本场按冻结口径只判取消、安全、来源释放和有界终态；距离、速度、规划和响应继续记录，但不参与通过判定。独立复审重算连续 48 帧和 11 组分段哈希，确认 Session／driver 取消、source 释放、host／time／cleanup 全部通过，没有未关闭的 P0／P1／P2。该场正式通过，只批准最后一场 `first_input_late_one_tick_2_0`。

### D068：晚一 tick 必须落在正式输入窗口内

第一次运行最后一场时，首个移动命令实际晚一 tick 生效，但输入账本只允许精确 tick，状态应为 `APPLIED_OUTSIDE_WINDOW`。runner 只比较相对偏移，错误写成 `passed=true`。原目录保持不变，不作为通过证据。

D068 只为静止起步的两点直线普通 Walk 产生 `[t+1, t+2]` typed 窗口。拐角、带速、制动、Crouch、Sprint、连续高度、严格动作和空中动作仍使用原来的精确规则。Runtime 必须通过获胜移动意图取得窗口，并由真实输入账本确认实际只在 `t+2` 生效。

相关检查 111/111 通过；独立复审没有未关闭的 P0／P1／P2。详细边界见 [D068](../decisions/0068-bind-ordinary-walk-late-start-to-ledger-window.md)。

新 Fabric 目录为：

`artifacts/f1-known-world-following/20261005T1754012106271Z-first-input-late-one-tick-2-0-d068/`

来源提交为 `cf0aa13`。正式账本记录 requested first／last=9、latest=10、actual=`[10]`、status=`APPLIED`、`valid_for_ticks=1`。稳定样本 27，响应 P95 为 3 tick，规划比为 1/11，恢复和安全违规为 0。目标满足后保持 `EXECUTING/QUIESCENT` 中性待命 126 帧；终态、source、host、time、cleanup 和 11 条分段流哈希全部通过独立复核。最后一场正式签署，F1-D 关闭；下一步进入 F1-E。

### F1-E：结构与产品验收

检查正式跟随差异：

- 跟随生产层的行数、状态数和决定分支；
- 十二个核心协调文件零修改；
- 没有新的跟随专用生命周期或恢复账本；
- 现有运动导航完整检查通过；
- 受影响的 Runtime、输入仲裁和目标修订检查通过。

原 R28 的五 tick 修订响应门槛转入本阶段。未达到时如实保留，不能用平均跟随距离掩盖。

实际结果见 [D069](../decisions/0069-accept-follow-product-and-retain-structure-failure.md)：

- 五个代表 Fabric 场景 5/5 通过，修订响应 P95 均不超过 4 tick；
- 薄跟随层只调用 `start`、`replace_goal` 和 `release`，没有保存导航内部状态；
- 十二个核心文件没有跟随专用状态或分支；
- 原零修改门槛失败：F1 开始后 6 个核心文件净增 2,399 行，D058 后仍净增 1,294 行；
- 完整运动导航 1,469/1,474，仍是冻结的同 5 项失败，没有新增失败。

因此 F1 功能交付通过，结构门槛未通过。精简证据见 `evidence/motion_navigation/f1-known-world-following-v1/`。后续按[独立结构整理计划](post-F1-navigation-structure-cleanup-plan.md)推进，不在 F1 中继续追加能力。

## 5. 完成条件

F1 只有同时满足下面条件才完成：

1. 正式跟随层只管理目标身份、目标修订和取消；
2. 十二个核心协调文件没有变化；
3. 已知开阔地的模拟固定清单无安全违规和无界等待；
4. 目标满足后可长时间待命，目标再移动时继续同一任务；
5. 代表性 Fabric 场景结果与原始记录一致；
6. 修订响应、跟随距离、规划频率和失败边界有完整数字；
7. 旧跟随实现没有进入正式入口；
8. 失败和不支持场景继续留在分母和证据中。

当前第 2 项没有满足，因此 F1 整体保持“功能通过、结构未通过”。行为不变的删除与归位已经转入独立结构整理计划。室内终点接近另开阶段，不能混进 F1。
