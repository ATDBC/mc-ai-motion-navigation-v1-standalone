# F2-S：路线优化前的支撑区域收敛方案

> **实施要求：** 实施时使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans`，按任务逐项完成 RED、最小修复、定向验证和提交。

日期：2026-10-08。状态：任务 4 已按 fail-fast 暂停，F2-S 未关闭。F2-SP 的性能结论保持通过。任务 4 的定向检查和区域性能通过；v9 新增 216 项已运行，原 v8 复跑发现 56 项旧成功退步。规划尾段修复恢复其中 55 项，剩余 1 项暴露“单目标面预选压缩可达目标集合”的规划契约缺口。该问题登记为 F2S-C-04，先由 [D078](../decisions/0078-search-goal-region-as-terminal-set.md) 和 [F2-SG](F2SG-region-goal-multi-terminal-planning.md) 修正区域目标规划契约。F2-S 在 F2-SG 通过前不继续 v7、五组行为集合、完整正逆序、Fabric 或任务 5。

**目标：** 在路线优化器产生更多非格心终点前，让障碍、UNKNOWN、支撑、Session 选面、规划尾段、路线接纳和路线复核使用一致的完成区域规则。

**设计：** 障碍、UNKNOWN 和支撑边界进入同一张网格，从所有安全单元中选出最大的连续矩形。Session 先便宜排序，再惰性运行精确查询。首次选择负责找区域，后续复核只验证已绑定区域仍安全。

**依据：** [D076](../decisions/0076-unify-goal-completion-region-before-route-optimization.md)、[连续地面路线执行](../architecture/continuous-ground-route-execution-v1.md)和 [F2-S 验收](../acceptance/F2S-support-region-convergence.md)。

## 全局约束

- Windows 是正式开发、性能和 Fabric 验收平台；Linux 只作补充复核。
- F2、F2-R、v7 和 v8 原证据只读，不回写失败或修改原分母。
- 只选择原 `GoalState` 内的精确正面积矩形。
- 支撑比例保持 `0.5`；UNKNOWN、危险材质、流体和未支持形状不放宽。
- Session 不新增状态、等待、重试或动作类型分支。
- 不用墙钟截止时间决定选哪个面、返回什么状态或是否跳过候选。
- 采用 fail fast：先固定 RED 和性能上限，再改几何，再接正式调用者，最后才跑大集合和 Fabric。

## 审查重点

1. 统一网格是否同时包含目标、障碍、UNKNOWN 和支撑形状边界，结果是否与旧切块顺序无关。
2. Session 是否先便宜排序、再惰性查询；首个、最后和全部不可行时是否都满足控制期限。
3. 初次选择和复核是否已经分开；区域外出现更优站位时，旧安全路线是否继续有效。
4. 放弃区域的依赖是否泄漏到执行契约。
5. 单网格内门槛切面是否继续类型化拒绝。
6. D061 是否保留生产 P95、P99、最大值及完整夹具门槛。

## fail-fast 顺序

1. 先运行任务 0 的正确性 RED、三类最坏耗时 RED 和独立参考搜索原型。
2. 在 Windows RED 结果上写入区域选择的明确 P95、P99 和最大值门槛。门槛未写入验收文档前，不得进入任务 1。
3. 只实现统一网格。几何定向测试不通过时，不修改 Session 或复核。
4. 再接入 Session、规划尾段和绑定区域复核。三类最坏耗时超过冻结最大值时，立即停止 F2-S，并另写后台或跨帧方案。
5. 定向检查通过后才运行 v9、旧大集合和 Fabric。不得靠重复运行挑选通过批次。

## 任务 0：冻结 RED、性能门槛、v9 和失败分类

**新增：**

- `tests/sim/f2s_cases.py`
- `tests/sim/manifests/navigation-product-f2s-support-region-v9.json`
- `tests/motion_nav/test_f2s_support_region.py`
- `scripts/f2s_support_region_evidence.py`

步骤：

1. 冻结平台外角、一格桥头、柱顶、悬崖角、挖洞平地、半砖和楼梯旁的目标。每类覆盖产品、近战和跟随尺寸、四方向、正常与首条晚 1 tick。
2. 保存当前点查询、区域查询、Session 选面、绑定区域复核和正式 Runtime 结果。平台外角原地失败必须先形成 RED。
3. 增加一个跨越旧障碍切块边界、但整个矩形净空且支撑合格的 RED。旧实现应选择较小区域或拒绝。
4. 为跟随尺寸杂乱目标冻结三类 Windows 性能 RED：排序后首个候选可行、最后一个候选可行、全部候选不可行。记录支撑面数、精确查询数、网格大小、P50、P95、P99 和最大值。
5. 依据 RED 写入区域选择的独立 P95、P99 和最大值门槛。该提交只冻结输入、证据和验收数字，不包含生产修复。
6. v9 保留 v8 全部输入，再增加支撑边缘、高度边缘、障碍与支撑同时裁剪及跨旧切块边界的场景。输入、种子、分层和哈希在生产修改前冻结。
7. 用独立、有界的参考搜索给 v8 的 `41` 项无已知路线标记 `REFERENCE_REACHABLE`、`PROVEN_UNREACHABLE` 或 `UNRESOLVED`。参考搜索不得导入或调用 `known_map_planner` 的节点扩展和代价；只有搜索空间穷尽才能写 `PROVEN_UNREACHABLE`。
8. 把 `9` 项无前进控制和 `5` 项停滞登记到缺陷台账，保存稳定 ID。
9. 运行 RED 和清单重放。输入必须唯一、顺序稳定且不依赖墙钟。
10. 提交冻结输入、RED、区域查询数值门槛和失败分类，不包含生产修复。

### 任务 0 实际结果

- v9 通过引用和哈希保留 v8 的 `104` 个几何输入与 `1800` 个杂乱输入，新增 `216` 个 F2-S 输入；新增输入哈希为 `7e4dc207a4cf1302061f27f6070443013db31a735926d1499e53c9450b49eba4`。
- 新输入覆盖九类地形、三种目标尺寸、四个方向和正常／首条晚一 tick。九类地形是平台外角、桥头、柱顶、悬崖角、挖洞、半砖边缘、楼梯边缘、障碍与支撑同时裁剪、跨旧切块。
- 七项 RED 都按预期失败：平台支撑角、跨旧切块最大矩形、绑定区域复核、Session 首个可行、Session 最后可行、Session 全部不可行、正式平台外角任务。三个 Session RED 直接检查调用次数、候选结果、missing 处理和 `WorldQueryCache` 身份，不再依赖源码字符串。正式链在 tick 21 以 `goal_standing_point_unavailable` 原地结束，零安全违规。
- Windows 基准由仓库 `.venv\python.exe` 的 Python `3.11.16` 运行，固定 `80` 次测量和 `8` 次预热。首个可行为 `1` 次精确查询，最后可行为 `12` 次，全部不可行为 `11` 次；三类最大值分别为 `0.2622`、`1.6338`、`1.4018 ms`。记录的 49 个单元是夹具预期的统一网格，不是当前生产查询实际生成的网格；任务 1 实现后才采真实网格。
- 区域选择门槛冻结为 P95 `≤ 4 ms`、P99 `≤ 6 ms`、最大值 `< 10 ms`。这给当前最坏 P95 留出三倍以上余量，同时仍小于生产准备的 `8/15/30 ms` 包络。
- v8 原记录实际仍是 `41` 项无已知路线、`9` 项无前进控制和 `5` 项停滞。独立平面洪泛搜索把 41 项标为 `39 REFERENCE_REACHABLE`、`0 PROVEN_UNREACHABLE`、`2 UNRESOLVED`。该搜索不导入正式规划器；方块中心四邻接穷尽不能证明玩家连续空间不可达，所以两项保守留作未解。
- 原始 RED、基准和标签位于 `evidence/motion_navigation/F2S-support-region-v1/red/`。任务 0 没有运行 Fabric，也没有修改 `mc2p/`。

## 任务 1：用统一网格选择最大安全矩形

**修改：**

- `mc2p/motion_nav/support_surfaces.py`
- `tests/motion_nav/test_f2s_support_region.py`
- `tests/motion_nav/test_f2_ground_completion_region.py`

接口保持 `standable_region_in_goal(...) -> StandableRegionResult`。新增私有纯函数：

`_largest_valid_subrectangle(xs, zs, valid_cells) -> tuple[float, float, float, float] | None`

步骤：

1. 先写平台外角、桥头、跨旧切块安全矩形，以及障碍和支撑同时裁剪的失败测试。
2. 把目标边界、按身体宽度扩张后的障碍边界、UNKNOWN 最坏占用边界和支撑形状边界放进同一组 `xs`、`zs`。
3. 网格边界保证单元内部的碰撞状态不变。用单元内部代表点判断净空；用四个顶点判断支撑。四个顶点都达到 `0.5`，单元才可用。
4. 用无效单元二维前缀和枚举连续子网格，复杂度限制为 `O(nx² nz²)`。结果按 `(-area, min_x, min_z, max_x, max_z)` 固定选择。
5. 允许结果跨过旧算法人为切出的相邻块，但返回矩形内每个单元都必须合格。不得使用外接矩形，也不得扩大原目标。
6. 用选定矩形重新运行净空与支撑查询，只保留其执行依赖、支撑面身份和真正定义其边界的 owner。
7. 加入密集抽样和错误副本：外接矩形、降低门槛、遗漏 UNKNOWN、依赖旧切块顺序和泄漏放弃区域依赖都必须被发现。
8. 保留单网格内门槛切面的 `UNSUPPORTED` 反例。
9. 定向测试通过后提交几何修复。

## 任务 2：统一正式调用者，并把选择与复核分开

**修改：**

- `mc2p/motion_nav/navigation_session.py`
- `mc2p/motion_nav/known_map_planner.py`
- `mc2p/motion_nav/route_validation.py`
- `tests/motion_nav/test_f2s_support_region.py`
- `tests/motion_nav/test_navigation_session.py`
- `tests/motion_nav/test_known_map_planning.py`

**新增：**

- `tests/motion_nav/test_route_validation.py`

新增公开查询：

`validate_standable_region(world, surface, goal_region, expected_region, *, body_width, body_height, minimum_support_fraction, allowed_materials, query_cache) -> StandableRegionResult`

步骤：

1. 先写正式链测试，证明 Session 当前能选中区域不可用的支撑面，且不会改选后续可用面。
2. Session 用不执行精确区域查询的稳定键排序候选。排序至少包含代表点到目标中心的距离和 `SurfaceNodeId`，不得读取墙钟。
3. `_surface_for_goal` 按排序顺序调用 `standable_region_in_goal()`，一次调用共用一个 `WorldQueryCache`。遇到第一个 `FEASIBLE` 立即停止；只有最终没有可行区域时才汇总缺信息和拒绝状态。
4. 规划尾段需要真实终点时，使用同一选择查询的参考点。`surface_overlaps_region()` 继续只作便宜粗判；不改 A*、节点键、代价或后台作业生命周期。
5. `validate_standable_region()` 只验证 `expected_region`：仍在目标内、净空、支撑合格、材质允许，而且支撑面身份未变。它不能搜索或比较其他矩形。
6. 路线复核改用新验证接口。区域外地形变化即使产生更大安全矩形，也不能让已绑定且仍安全的路线失效。
7. 加入交叉层不变式：相同世界、目标、身体和 profile 下，Session、规划尾段和首次接纳使用相同选择规则；复核只检查已绑定矩形。
8. 加入静态门禁：Session 和规划尾段不得再用 `standable_point_in_region()` 决定目标可用性；复核不得调用全局区域选择。`query_standable_connection()` 的精确连接用途保留。
9. 运行任务 0 的三类 Windows 最坏耗时。若区域查询独立门槛、生产 P99 或生产最大值失败，停止实施，不进入任务 3；另写后台或跨帧设计。
10. 确认 Session 方法数、状态字段和动作分支数不增加，再提交调用链收敛。

## 任务 3：拆分性能量尺并保留失败分类

**修改：**

- `scripts/benchmark_d061_long_session.py`
- `tests/motion_nav/test_d061_long_session_benchmark.py`
- `scripts/f2s_support_region_evidence.py`
- `docs/motion_navigation/acceptance/defect-ledger.md`

步骤：

1. 每帧耗时拆成 `production_prepare_ms`、`simulation_backend_ms` 和 `full_harness_ms`，保留原墙钟样本与冷启动标记。
2. 生产准备保持 P95 `≤ 8 ms`、P99 `≤ 15 ms`、最大值 `< 30 ms`；完整夹具保持 `≤ 50 ms`。模拟后端单独报告 P50、P95、P99 和最大值。
3. 区域选择单列耗时、候选面数、精确查询数和网格大小，并使用任务 0 冻结的 P95、P99 和最大值门槛。跟随目标修订帧单独分层。
4. 用错误副本确认隐藏生产超时、只保留 P95、混淆后端耗时、删除完整夹具门槛和用墙钟改变选择都会失败。
5. 将 `41` 项独立可达标签与 `9+5` 项执行失败稳定 ID 写入证据，不改变原结果。单独报告 `UNRESOLVED` 数量。
6. v9 每层报告“存在站立点但正式任务失败”的数量，只作产品缺口度量。
7. 运行定向测试和一次正式 Windows 性能采集，提交量尺修正。

### 任务 3 实际结果

- 首版量尺 schema 升为 `mc2p.d061-long-session-performance.v5`，但复审发现首版生产准备只累计 Follow、Navigation 和 Arbiter 的部分调用，漏掉 Runtime 在后端前后的输入账本、窗口检查、trace、结果校验、正式观察接入和报告组装。首版 `4096` 帧不能作为“完整生产计算”的通过或失败结论。
- 修正版使用 typed 单帧 recorder。它直接包住 `PlayerRuntimeV1.control_frame`，进入 `_backend_step` 时暂停生产计时并单独计后端，返回后继续计 Runtime 外壳。各段不重叠，也没有通过完整帧减后端推算生产时间。源码哈希口径明确为 `windows_worktree_bytes`。
- 错误副本证明：只保留 P95、隐藏单帧超时、把后端耗时混入生产、删除完整夹具门槛，都会让检查失败。
- 区域选择正式 Windows 结果通过。首个可行、最后可行、全部不可行的 P95 分别为 `0.4681`、`3.5318`、`3.0814 ms`，三类都满足 `4/6/<10 ms`。
- `41` 项无已知路线仍为 `39 REFERENCE_REACHABLE`、`0 PROVEN_UNREACHABLE`、`2 UNRESOLVED`；`9+5` 个执行失败 ID 没有改变。v9 汇总器已经能逐层报告“存在站立点但任务失败”，但没有运行完整 v9。
- 唯一一次正式 D061 使用 `.venv` Python 3.11，保留 `4096` 帧。旧窄口径整体生产准备为 `4.1974/4.3559/4.5309/8.6204 ms`；跟随修订帧为 `7.9296/8.6204/8.6204/8.6204 ms`。因为遗漏的 Runtime 外壳耗时非负，`8.6204 ms` 仍是完整生产时间的下界，已经足以触发 fail-fast；不能把旧口径的其他通过项写成修正版完整生产通过。
- 按要求没有重跑正式 `4096`。另做一次非正式短 Windows 根因诊断，得到 `77` 个连续目标修订样本。修正版生产 P50/P95/P99/max 为 `8.3477/8.8566/8.8845/8.8845 ms`。修订帧 P95 中，Follow update 为 `3.1117 ms`，Navigation prepare 为 `5.0374 ms`，不含后端的 Runtime 外壳为 `0.6591 ms`，Navigation adopt 为 `0.1304 ms`。Follow 内部的目标替换、Session 更新、选面和精确区域选择 P95 分别为 `3.0588/2.9148/2.7117/1.1172 ms`；这些是嵌套耗时，不能相加。
- 非正式诊断只用于定位同步路径热点，不签署产品性能。没有继续运行任务 4。
- 原始性能结果、COMMAND、SHA256、区域基准和失败标签保存在 `evidence/motion_navigation/F2S-support-region-v1/task3/`。

## 任务 4：Windows、v9 与 Fabric 验收

**前置条件：** [D077](../decisions/0077-bound-goal-surface-selection-work.md) 和 [F2-SP 验收](../acceptance/F2SP-goal-selection-performance-remediation.md)已经通过。F2-SP 未通过时，本任务保持未运行。

**修改：**

- `scripts/f2_ground_route_runtime.py`
- `docs/motion_navigation/acceptance/F2S-support-region-convergence.md`
- 新增 `evidence/motion_navigation/F2S-support-region-v1/`

步骤：

1. 先运行统一网格、Session 惰性选面、规划尾段、首次接纳和绑定区域复核的定向检查。任一失败即停止。
2. 运行任务 0 的三类最坏耗时。任一数值门槛失败即停止，不能通过减少样本、墙钟短路或重复运行改写结果。
3. 运行 v9，分别报告障碍边缘、支撑边缘、高度边缘、跨旧切块和原 v8 结果。
4. 运行 F2 `528` 项、F2-R `1904` 新增项、v7 `2000` 项、五组行为集合、严格动作对照和 Windows 完整正逆序。原成功退步、新安全事件和来源泄漏必须为 `0`。
5. 按 fail fast 顺序运行 Fabric：每类先一个 smoke，再运行平台外角、桥头和柱顶四方向正常／首条晚1，共 `24` 个最低正例。
6. Fabric 必须走 profile 4、Runtime、NavigationSession 和实际应用账本。零掉落、伤害、危险接触、期限违规、终态潜行和未注销来源。
7. 全部门槛通过后才更新阶段状态。

### 任务 4 当前结果与停止原因

- 文件尾整理提交为 `fb2a73f1`。Windows 定向检查使用实际测试所有者，`143/143` 通过。任务 0 的三类隔离区域性能 P95 分别为 `0.4386/3.5447/3.1455 ms`，P99 和最大值也全部满足 `4/6/<10 ms`。
- v9 新增 `216` 项中，支撑边缘 `96/96`、障碍边缘 `24/24`、跨旧切块 `24/24` 完成；高度边缘完成 `24/72`。其余 `48` 项作为“存在站立点但任务失败”的产品缺口报告。216 项的安全事件、伤害、来源泄漏和终态潜行均为 `0`。
- 原 v8 首次复跑的固定几何为 `104/104`。杂乱层从历史 `1715/1800` 变为 `1670/1800`；合并后为 `1774/1904`。相对历史记录有 `56` 项旧成功退步、`11` 项旧失败转成功，零新安全事件。该失败结果完整保留。
- 其中 `55` 项退步来自规划尾段把已经证明的最后一条地面边替换成未经规划的斜线。斜线撞柱后被接纳器拒绝。`f9f03590` 保留已证明的支撑面终点；如果原目标更小，再从该点追加同一支撑面上的精确尾段。相关 `100/100` 定向检查通过，原 56 项退步的聚焦复跑恢复 `55` 项，零新安全事件。
- 剩余 `f2r/clutter/0.2/3/9/product` 的目标横跨两个支撑面。D077 的代表点排序选择较近但图上不可达的面，并在规划前丢掉另一个可达面。几何和完成区域都可行，失败原因为 `planning_no_known_route`。这不是继续调整尾段能解决的问题。
- F2-S 自身在当前停止点不修改排序或 planner，也不重复 D061。继续前必须先由独立阶段明确多目标面规划契约、性能和旧证据如何重新验收。
- 新决定已经由 D078 冻结。F2-SG 使用现有 A* `goal_test` 做一次多终点搜索，不修改通用搜索循环，不采用虚拟汇点或 Session 候选重试。F2-S 任务 4 保持暂停，待 F2-SG 通过后从本停止点继续。
- 首次失败、聚焦修复及命令和哈希位于 `evidence/motion_navigation/F2S-support-region-v1/task4/`。

### F2-SG 首轮实施后的状态

F2-SG 批次 0—3 已把完整区域目标接入一次后台多终点搜索，F2S-C-04 主例已通过。不过原 v8 杂乱层首次完整复跑只有 `1696/1800`，出现 `51` 项旧成功退步。零边末段修复恢复其中 `42` 项，剩余 `9` 项共同指向缺失的 terminal execution proof 和末段正式成本。

现有 GroundTraversalPlan 已证明这个方向可以覆盖停稳、站立、普通 Walk 的代表末段，但不能覆盖当前目标契约的全部模式、姿态和连续入口。F2-SG 因此没有通过，后续大集合、D061 和 Fabric 没有运行。F2-S 任务 4 继续停在原位置；不能把多终点主例通过写成 F2-S 通过。

## 任务 5：公开整理与路线优化交接

**修改：**

- `AGENTS.md`
- `packaging/motion-navigation-standalone/README.md`
- `packaging/motion-navigation-standalone/AGENTS.md`
- `config/motion-navigation/standalone-export-v1.json`
- 本阶段文档和验收记录

步骤：

1. 保留 F2-R 关闭结论，将 F2-S 写成新阶段，不改写旧证据。
2. 主项目保留旧 `docs/superpowers/plans/2026-10-06-motion-navigation-m0-m1.md`。公开清单和公开完整性测试删除它，确保公开运动导航文档只使用四类正式目录。
3. 路线优化方案在实施前冻结预计修改文件、不修改的 owner，以及至少一个必须潜行才能完成的 Fabric 场景。
4. 生成独立公开快照，运行哈希、专项和完整 motion_nav 检查。公开 `main` 只接收整理后快照，三方审查分支不修改。

## 止损条件

- 需要按地形名称、方向或目标用途写生产分支；
- 需要降低 `0.5` 支撑门槛、扩大原目标或使用外接矩形；
- 统一网格不能在固定候选上有界运行；
- Session、监督者或 Runtime 需要新增状态和重试路径；
- 需要修改 A*、动作证明或空中动作语义；
- 区域选择、生产 P99 或生产最大值门槛失败；
- 两轮修复后仍出现同类点／区域分裂。

## 进入路线优化器的条件

1. 支撑边缘固定正例通过，原成功退步为 `0`；
2. Session、规划尾段和接纳共用首次选择，复核只验证绑定矩形；
3. v9、`41` 项独立可达标签和 `14` 项执行失败 ID 已冻结；
4. Windows、区域查询、D061 和 Fabric 门槛通过，失败证据保留；
5. 路线优化器的文件范围、owner 边界和必须潜行场景已经冻结。
