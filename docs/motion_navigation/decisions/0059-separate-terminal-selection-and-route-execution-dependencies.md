# D059：区分目标站位选择依赖与已选路线执行依赖

日期：2026-10-05。状态：D059-A 至 D059-D 的组件实现、性能证据和独立复审整改已完成。D059 修后的 `straight_2_0` Fabric 单场复跑已经运行但仍未通过；terminal 等于末图节点时跳过 exact query 的缺口转入 [D060](0060-prove-terminal-node-execution-without-selection-leakage.md)。F1-D 保持打开。

## 要解决的问题

D058 允许普通同高 Walk 在路线依赖变化后重放原几何查询。组件和性能检查已经通过，但新的 `straight_2_0` 实机批次仍发生四次 `cancel_braking` 和四次依赖恢复。

四次触发仍是路线前方空气从 `UNKNOWN` 变成 `AIR`。它们位于 z=10、11、12、13，对应 Observation 45、56、62、73。这四份观察分别在场景 tick 42、53、59、70 写入，supervisor 在下一控制帧 tick 43、54、60、71 消费变化并启动恢复。D058 的运行入口确实被调用，但活动路线返回 `STOP`。修复后的正确处置要由每个变化格的 owner/provenance 决定，不能预先全部写成 `CONTINUE/REVALIDATED`。

当前证据没有保存 `ActiveRouteValidation`、dependency owner 和 query kind，因此不能从原批次直接读出 typed `STOP` 原因。结合最终路线的构造方式，最可能的原因是：

1. `standable_point_in_region()` 为了从整个 `GoalState` 区域中选择一个站位，会检查多个候选格；
2. `terminal_target.dependencies` 因此包含已选站位的证明，也包含没有被选中的候选格；
3. RouteAdmitter 把这整包“选择依赖”复制进最后一个 Walk；到达终点的 corridor 和随后构造的 validation plan 也会带入这组依赖；
4. 只有已选终点的 direct connection 有精确 `STANDABLE_CONNECTION` recipe；其余选择依赖落入 `NON_RECIPE` owner；
5. 未选格后来从 `UNKNOWN` 变成 `AIR` 时，tracker 按 D058 的安全规则返回 `STOP/NON_RECIPE_OWNER_CHANGED`。

这里混合了两类用途不同的依赖：

- **目标站位选择依赖**：回答“在整个目标区域里选哪一个站位”；
- **已选路线执行依赖**：回答“身体沿已经接纳的路线走到已选站位是否仍安全”。

目标区域里另一个未选格出现新空气证据，可能影响重新选择的结果，但不会自动使当前已选路线失效。反过来，已选站位的支撑消失或头部空间被堵住，必须在同一帧停止。

## 决定

保留目标站位选择的现行查询和失败边界。RouteAdmitter 仍在当前 `GoalState`、goal revision 和 `WorldView` 上调用 `standable_point_in_region()`。未知、阻塞和不支持仍按原规则影响本次选择与接纳，不能把未知格当作空气，也不能缓存旧 revision 的选择。

路线接纳完成后，只从活动执行依赖中去掉本次终点选择额外加入的部分。不能用终点 proof 整包替换最后一个 Walk 的 dependencies。冻结集合关系为：

```text
final_active_execution_dependencies
  = pre_terminal_route_execution_dependencies
    ∪ exact_terminal_proof_dependencies
```

其中 `pre_terminal_route_execution_dependencies` 原样保留此前所有 `SurfaceWalkEdge`、surface node 和 initial connection 的执行依赖。即使某个格也出现在 `terminal_selection_dependencies` 中，它只要仍由前腿或 initial connection 拥有，就必须继续留在 provenance 中。具体规则如下：

- 如果最终终点连接有精确的 `STANDABLE_CONNECTION` proof，只移除本次追加的 `terminal_selection_dependencies`，再把 exact proof dependencies 加到最后一个 Walk；
- corridor 只有在其 prefix 确实到达候选终点时，才做同样的差集／并集。没有到达终点的 bounded corridor 不读取这组终点选择依赖；
- validation plan 保留所有原 `WALK_LEG` 和 `INITIAL_CONNECTION` owner。终点 recipe 只拥有 exact proof dependencies，不能覆盖、合并或重建前腿 provenance；
- `ActionRoute.goal_state` 继续保留完整目标区域，运行时的目标满足判断不变；
- 如果没有精确 terminal proof，保持 D058 的 `NON_RECIPE` 失败关闭，不能猜测或缩小依赖；
- 候选身份、goal revision、世界会话和接纳时的站位选择检查全部保留。

