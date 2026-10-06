# D064：普通地面先尝试有证明的局部直达

日期：2026-10-05。状态：A—E 已完成；独立复审没有 P0／P1／P2，D064 后的新 `straight_2_0` 已通过。D063 后的实机失败原样保留。F1-D 其余场景仍未完成，下一场只允许单独运行 `lateral_2_0`。本决定处理普通同高地面的首次行动延迟和目标修订停车，不修改跟随策略、距离门槛、A*、严格动作或空中动作。

## 1. 要解决的问题

D063 后的新实机目录为：

`artifacts/f1-known-world-following/20261005T1630101063860Z-straight-2-0-d063/`

目标面 98 格在 Observation 2 请求，并在 Observation 3 一次回答。失败发生在后面两段：

1. rev1 在场景 tick 1 提交后台规划。目标在 tick 4 更新到 rev2；首个规划结果到 tick 9 才返回，而且只是“还缺 6 格”。tick 12 又得到 33 格缺失，tick 14 才发送第一条 Walk。
2. 执行期间，tick 26、41、65 的目标修订缺少新目标面事实。现行代码先让旧路线退场，到 tick 29、44、68 才请求各 28 格。稳定窗口内因此多次停止。

首个后台结果只展开 1 个节点。结合 Windows `spawn` 和进程队列，长等待更可能来自首作业冷启动、进程通信和旧 revision 排队。当前没有 worker 内部时间戳，所以这项只作有源码支持的推断。无论具体耗时来自哪里，普通开阔地的第一步都不应依赖这次后台往返。

本批 target 实际应用 30 个输入 tick，与 D060 相同。均速 1.773723 格／秒比门槛下界少 0.026277 格／秒，属于独立的夹具有效性失败。稳定 excess P95 为 4.000742 格，比 1.5 门槛高 2.500742 格。即使把两批目标总位移约 0.586 格的差异全部计入，也解释不了稳定距离退步。主问题仍是首次等待和三次串行退场。

## 2. 准备怎么做

### 2.1 在 RouteAdmitter 建立 multi-block direct Walk

新增一个有类型的普通地面直达入口。它只接受：

- 当前身体在地面，模式为 Walk，姿态为 standing；
- 起点和目标支撑面同高；
- 目标允许 Walk 和 standing；
- 当前点到目标候选点不超过既有 `maximum_corridor_blocks`；
- 世界、profile、能力和请求身份匹配。

入口继续使用现有 `standable_point_in_region()` 和 `query_standable_connection()`。后者会检查整段碰撞、每 0.1 格的地面支撑、材质能力和所有依赖格。不得另写简化碰撞或把 UNKNOWN 当作空气。

结果只有三类：

- `ACCEPTED`：生成正式 `WalkSegment`、`WALK_LEG` owner、`STANDABLE_CONNECTION` recipe、validation plan 和依赖来源；
- `NEEDS_INFORMATION`：返回这一个直达候选真正缺少的有界格子；
- `NOT_APPLICABLE`：已知阻挡、高差、严格动作、距离超限或能力不支持，交给现有后台规划。

身份失配和世界会话错误仍是安全拒绝，不能伪装成普通回落。

### 2.2 初始目标先走局部直达

首次目标面和当前支撑确定后，先调用这个入口：

- 已证明可行时，直接安装普通 Walk，不等待 PlannerWorker；
- 缺信息时，沿用现有 Observation V3 请求、128 格分页和五 tick 节流；
- 不适用时，才提交现有后台规划。

这不是一条不受验证的临时路线。它与后台候选一样拥有正式依赖、重验和执行责任。

### 2.3 活动路线取证时不先停车

普通 Walk 正在执行时，如果新目标的直达候选缺信息：

- 当前路线继续作为 incumbent；
- 同一帧登记新 goal revision 和 missing，并发出观察请求；
- 事实到齐后建立 successor direct route，交给现有 `ExecutionSupervisor.offer_route()`；
- successor 的移动输入实际赢得仲裁后，才以现有 `TRANSFERABLE` 证据替换 incumbent。

