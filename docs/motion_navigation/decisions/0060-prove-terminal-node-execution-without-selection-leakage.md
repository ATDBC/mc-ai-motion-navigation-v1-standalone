# D060：终点等于末图节点时仍建立精确执行证明

日期：2026-10-05。状态：D060-A 至 D060-E 已完成；[D061](0061-separate-independent-scenario-and-long-session-performance.md)两条性能门槛均已通过。D060-F `straight_2_0` 已运行但未通过：D060 覆盖的 equality graph route 生效，连续前进后暴露 local direct Walk 和 forward-entry single-node 两条共享 proof 缺口，转入 [D062](0062-unify-direct-walk-validation-proof.md)。原失败证据不改写。

## 要解决的问题

D059 已经把“在目标区域里选择站位所需的依赖”和“走向已选站位所需的依赖”分开，但它有一个没有覆盖的分支：已选终点正好等于规划路径的最后一个图节点时，`RouteAdmitter` 不再调用 `query_standable_connection()`。这条路线因此没有 terminal `STANDABLE_CONNECTION` 精确证明。

`standable_point_in_region()` 会按顺序尝试多个站位。一个候选点因为前方净空未知而失败后，函数仍会把该候选的依赖保留在最终结果中。它随后可能在末图节点中心找到合法站位。现行代码看到“终点没有位移”，就把这整包选择依赖并入最后一个 Walk。没有 recipe 的部分成为 `NON_RECIPE` owner。

D059 修后的正式 `straight_2_0` 批次暴露了这个分支：

`artifacts/f1-known-world-following/20261005T0035326046513Z-straight-2-0-d059/`

运行源码为 `40cafce8a0f747279aacaa24e41976d36bd1d63d`，启动前工作树干净。场景接受 19 次修订，提交 25 次规划，比例为 1.315789；任务恢复为 4。稳定窗超出 2.5 格保持区的距离平均值／P95 为 1.887882／2.136090 格。修订响应 P95 为 4 tick，最终距离为 0.741732 格，安全、终态、来源释放、host、时间归属和清理均通过。距离、规划比例和恢复门槛未通过，所以本批失败，其余四个场景没有运行。

## 证据边界

原始轨迹直接记录了四次 incumbent 重验结果：

| 控制帧／Observation sequence | route／goal revision | 变化格 | typed 结果 |
|---|---|---|---|
| tick 41／43 | `ed37db6656912f0f70c7da2e`／9 | `(0,-60,10)`、`(0,-59,10)` | `STOP/NON_RECIPE_OWNER_CHANGED`，0 query |
| tick 51／53 | `dc9318ad75bd3279786d03e1`／11 | `(0,-60,11)`、`(0,-59,11)` | 同上 |
| tick 61／63 | `39bfcd602098f8e0eb249741`／13 | `(0,-60,12)`、`(0,-59,12)` | 同上 |
| tick 70／72 | `9c43371c24ea81b1613fcaf9`／15 | `(0,-60,13)`、`(0,-59,13)` | 同上 |

每次 `STOP` 都在同一帧进入 `cancel_braking`，并只增加一次 dependency recovery。正式 observation request 在变化写入前主动请求了对应列。原始证据没有保存完整的 validation plan，也没有单独保存 `WorldKnowledge.set_protection()` 的调用参数。因此，owner 的来源不能只靠实机 JSON 猜测。

离线分析把该批原始 Observation 按顺序写入真实 `WorldKnowledge`，再使用正式目标选择、普通地面 profile、surface route 构建和 `RouteAdmitter` 重建相同几何。它确认：

- 四次目标中心 z 分别为 9.926195、10.821322、11.803690、12.797093；
- 末图节点中心分别为 z=9.5、10.5、11.5、12.5；已选终点与末图节点完全相等；
- z=10、11、12、13 的两格净空属于 `terminal_target.dependencies`；
- 它们不属于 planner candidate、末节点、`SURFACE_EDGE` 或 D057 initial connection 的依赖；
- validation provenance 中只有 `NON_RECIPE` owner；
- 从最后 Walk 的前一可执行点到已选节点中心重放现有 `query_standable_connection()`，四次都为 `FEASIBLE`，精确依赖均不包含触发格。