本决定不改变 `standable_point_in_region()` 的搜索顺序、区域几何或选择算法，也不扩大 D058 的动作白名单。

## 永久保存有类型的路线重验诊断

先补证据，再改依赖。以后不能再从 `active_route_dependency_changed` 反推 tracker 的结果。

现有 `BodyRouteValidation` 作为每帧诊断载体，增加 `observation_sequence_id`。它继续直接保存：

```python
@dataclass(frozen=True, slots=True)
class BodyRouteValidation:
    observation_sequence_id: int
    incumbent: ActiveRouteValidation | None
    pending: ActiveRouteValidation | None
```

边界如下：

- `ExecutionSupervisor` 是唯一生产者；sequence 必须等于本帧 `NavigationFrame.body.sequence_id`；
- incumbent 和 pending 保存 tracker 返回的原始 `ActiveRouteValidation`，不能在 Session 中改 reason、删字段或合并两者；
- `NavigationSession` 只把本帧不可变结果原样放入 `NavigationDiagnostics.route_validation`；没有执行路线重验的帧写 `None`，不能沿用上一帧结果；
- `tests/sim/runner.py` 目前手工组装逐帧 row，必须显式新增 `"route_validation": None if diagnostics.route_validation is None else asdict(diagnostics.route_validation)`；不能假设新增 dataclass 字段会自动进入模拟 trace；
- F1-D runner 当前保存整个 `"diagnostics": asdict(diagnostics)`。测试必须确认新增字段确实随整体 diagnostics 写入，不能只靠代码阅读推断；
- 两个 runner 的持久化测试都必须从 JSON 重新读取并逐字段比较 observation sequence、disposition、typed reason、identity、affected cells、refreshed dependencies、missing cells 和 query count，证明结果不是只在进程内可见。

这些字段只用于诊断和验收。Session 的权限判断仍消费 supervisor 当帧返回的 typed 结果，不能改成读取已序列化数据或 reason 文本。

## 最小实现

### 1. 先复现真实依赖污染

组件红测使用一个面积大于单格的 `GoalState`。场景必须满足：

- `standable_point_in_region()` 扫描到至少一个未选中的 `UNKNOWN` 格；
- 同一次接纳仍选出合法终点；
- 已选终点有精确 `STANDABLE_CONNECTION` proof；
- 最终路线是无 traversal plan 的普通同高 Walk。

先断言当前实现把未选格登记到最后 Walk 的 `NON_RECIPE` owner。随后把该格从 `UNKNOWN` 更新为 `AIR`，红测必须复现 `STOP/NON_RECIPE_OWNER_CHANGED`。测试还要逐格读取 validation plan 的 typed owner 和 provenance，记录该格是否同时属于 pre-terminal 前腿、initial connection 或 exact terminal recipe。这个测试用于确认根因；如果得到其他 typed reason，停止实施并按新事实修订本决定，不能为了符合方案改测试。

### 2. 只移除终点选择额外加入的依赖

RouteAdmitter 在最终 `ActionRoute` 已完成终点替换后区分三组依赖：

- `terminal_selection_dependencies`：`standable_point_in_region()` 返回的完整扫描依赖，只用于本次选择与接纳；
- `pre_terminal_route_execution_dependencies`：终点选择前已经属于 SurfaceWalkEdge、surface node 或 initial connection 的执行依赖，owner 和 provenance 必须原样保留；
- `exact_terminal_proof_dependencies`：已选终点 exact proof 的 dependencies，只作为终点连接新增到最后 Walk、到达终点的 corridor 和 terminal recipe。

只有 exact proof 存在时才执行依赖分离。实现应在追加终点选择依赖前保留 pre-terminal 集合，并按上面的冻结公式构造最终集合；不能对已经合并的总集合直接做 `- terminal_selection_dependencies`，否则会误删与前腿共享的格。实现继续复用现有 `_StandableQueryProof` 和 `WalkValidationRecipe(STANDABLE_CONNECTION)`，不新增第三种几何查询，也不复制 support、sweep 或 endpoint region 算法。

最终结构必须满足：

- 最后 Walk 的 dependencies 等于原 pre-terminal 执行依赖与 exact terminal proof 依赖的并集，不含只由未选站位扫描引入的依赖；
- 只有实际到达终点的 corridor 才使用同一公式；未到终点的 corridor 保持原 prefix dependencies；
- active validation provenance 保留每个原 owner 的引用，terminal recipe 只增加自己的 exact proof dependencies；
- terminal recipe 的初始 dependencies 恰好等于首次 exact proof；
- 选定支撑、头部净空和连接 sweep 的格仍由该 recipe 拥有；
- 没有 exact proof 时，原完整依赖继续进入 `NON_RECIPE` owner 并失败关闭。