等待期间不能进入 `STOPPING`，不能清空仍有效的旧路线，也不能购买 recovery。若旧路线先耗尽，仍按现有安全收尾停下；不能为了不断步越过已经证明的走廊。

空中动作、严格动作、带伤害动作、高差动作和要求安全交接的控制者保持原规则。它们不会因为本决定获得移动中替换权限。

### 2.4 与后台规划的关系

局部直达是快速、短程、有证明的普通地面候选。后台规划继续负责：

- 绕障碍；
- 高差、跨隙和其他动作；
- 超出局部长度的目标；
- 直达候选已知不可行的情况。

本轮不优化 A*，不新增 corridor 预取器，也不让 FollowDriver 读取导航内部状态。入口只看 `GoalState`、身体、profile、支撑面、世界事实和正式身份；不得按 task id、技能类型或 reason 字符串分支。

### 2.5 用任务级 typed policy 选择规划入口

局部直达不能根据目标大小、保持策略或调用方名字自动开启。`GoalPlanningPolicy` 明确声明任务使用哪条规划入口：

- 默认 `BACKGROUND_PLANNER`，所有既有调用方继续走原后台规划；
- `PROVED_LOCAL_DIRECT_THEN_BACKGROUND` 先尝试本决定的有证明直达，不能应用时再回后台；
- `GoalRequestLedger` 是策略的唯一拥有者。同一任务内的 goal revision 不得改变策略；same-task continuation 和重锚必须继承原值；
- `KnownWorldFollowDriver` 只在 `start()` 时声明 direct-first。它不读取 Session 私有状态，也不新增跟随专用交接分支。

`NavigationSession` 只读取这份 typed policy。它不得根据 reach policy、goal 几何、task id 或 reason 字符串推断权限。

## 3. 实施顺序

### A. 先写失败检查

1. 初始 6 格同高目标，目标面事实一次到齐；PlannerWorker 固定延迟 8 tick，机器人仍在 11 tick 内开始 Walk。
2. 活动普通 Walk 连续三次目标修订，每次先缺目标面事实；旧路线保持前进，0 次 `STOPPING`、0 个无输入空档、0 recovery。
3. missing 到齐后 successor 通过正式 Supervisor 交接，旧 owner 只在 successor 输入获胜后退役。
4. 支撑移除、净空阻塞、世界／profile／能力／九字段身份错配不能前进。
5. 高差、strict、空中、距离超限和已知直达阻挡继续走后台或原安全等待。

### B. RouteAdmitter

先把 same-support local builder 中的正式证明构造复用为私有 helper，再增加 multi-block typed 入口。只迁移已经重复的构造，不新建插件层。

### C. Session 与 Supervisor 接线

初始目标和活动目标修订共用同一个 direct admission 结果。活动路线缺信息时，只保存新目标和 missing；不发停止请求。事实到齐后尝试 successor，不能复制一套交接状态机。

### D. 直接检查与性能

先跑 D064 专项，再跑 D062／D063、目标修订、Supervisor 和 F1 正式链。专项稳定后才扩大完整 motion_nav。测量 direct query 和完整 prepare：

- direct query P95≤2 ms、max<8 ms；
- prepare P95≤8 ms、P99≤15 ms、max<30 ms；
- 正式控制路径 max<50 ms、deadline miss=0、minimum slack>0；
- 每帧空气请求≤128。

### E. 独立复审与单场实机

独立复审 P0／P1／P2 清零后，只运行一个新世界、新目录的 `straight_2_0`。它必须同时满足：

- 首个实际 Walk≤11 tick；
- 首个 Walk 前后台 planning submission≤1；
- 活动目标修订引起的 `STOPPING` 为 0；
- 目标面事实等待不产生无输入空档；
- task recovery=0；
- planning／accepted ratio≤1.25；
- revision response P95≤5 tick；
- stable excess mean≤0.75 格、P95≤1.5 格；
- 原安全、终态、source、host、time 和 cleanup 门槛全部通过。