离线重建使用不同的测试请求和工作身份，不能把重建 route hash 当作实机直接证据。它用于确认依赖来源和现有查询结果。D060 的组件测试必须把这个结论转成仓库内可重复的正式检查，不能依赖 `.tmp` 文件。

## 决定

只补普通同高 Walk 的一个证明缺口。满足下面全部条件时，即使已选终点等于末图节点，也要调用现有 `query_standable_connection()`：

- 最后一个 action 是普通 `WalkSegment`；
- 所有相关点同高；
- `traversal_plan is None`；
- 模式为普通 `MovementMode.WALK`；
- 最终 `WalkSegment.fixed_route.points` 至少有两个点；
- 已选终点等于 `fixed_route.points[-1]`，前一点机械定义为 `fixed_route.points[-2]`；
- terminal recipe 能绑定同一个 action index、该 Walk 的 `fixed_route_id` 和最后一条 leg，即 point indices `len(points)-2 -> len(points)-1` 及其 progress 区间。

查询参数必须使用最终已选的 `terminal_target.position`、候选末节点的实际 `SupportSurface`，以及 `final_walk.fixed_route.points[-2]`。继续调用现有 `query_standable_connection()`，不能复制 support、sweep、endpoint region 或 0.1 格支撑采样算法。

只有查询返回 `FEASIBLE`，RouteAdmitter 才沿用 D059 已有的 `_StandableQueryProof` 和 exact terminal 来源记录。最终依赖继续使用同一公式：

```text
final_walk_dependencies
  = final_walk_dependencies_before_terminal_selection
    ∪ exact_terminal_dependencies

full_corridor_dependencies
  = existing_full_corridor_prefix_dependencies
    ∪ exact_terminal_dependencies
```

`final_walk_dependencies_before_terminal_selection` 只保存最终 Walk 在追加终点选择依赖之前已经拥有的依赖。`existing_full_corridor_prefix_dependencies` 只保存现有 full corridor 在终点处理之前已经拥有的 prefix 依赖。早期 action、strict action 和其他 Walk 继续由各自 owner 持有，不能复制进最后 Walk 或 terminal recipe。实现只能移除本次额外追加的 selection dependencies，不能从合并后的总集合反推来源。

如果精确查询返回 `NEEDS_INFORMATION`、`BLOCKED` 或 `UNSUPPORTED`，保持现有失败关闭。下面边界也不扩大：

- 已有 ground traversal proof 后另加的 tail；
- 第二次 direct query 非 `FEASIBLE` 后追加的 tail；
- 最终 Walk 少于两个 fixed-route points，或 terminal 不能绑定 `points[-2] -> points[-1]` 的最后一腿；典型反例是 body connection 后只有一个 graph node，无法形成 terminal leg；
- 高差 Walk、带 traversal plan 的 Walk、Step、JumpUp、JumpGap、ControlledDrop 和非 WALK 模式；
- 无法把查询参数精确映射到最终 action／fixed route 的路线。

这些场景继续保留完整 terminal selection dependencies，无法由 recipe 证明的部分继续使用 `NON_RECIPE` owner，命中时同帧停止。

## owner、消费者和身份不变量

D060 不改变 D058／D059 已建立的权限边界：

- pre-terminal `NON_RECIPE` 与新的 terminal exact recipe 共享同格时，前者仍优先导致 `STOP/NON_RECIPE_OWNER_CHANGED`；
- strict owner 与 exact recipe 共享同格时仍 `STOP/STRICT_OWNER_CHANGED`；
- terminal recipe 只拥有精确查询返回的 dependencies，不能吸收选择扫描依赖，也不能覆盖前腿 provenance；
- 到达候选终点的 full corridor 使用 `existing full-corridor prefix dependencies ∪ exact terminal dependencies`；没有到达终点的 bounded prefix 不带 terminal selection 或 terminal exact 依赖；
- 早期 action、strict action 和其他 Walk 的独占格保持原 owner，并随各自 action 进度退役；terminal proof 不能延长这些 owner，也不能把它们复制到最终 Walk；
- `NavigationSession.observation_request()` 和 `WorldKnowledge.set_protection()` 只消费 tracker 的 effective dependencies。修正后 selection-only 格不再被路线主动请求或保护；选定支撑、净空、sweep 和 0.1 格采样依赖必须继续保留；
- `ActionRoute.goal_state`、目标修订、候选接纳和目标满足判断不变。

