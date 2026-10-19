# 运动导航代码边界

日期：2026-09-23

## 要解决的问题

`mc2p/motion_nav` 同时积累了正式运行代码、离线证据工具和旧阶段参考实现。文件仍有用途，不代表它们都可以进入在线控制链。若继续平铺在同一层，新代码很容易误用物化图、历史空气协调器或验收辅助函数。

## 目录职责

| 位置 | 保存什么 | 使用限制 |
|---|---|---|
| `mc2p/motion_nav/*.py` | 正式运行契约与在线实现 | 可以进入观察、规划、执行和控制链 |
| `mc2p/motion_nav/evidence/` | 校准、比较、快照验证和验收计算 | 只读正式产物，不拥有在线状态，不写玩家输入 |
| `mc2p/motion_nav/legacy/` | 已被正式实现替代、但旧证据仍需调用的实现 | 新运行代码不得依赖；迁移完调用者和证据后再决定删除 |
| `mc2p/motion_nav/planning_reference.py` | 物化图 A* 与独立 Dijkstra 等小图参照入口 | 只用于回归、等价性检查和诊断，不进入大地图热路径 |

## 当前隔离结果

- `AirConfirmationService` 已被 `NavigationObservationAdapter.air_request()` 和 `ingest()` 取代，移入 `legacy`。旧路径保留薄兼容层。
- 地面校准、运动预测比较、三版快照验证、历史证据归一化和路线形状计分移入 `evidence`。旧路径只转发到唯一实现。
- `build_walk_graph`、`astar_plan`、`build_surface_graph`、`astar_surface_plan` 和两个 Dijkstra 参照函数仍保留原定义，以免破坏冻结证据；新诊断调用统一经过 `planning_reference`。
- 正式规划继续使用 `plan_known_snapshot` 和 `plan_known_surface_snapshot`，按搜索需要展开节点和支撑列。
- `ActionRouteExecutor.start(require_verified_gap_motion=False)` 只允许历史 B09 回归使用。正式运行保持默认值 `True`。

## 兼容层规则

旧导入路径暂时保留，因此这次整理不改变已有脚本和外部调用。兼容文件只能转发符号，不得再增加状态、算法或业务分支。新代码必须使用新的明确目录。

以后删除兼容层前，需要同时满足：

1. 仓库内没有旧路径调用；
2. 冻结证据与公开快照不再依赖该路径；
3. 对应回归已经转到现行实现；
4. 删除后运行运动导航完整检查。