多 owner 反例必须走真实 RouteAdmitter builder，不能手工拼 `ActiveRouteValidationPlan`：

- 让一个 pre-terminal `NON_RECIPE` owner 与 exact terminal recipe 共享同一格。移除 selection owner 后，原 `NON_RECIPE` owner 仍在，该格变化必须 `STOP`；
- 让 exact terminal recipe 与严格动作 owner 共享同一格。严格 owner 仍在，该格变化必须 `STOP`；
- 两组都直接断言 builder 生成的 owner 表和 `position -> owner_refs`，证明集合分离没有覆盖其他 provenance。

### 3. 没有 exact terminal proof 时保持失败关闭

下面三个真实 builder 分支都没有 exact terminal proof，必须分别覆盖：

1. terminal 与最后一个图节点重合，没有第二次 direct query；
2. ground traversal `has_proof`，原 proved route 后另加 tail；
3. 第二次 direct query 返回非 `FEASIBLE`，随后 append tail。

三组都把完整 `terminal_selection_dependencies` 留在活动执行依赖中；其中没有被既有 recipe 覆盖的部分进入 `NON_RECIPE` owner。命中这类 owner 的相关格变化时必须同帧 `STOP`，不能因为同高、目标可达或存在其他 Walk recipe 而缩小依赖。

### 4. effective dependencies 的两个正式消费者

集合修正后要直接检查两个现行消费者：

- `NavigationSession.observation_request()` 仍请求 exact terminal proof 的支撑、净空、connection sweep 和 0.1 格采样支撑；只属于未选站位的 selection-only 格不再请求；
- `WorldKnowledge.set_protection()` 通过 supervisor 的 effective dependencies 保护同一组 exact proof 格；selection-only 格不再保护。

测试分别构造 corridor 确实到达终点和 bounded corridor 未到终点两种路线，直接断言冻结公式。到达终点时加入 exact proof dependencies；未到终点时保持原 prefix dependencies，不加入或删除任何 terminal 集合。

### 5. 保留安全反例

使用真实 `WorldKnowledge` 更新覆盖：

- 移除已选终点支撑；
- 在已选终点头部空间插入 full cube；
- 通过真实 RouteAdmitter builder 让 exact terminal recipe 与 strict／non-recipe owner 共享一个 changed cell；
- 构造没有 exact terminal proof 的 tail。

身份反例必须逐项覆盖 `ActiveRouteValidationIdentity` 的全部字段：

- `world_session`；
- `route_id`；
- `route_revision`；
- `source_request_id`；
- `goal_id`；
- `goal_revision`；
- `planning_generation`；
- `work_identity`；
- `action_index`。

`fixed_route_id` 不属于 `ActiveRouteValidationIdentity`，单独作为 recipe／owner／progress 身份检查。上述九个身份字段与单列的 `fixed_route_id` 中，任一错配都必须返回 typed `STOP`，旧身份不能授权当前路线，也不能刷新 dependencies。

这些场景必须在同一帧返回 typed `STOP`，incumbent 不得再提交正向输入。pending 的失败仍只丢 pending，不能刹停合法 incumbent。

### 6. 重放四次顺序变化

正式链组件场景先用 typed owner/provenance 对 z=10、11、12、13 逐格分类，再冻结各自的预期处置：

- 如果变化格只属于被移除的 `terminal_selection_dependencies`，修复后必须返回 `UNAFFECTED/NO_INTERSECTION`，`affected_cells=()`、`queries_used=0`；
- 如果变化格还属于保留的 pre-terminal Walk 或 exact terminal proof，修复后必须沿对应 recipe 返回 `CONTINUE/REVALIDATED`，并记录真实 affected cells、refreshed dependencies 和 query count；
- 如果分类发现它还属于严格动作或其他仍应失败关闭的 owner，不能删除该 owner，也不能把该格塞进 terminal recipe 来追求继续。先修订逐格预期，再决定该场景是否仍应停止。

完成分类后，按 Observation 45、56、62、73 的顺序把四个位置从 `UNKNOWN` 更新为 `AIR`。每次都先让正式 Session 接受对应的新 goal revision，再由正式 RouteAdmitter 接纳新的 route id／route revision，并为该 RouteControl 创建自己的 tracker。不能在同一个 tracker 上连续改四格，也不能手工改身份冒充四次目标修订。测试时间线要区分观察写入帧和下一控制帧消费；共同门槛是：