target speed 仍按原 2.0±0.2 格／秒判断。若夹具再次单独失败，必须保留该批并先处理夹具有效性；不能用它放宽跟随距离。

## 4. A—E 实施与验收结果

实现和复审补强分成六个提交：

- `da9787d`：`RouteAdmitter` 增加普通同高 multi-block direct Walk，并复用正式 `STANDABLE_CONNECTION` recipe、`WALK_LEG` owner、依赖与身份校验；
- `72920ae`：Session 接入初始直达和活动 Walk 的并行取证、successor offer 与现有交接；
- `f71a36c`：增加 `GoalPlanningPolicy`，恢复所有默认调用方的后台规划语义，只让显式声明的任务启用 direct-first；
- `ffbe30a`：检查 direct corridor 中每一个已知支撑格的材质能力；
- `b6894a9`：owned planning 只有在本 revision 符合普通同高 direct 资格时才退役；非规划路线接纳后重置局部尝试链；旧 incumbent 先完成时保留新 revision 的 missing；direct route 不继承已退休 work identity；
- `546e6e6`：正式 Following Runtime 连续三轮目标面缺事实，整个场景 0 `STOPPING`、等待期逐 tick 0 中性输入空档、0 recovery、0 planning submission。

最终聚焦集合为 140/140。最近一次完整 motion_nav 是 typed policy 修正后的 1,442/1,447，失败仍是此前登记的同 5 项；本次生命周期补强后没有重跑完整集合，因此这里只签署聚焦结果。

最终性能来源是干净提交 `546e6e6`。direct query 本地预热 100 次、测量 1,000 次，P95／最大值为 1.6753／1.6999 ms，满足 P95≤2 ms、max<8 ms。D061 冻结短参数回归的 prepare P95／P99／最大值为 3.9066／3.9725／3.9725 ms；正式控制路径 P95／P99／最大值为 21.5256／23.2795／23.2795 ms，deadline miss 为 0，最小余量为 26.7205 ms，18 项 gate 全部通过。公开证据分别位于 `evidence/motion_navigation/d064-direct-v1/` 和 `evidence/motion_navigation/d064-control-short-v1/`。短参数结果只用于发现立即回归，不替代 D061 v4 长 Session。一次 16-frame 过短诊断没有 retained gen2，整体退出 1；失败摘要保留，不能冒充通过。

A—D 收口后，独立复审确认没有未关闭的 P0／P1／P2。随后按 fail-fast 只运行一次新的 `straight_2_0`：

`artifacts/f1-known-world-following/20261005T2003536561761Z-straight-2-0-d064/`

该批第一条 Walk 位于 tick 4。全场 `STOPPING=0`，保持区外的中性输入空档为 0，task recovery 为 0。planning submission／accepted revision 为 1／11，revision response P95 为 3 tick，Observation payload 最大 98 格。稳定窗 excess lag 的 mean／P95／max 都是 0；目标实际均速为 1.822656 格／秒。安全、终态、source 释放、host、time attribution 和 cleanup 全部通过。

D063 后的失败批次继续保留，不用本次通过结果改写。D064 的冻结目标已经完成，但 F1-D 还没有关闭。下一步只允许从新世界、新目录运行 `lateral_2_0`；不得连续运行后续场景。

## 5. 停止条件

- 专项出现安全退步，停止；
- direct query 或 prepare 超过门槛，停止并先定位；
- 独立复审仍有 P0／P1／P2，停止；
- 新 `straight_2_0` 任一门槛失败，保留原始结果并停止，不运行其余四场。

## 6. 不在本轮做

- 不修改跟随距离、稳定窗口或目标速度阈值；
- 不优化 A* 或 PlannerWorker 进程；
- 不加入疾跑、转身、路径平滑或新的移动能力；
- 不处理未知探索；
- 不把 multi-block direct 扩展到高差、空中或伤害动作。
