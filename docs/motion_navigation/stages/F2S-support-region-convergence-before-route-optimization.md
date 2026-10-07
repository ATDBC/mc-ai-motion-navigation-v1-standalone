# F2-S：路线优化前的支撑区域收敛方案

> **实施要求：** 实施时使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans`，按任务逐项完成 RED、最小修复、定向验证和提交。

日期：2026-10-08。状态：计划已冻结，尚未实施。

**目标：** 在路线优化器产生更多非格心终点前，让障碍、支撑、Session 选面、规划尾段和路线接纳共用同一份正式完成区域结论。

**设计：** 净空先生成精确矩形块，支撑网格再从每个块中裁出门槛合格的最大子矩形。Session、需要真实终点的规划尾段和接纳层都调用 `standable_region_in_goal()`。中心点搜索和生命周期不改。

**依据：** [D076](../decisions/0076-unify-goal-completion-region-before-route-optimization.md)、[连续地面路线执行](../architecture/continuous-ground-route-execution-v1.md)和 [F2-S 验收](../acceptance/F2S-support-region-convergence.md)。

## 全局约束

- Windows 是正式开发、性能和 Fabric 验收平台；Linux 只作补充复核。
- F2、F2-R、v7 和 v8 原证据只读，不回写失败或修改原分母。
- 只选择原 `GoalState` 内的精确正面积矩形。
- 支撑比例保持 `0.5`；UNKNOWN、危险材质、流体和未支持形状不放宽。
- Session 不新增状态、等待、重试或动作类型分支。
- 采用 fail fast：代表性 RED 的实际根因与 D076 不符时，立即停止扩大测试。

## 审查重点

1. 障碍产生多个精确块后，是否检查了所有块。
2. 支撑裁剪后，放弃区域的依赖是否泄漏到执行契约。
3. 单网格内门槛切面是否继续类型化拒绝。
4. Session、规划尾段和接纳是否得到同一支撑面与矩形；传入相同 `connection_from` 时，参考点是否一致。
5. D061 是否分开生产、模拟后端和完整夹具耗时，而且没有降低门槛。

## 任务 0：冻结 RED、v9 和失败分类

**新增：**

- `tests/sim/f2s_cases.py`
- `tests/sim/manifests/navigation-product-f2s-support-region-v9.json`
- `tests/motion_nav/test_f2s_support_region.py`
- `scripts/f2s_support_region_evidence.py`

步骤：

1. 冻结平台外角、一格桥头、柱顶、悬崖角、挖洞平地、半砖和楼梯旁的目标。每类覆盖产品、近战和跟随尺寸、四方向、正常与首条晚 1 tick。
2. 保存当前点查询、区域查询、Session 选面和正式 Runtime 结果。平台外角原地失败必须先形成 RED。
3. v9 保留 v8 全部输入，再增加支撑边缘、高度边缘和障碍与支撑同时裁剪的场景。输入、种子、分层和哈希在生产修改前冻结。
4. 用独立参考搜索给 v8 的 `41` 项无已知路线标记 `REFERENCE_REACHABLE`、`PROVEN_UNREACHABLE` 或 `UNRESOLVED`。
5. 把 `9` 项无前进控制和 `5` 项停滞登记到缺陷台账，保存稳定 ID。
6. 运行 RED 和清单重放。输入必须唯一、顺序稳定且不依赖墙钟。
7. 提交冻结输入和 RED，不包含生产修复。

## 任务 1：在所有精确块中选择支撑合格子矩形

**修改：**

- `mc2p/motion_nav/support_surfaces.py`
- `tests/motion_nav/test_f2s_support_region.py`
- `tests/motion_nav/test_f2_ground_completion_region.py`

接口保持 `standable_region_in_goal(...) -> StandableRegionResult`。新增私有纯函数：

`_largest_supported_subrectangle(xs, zs, fractions, minimum_support_fraction) -> tuple[float, float, float, float] | None`

步骤：

1. 先写平台外角、桥头和“最大障碍块支撑不足，较小块可用”的失败测试。
2. 使用无效顶点二维前缀和枚举连续子网格，复杂度限制为 `O(nx² nz²)`。不采用对每个候选再次扫描全部顶点的六重循环。
3. 对每个净空精确块独立建网格并查找子矩形，再以 `(-area, min_x, min_z, max_x, max_z)` 全局选择。
4. 用选定矩形重新运行净空与支撑查询，只保留其执行依赖和定义边界的障碍 owner。
5. 加入密集抽样和错误副本：外接矩形、降低门槛、只检查最大障碍块和泄漏放弃区域依赖都必须被发现。
6. 保留单网格内门槛切面的 `UNSUPPORTED` 反例。
7. 定向测试通过后提交几何修复。

## 任务 2：统一正式调用者

**修改：**

- `mc2p/motion_nav/navigation_session.py`
- `mc2p/motion_nav/known_map_planner.py`
- `tests/motion_nav/test_f2s_support_region.py`
- `tests/motion_nav/test_navigation_session.py`
- `tests/motion_nav/test_known_map_planning.py`

步骤：

1. 先写正式链测试，证明 Session 当前能选中区域不可用的支撑面，且不会改选可用面。
2. `_surface_for_goal` 改为实例方法，调用 `standable_region_in_goal()`，并传入 `self.profiles.ground.support_materials`。候选按区域参考点到目标中心的距离排序，平手按 `SurfaceNodeId` 固定。
3. 规划尾段需要真实终点时，改用同一区域查询的参考点。`surface_overlaps_region()` 继续只作便宜粗判；不改 A*、节点键、代价或后台作业生命周期。
4. 加入交叉层不变式：同一世界、目标、身体和 profile 下，Session 选面、规划尾段和接纳重放的状态与区域边界一致。参考点允许随明确的 `connection_from` 改变；输入相同时必须一致。
5. 加入静态门禁：Session 和规划尾段不得再用 `standable_point_in_region()` 决定目标可用性。`query_standable_connection()` 的精确连接用途保留。
6. 确认 Session 方法数、状态字段和动作分支数不增加，再提交调用链收敛。

## 任务 3：拆分性能量尺并保留失败分类

**修改：**

- `scripts/benchmark_d061_long_session.py`
- `tests/motion_nav/test_d061_long_session_benchmark.py`
- `scripts/f2s_support_region_evidence.py`
- `docs/motion_navigation/acceptance/defect-ledger.md`

步骤：

1. 每帧耗时拆成 `production_prepare_ms`、`simulation_backend_ms` 和 `full_harness_ms`，保留原墙钟样本与冷启动标记。
2. 生产准备保持 P95 `≤ 8 ms`；完整夹具保持 `≤ 50 ms` 健康门槛；模拟后端单独报告 P50、P95、P99 和最大值。
3. 用错误副本确认隐藏生产超时、混淆后端耗时和删除完整夹具门槛都会失败。
4. 将 `41` 项可达标签与 `9+5` 项执行失败稳定 ID 写入证据，不改变原结果。
5. 运行定向测试和一次正式 Windows 性能采集，提交量尺修正。

## 任务 4：Windows、v9 与 Fabric 验收

**修改：**

- `scripts/f2_ground_route_runtime.py`
- `docs/motion_navigation/acceptance/F2S-support-region-convergence.md`
- 新增 `evidence/motion_navigation/F2S-support-region-v1/`

步骤：

1. 先运行支撑几何、Session 选面、规划尾段和接纳重放的定向检查。任一失败即停止。
2. 运行 v9，分别报告障碍边缘、支撑边缘、高度边缘和原 v8 结果。
3. 运行 F2 `528` 项、F2-R `1904` 新增项、v7 `2000` 项、五组行为集合、严格动作对照和 Windows 完整正逆序。原成功退步、新安全事件和来源泄漏必须为 `0`。
4. 按 fail fast 顺序运行 Fabric：每类先一个 smoke，再运行平台外角、桥头和柱顶四方向正常／首条晚1，共 `24` 个最低正例。
5. Fabric 必须走 profile 4、Runtime、NavigationSession 和实际应用账本。零掉落、伤害、危险接触、期限违规、终态潜行和未注销来源。
6. 全部门槛通过后才更新阶段状态。

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
- 支撑子网格不能在固定候选上有界运行；
- Session、监督者或 Runtime 需要新增状态和重试路径；
- 需要修改 A*、动作证明或空中动作语义；
- 两轮修复后仍出现同类点／区域分裂。

## 进入路线优化器的条件

1. 支撑边缘固定正例通过，原成功退步为 `0`；
2. Session、规划尾段和接纳共用区域查询；
3. v9、`41` 项可达标签和 `14` 项执行失败 ID 已冻结；
4. Windows、D061 和 Fabric 门槛通过，失败证据保留；
5. 路线优化器的文件范围、owner 边界和必须潜行场景已经冻结。