- `changed_cells` 保留该变化；
- incumbent 返回与逐格 owner 分类一致的 typed 处置；
- `affected_cells`、identity、observation sequence 和 query count 与该处置一致；
- query count 不超过单 RouteControl 2 次、整帧 4 次；
- 任务恢复、`cancel_braking` 和额外 recovery planning submission 都是 0；
- 同帧原 Walk 继续前进。

这组检查不能通过过滤 `UNKNOWN -> AIR`、清空 pre-terminal dependencies、把 `NON_RECIPE` 改名，或为未选站位伪造 recipe 来通过。

组件测试通过合法 `WorldKnowledge` 更新显式注入四格，所以四格都必须出现在各自的 `changed_cells`。Fabric 门槛不同：

- 仍属于 execution proof 且继续由 `observation_request()` 请求的格，必须在正式观察到达后进入 `changed_cells`，并按分类得到 `CONTINUE/REVALIDATED`；
- selection-only 格已从 observation request 和保护集合移除，允许保持 `UNKNOWN`，也允许不出现在 Fabric 的 `changed_cells`；
- 如果 selection-only 格由其他正式观察来源变成已知，它必须得到 `UNAFFECTED/NO_INTERSECTION`、零查询、零恢复，不能重新触发制动。

## 实施顺序

1. **D059-A：诊断契约。** 给 `BodyRouteValidation` 绑定 observation sequence，由 supervisor 唯一产生；Session diagnostics 原样携带。`tests/sim/runner.py` 手工加入单独的 `route_validation` 字段；F1-D runner 继续整体 `asdict(diagnostics)`，并用测试确认字段存在。先写序列、incumbent／pending 和两类 JSON round-trip 红测。
2. **D059-B：根因红测与逐格分类。** 用大 `GoalState` 和真实 `WorldKnowledge` 复现未选站位 `UNKNOWN -> AIR` 导致的 `NON_RECIPE` 停止。读取 z=10、11、12、13 的全部 typed owner/provenance，区分 selection-only、可重验执行 proof 和必须停止的共享 owner，并把逐格预期写入验收记录。四次重放分别接受真实的新 goal revision 和新 route／revision，各自创建 tracker。若 typed 结果不同，先更新根因记录。
3. **D059-C：依赖分离。** 只修改 RouteAdmitter 最终路线构造。exact proof 存在时，保留全部 pre-terminal route execution dependencies，只去掉本次追加的 terminal selection dependencies，再加入 exact terminal proof dependencies；到达终点的 corridor 做同样处理，validation provenance 保留前腿 owner。补真实 builder 的 non-recipe／strict 共享格反例，并覆盖三个无 exact proof 分支。
4. **D059-D：消费者、安全与性能。** 检查 `NavigationSession.observation_request()` 和 `WorldKnowledge.set_protection()` 的 exact／selection-only 集合；直接断言 corridor 到达与未到终点两种公式。逐项跑完整 ActiveRouteValidationIdentity 和 fixed route 身份反例。随后跑 D058 全部组件、真实世界反例、四次顺序变化、F1-C 完整清单和现有性能基准。新增逻辑不能提高查询上限；D058 新增段仍要求 P95≤1 ms，完整准备仍要求 P95≤8 ms、P99≤15 ms、最大值<30 ms。
5. **独立复审。** 检查诊断没有成为权限旁路，执行依赖没有漏掉已选终点的 support／clearance／sweep，选择依赖仍参与接纳。
6. **只复跑 `straight_2_0`。** 使用新世界和新目录。失败就停止，不运行另外四个 F1-D 场景。

## D059-D 实施结果

D059-D 已完成消费者、身份、安全和性能检查，初始源码提交为 `2b9f2fa`，复审身份边界修正为 `6645780`。`NavigationSession.observation_request()` 与 `WorldKnowledge.set_protection()` 都继续读取 supervisor 的 effective dependencies。组件结果确认：有 exact terminal proof 时，两者保留已选终点的支撑、净空、扫掠和 0.1 格采样依赖，并排除 selection-only 格；没有 exact proof 时仍保留完整选择依赖。bounded corridor 尚未到达终点时不带终点集合，进度到达终点后才加入 exact proof，selection-only 格始终不进入。