`ActiveRouteValidationIdentity` 的九个字段继续逐项匹配：`world_session`、`route_id`、`route_revision`、`source_request_id`、`goal_id`、`goal_revision`、`planning_generation`、`work_identity`、`action_index`。`fixed_route_id` 继续由进度证据单列检查。任何错配都不能借用本次 exact proof，必须在正向输出前停止。

## 实施顺序

### D060-A：冻结实机分支的红测

在现有 D059 路线接纳测试旁增加最小 fixture。优先从失败批次提炼普通平地的精确参数：目标中心 z=9.926195、规划起点 z=5、末图节点 z=9、接纳身体 z=6.284452。fixture 必须自行构造合法 `WorldKnowledge`，不读取本地 artifact 或 `.tmp`。

红测先证明现行行为：terminal 等于末图节点；触发净空格只属于 terminal selection；没有 terminal exact recipe；合法 `UNKNOWN -> AIR` 得到 `STOP/NON_RECIPE_OWNER_CHANGED`。随后把期望改为 D060 行为，确认测试在生产修改前失败。再用 z=10—13 四组目标、新 goal revision、新 route identity 和独立 tracker 冻结实机结构，不能在同一 tracker 上连续改四格。另加一个 body connection 后只有单 graph node、最终 Walk 无法形成最后一腿的反例，必须保留完整 selection dependencies 和 `NON_RECIPE` owner。

### D060-B：补齐 builder

只修改 RouteAdmitter 的终点处理。对符合白名单的 equality 分支，以最终 `WalkSegment.fixed_route.points[-2]` 为 connection source 调用现有精确查询；`FEASIBLE` 时复用 D059 的 terminal proof 和 exact dependency 来源，让 validation plan 把同 action index、同 fixed route id 的最后一腿绑定为 `STANDABLE_CONNECTION`。不得新增 query kind、长期缓存、目标策略或恢复分支。

先运行 D060-A 和现有 D059 builder 检查。若实际需要修改规划搜索、`standable_point_in_region()` 或 D058 tracker，停止并重新审查范围。

### D060-C：消费者和安全反例

检查修正后的 action、full corridor、bounded prefix、observation request、world protection 和 provenance。四条 selection-only 变化必须为 `UNAFFECTED/NO_INTERSECTION`、affected 空、0 query、0 dependency recovery、0 `cancel_braking`，并在同帧继续 Walk。

真实 `WorldKnowledge` 反例必须覆盖：移除已选支撑、插入已选净空 full cube、shared pre-terminal `NON_RECIPE`、shared strict owner，以及九字段身份和 fixed route identity 错配。它们都要在同帧 `STOP`，且不得再提交正向输入。增加 Walk→strict→Walk 或等价多 action 路线：早期 action 的独占格不能出现在最后 Walk；推进后按原 action owner 正常退役。ground traversal tail、direct 非 `FEASIBLE` 和无法绑定最后一腿三个 no-proof 分支继续保留选择依赖并失败关闭。

### D060-D：回归和 v5 性能证据

先跑 D058、D059、route admission、route body、Session、F1-C 和 F1 跟随聚焦集合，再运行完整 motion_nav。D059-D 的 1,408 项完整集合曾有 5 项失败：`changed_heading...seed=163`、standalone 旧 CraftGround 闭包、R28 migration 旧恢复入口和 R28 shared recovery 旧期望在干净 `ced67be` 上也稳定失败；landing-support I3 只在完整集合出现、单独复跑通过。D060 必须逐项记录新的完整结果：同样失败只能按既有基线登记，不能写成完整通过；任何新增失败、失败签名变化或单独可复现的产品回归都要停止收口并先查根因。

性能证据写入新的 `evidence/motion_navigation/d060-route-revalidation-v5/`，不覆盖 D059 v4。使用项目规定的 conda launcher，从干净的 D060 生产提交运行并记录 source identity、原始样本、环境、命令和 SHA256。六组重验继续报告 P95／P99／最大值；新增段 P95≤1 ms。完整 F1-C prepare 丢弃前 100 次后至少保留 1,000 个样本，继续要求 P95≤8 ms、P99≤15 ms、最大值<30 ms。新增 exact query 只发生在 route admission，不能增加无交集帧或逐帧重验的查询数。

### D060-E：独立复审