完整身份检查没有读取诊断来授权。`RouteControl` 从当前不可变路线和 action index 生成 `ActiveRouteValidationIdentity`，`ExecutionSupervisor` 把它作为本帧期望身份交给 tracker。复审进一步要求构造时验证实际 owner：tracker 必须持有同一 `ActiveRoute`；executor 必须持有该路线的同一 `ActionRoute`；存在 coordinator 时，它必须同时持有同一 `ActiveRoute` 和同一个 executor。任一对象身份错配都在创建控制者时抛出 `ContractViolation`，不能先生成看似属于新路线的 validation identity。九个 validation 字段任一错配仍返回 `STOP/ROUTE_IDENTITY_CHANGED`；`fixed_route_id` 继续由正式进度证据单独检查。

复审修正后的 RouteControl、Session、D058／D059 和 F1 聚焦组合检查为 **282/282**。完整 `tests/motion_nav` 的前次运行仍是 1,408 项、5 项失败；其中 4 项在干净的 `ced67be` 上同样稳定失败，另 1 项落点移除场景单独复跑通过。本批没有为这些既有或时序问题改生产逻辑，也不把完整集合写成通过。

复审后的正式性能证据位于 `evidence/motion_navigation/d059-route-revalidation-v4/`，来源提交为 `6645780`，运行前工作树干净。source identity 现在包含 `route_body_controller.py`。六组路线重验各 1,000 个样本，P95 为 0.0056—0.2951 ms，全部低于 1 ms。完整 F1-C prepare 丢弃前 100 次后保留 3,413 个样本，P95／P99／最大值为 6.3571／10.2462／13.3886 ms，全部通过门槛。v3 保留原字节，作为复审修正前的历史证据；它的 source identity 没有包含 `route_body_controller.py`，不再作为当前最终证据。

D059-D 组件与性能批本身没有运行 Fabric，独立复审整改随后完成。D059 修后的 `straight_2_0` 已从新世界、新目录单场运行，原始目录为 `artifacts/f1-known-world-following/20261005T0035326046513Z-straight-2-0-d059/`。结果仍未通过：稳定 excess mean／P95 为 1.887882／2.136090 格，规划提交／接受修订为 25／19，task recovery 为 4。原数字和原文件保持不变。typed 诊断确认四次都是 `STOP/NON_RECIPE_OWNER_CHANGED`；后续处理见 [D060](0060-prove-terminal-node-execution-without-selection-leakage.md)。F1-D 没有关闭。

## 冻结时预计文件范围与实际修改

生产修改预计限于：

- `mc2p/motion_nav/execution_supervisor.py`：产生带 observation sequence 的 typed validation frame；
- `mc2p/motion_nav/navigation_session.py`：原样携带当帧诊断；
- `mc2p/motion_nav/route_admission.py`：分开 terminal selection 和 exact execution dependencies。

实际实施还修改了 `mc2p/motion_nav/route_body_controller.py`。它负责确认 `ActiveRoute`、tracker、executor 和可选 coordinator 属于同一控制者，并从这组已核对的对象生成当前 validation identity。`execution_supervisor.py` 只把该身份传给 tracker；它没有复制身份，也没有新增长期状态。

测试和证据修改预计限于 D058 路线接纳／运行时测试、导航诊断测试、F1 模拟与 Fabric runner 测试。若实现要求修改 `standable_point_in_region()`、规划搜索、GoalState 几何、跟随阈值、恢复预算或底层控制，先停止并重新审查范围。

## 完成门槛

D059 只有同时满足下面条件才完成：

1. 每帧路线重验结果可以从持久化 JSON 直接审计；
2. 大目标区域红测证明未选站位依赖与已选路线依赖已经分开；
3. 四次组件注入分别使用真实的新 goal revision、新 route／revision 和独立 tracker，并得到与逐格 owner/provenance 分类一致的 `UNAFFECTED/NO_INTERSECTION` 或 `CONTINUE/REVALIDATED`；共同满足 0 任务恢复、0 `cancel_braking`，并继续同帧 Walk；
4. 真实 builder 的 non-recipe／strict 共享 owner 和三个无 exact proof 分支仍同帧停止；
5. observation request 与 world protection 保留全部 exact support／clearance／sweep／0.1 格支撑，并排除 selection-only 格；
6. 已选终点的支撑、净空或连接变化仍同帧停止；ActiveRouteValidationIdentity 九个字段和单列 fixed route id 任一错配也都停止；
7. D058 的 query 上限、性能和原严格动作边界全部保持；
8. 独立复审没有未关闭 P0／P1／P2；
9. 新 `straight_2_0` 同时通过距离、响应、规划频率、安全、终态和清理门槛。

本决定不把新的失败批次改写为通过。F1-D 只有在 `straight_2_0` 通过后，才恢复其余四个场景的逐场验收。