独立复审重点检查：equality 分支是否真的使用最终 action 的前一点；是否误删 candidate／node／edge／initial 依赖；两个保守 tail 分支是否被放宽；shared owner 和身份是否仍先失败关闭；性能 source identity 是否包含全部实际执行文件。P0／P1／P2 清零后才能进入实机。

### D060-F：只复跑 `straight_2_0`

使用正式 F1 runner、Fabric 1.21、新世界和新目录，只运行 `straight_2_0`。先独立解析原始轨迹与 manifest。四条新 route identity 中，若 selection-only 格由其他正式来源进入 `changed_cells`，必须得到 `UNAFFECTED/NO_INTERSECTION`、0 query、0 recovery；如果不再被路线请求，允许保持 `UNKNOWN`。选定执行证明发生变化时仍须按 typed 结果重验或停止。

场景还必须同时满足既有 F1-D 门槛：目标实际稳定速度 1.8—2.2 格／秒；稳定 excess mean≤0.75、P95≤1.5；修订响应 P95≤5 tick；规划提交／接受修订≤1.25；task recovery 为 0；最终进入 2.5 格保持区；安全违规为 0；终态、来源释放、host、时间归属和清理全部通过。任一门槛失败都停止，不运行另外四场。

## 不做的事

- 不修改 `standable_point_in_region()` 的候选顺序或目标区域；
- 不改变跟随保持距离、修订节流、目标速度、规划期限或恢复预算；
- 不把 selection-only 变化过滤出 `changed_cells`；
- 不把一次 Fabric 代表场景写成统计证明；
- 不为了清空旧完整集合失败顺手修改无关行为。

## 2026-10-05 实施与性能状态

D060-A 至 D060-C 已完成。equality 分支现在用最终 Walk 的 `fixed_route.points[-2]` 建立精确 terminal proof；消费者、shared owner、no-proof、身份、保护和多 action 退役检查均已通过。聚焦集合 250/250；完整 motion_nav 共 1,417 项，仍是既有 5 项失败，没有新增失败。

原 D060-D 完整 prepare 门槛未通过。v5、v6 和低扰动 GC 诊断 v8 都在同一个 captured sample 3242 超过 30 ms，最大值分别为 34.7251、51.8439 和 48.8184 ms。v8 记录到一次 36.9770 ms 的 generation 2 扫描，足以解释该帧超限。六组 route revalidation 和 prepare P95／P99 均通过。v7 因加入 phase wrapper、thread clock 和逐样本对象而改变 GC 相位，只算诊断，不算验收。

D060-D 的性能部分改由 [D061：分开验证独立场景和长 Session 性能](0061-separate-independent-scenario-and-long-session-performance.md)共同决定。v5—v8 原结果不改写。

D061 独立场景批次保留 3,413 个 prepare 样本，P95／P99／最大值为 6.8668／10.9606／14.4872 ms，六组路线重验 P95 为 0.0060—0.5180 ms。长 Session v3 在同一 task／Session／world 中保留 4,096 个正式控制帧；prepare P95／P99／最大值为 5.2493／5.3928／10.1588 ms，retained 产品控制路径 P95／P99／最大值为 11.2050／30.5680／41.2163 ms。正式 trace 线程的 retained gen2 后继续完成至少 128 帧。

v3 的 450 ms slack 来自 500 ms 逻辑 lease，不能代表 50 ms 控制周期。v4 已改用同一对控制路径墙钟起止点计算 `50 ms - duration`，并只用 retained 数据判断控制最大值、deadline miss 和 minimum slack。v4 的 prepare P95／P99／最大值为 4.3106／4.4078／5.7910 ms；retained 控制路径 P95／P99／最大值为 9.9315／25.3232／39.7818 ms，最小余量 10.2182 ms，deadline miss 为 0。D060-D 据此完成。

D060-E 独立复审已经完成。D060-F 从干净提交 `6ff9ad1` 运行一次，原始目录为 `artifacts/f1-known-world-following/20261005T0408072740471Z-straight-2-0-d060/`。稳定 excess mean／P95 为 0.649322／1.541509 格，规划提交／接受修订为 20／16，task recovery 为 2。P95 和零恢复门槛未通过，因此其余四场没有运行。两次恢复的新根因和后续边界见 [D062](0062-unify-direct-walk-validation-proof.md)。v1 的误通过、v2 的诊断失败及其本地原始 trace 都继续保留，不能用后续结果改写。
