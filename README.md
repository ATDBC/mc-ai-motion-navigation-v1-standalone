# Minecraft 机器人运动／导航：B01–B10

本仓库保存当前运动／导航主线的独立源码快照。来源为本地 `refactor/motion-navigation-v1` 分支提交 `ad8a550`。内容包括现行实现、必要配置、测试、Fabric 接入代码、四类设计文档和只读回放工具；不包含旧导航版本、世界存档、原始轨迹、日志或构建产物。

## 当前进度

B01 至 B10 已在冻结范围内完成：

- B01–B05：参照版本、统一数据与几何、固定路线步行、已知图后台规划和一格上升；
- B06–B09：普通材质、连续支撑面、Walk／Sprint／Crouch／合法预置 Crawl、同高一格跨隙和相邻一格零伤害下降；
- B09-R：给定身体状态、世界事实和逐 tick 输入，计算并验证运动后果；
- B10-A：状态锚点、真实输入应用账本、命令投影和在线预算；
- B10-B：受控入口下自动求解同高一格跨隙命令；
- B10-C：后台求解、当前状态接纳、回执驱动执行、八行连续动作矩阵和默认协调入口。

当前表面规划按需生成支撑与动作边。同一对位置可以保留不同动作，`PlannerStateKey` 会区分影响后续动作的模式、姿态、方向和速度区间。100×100 已知平地基准展开 198 个节点，20 次运行 P95 为 87.3973 ms，低于 500 ms 门槛。

B10-C 的默认 Fabric 协调链为 10／10；冻结的八行接续矩阵均有连续正例、边界检查和中断证据。当前运动导航专项测试为 314 项通过。

运行代码、离线证据工具和历史兼容实现已经分开：正式代码位于 `mc2p/motion_nav/`，离线工具位于 `mc2p/motion_nav/evidence/`，已被替代但仍需保留的实现位于 `mc2p/motion_nav/legacy/`。物化图和 Dijkstra 参照入口集中在 `planning_reference.py`，不进入正式大地图热路径。旧导入路径暂时保留为薄兼容层。

## 推荐阅读顺序

1. `AGENTS.md`
2. `docs/motion_navigation/stages/B10C-planning-continuous-execution.md`
3. `docs/motion_navigation/acceptance/B10C-planning-continuous-execution.md`
4. `docs/motion_navigation/architecture/B10-motion-solving-continuous-execution-v1.md`
5. `docs/motion_navigation/architecture/package-boundaries-v1.md`
6. `docs/motion_navigation/architecture/mc_motion_navigation_architecture_v1.md`

文档只维护四类：

- `architecture/`：职责、接口、状态归属和长期不变量；
- `stages/`：阶段范围、完成状态和下一阶段条件；
- `acceptance/`：场景、指标、结果与证据边界；
- `decisions/`：设计顺序和验收门槛的变更原因。

已经被稳定架构和阶段记录取代的长方案不再放入仓库，避免旧状态与当前实现并存。

## 目录

- `mc2p/motion_nav/`：世界知识、几何、规划、动作求解、候选接纳和执行；
- `mc2p/motion_nav/evidence/`：离线校准、差分、快照校验和验收计算；
- `mc2p/motion_nav/legacy/`：已被正式实现替代但仍需兼容旧证据的代码；
- `config/motion-navigation/`：环境身份、材质、地面模式和动作 Profile；
- `tests/motion_nav/`：行为、错误边界、回归和性能工具测试；
- `scripts/`：Fabric 验收、差分和性能基准；
- `docs/motion_navigation/`：现行四类文档；
- `tools/navigation_replay/`：只读回放与物理对照工具。

## 当前边界

本仓库仍是运动／导航源码快照，不是可以单独启动的完整 Minecraft 工程。实机脚本需要原项目的 contracts、runtime、Fabric 启动和证据工具。

B10 首版不包含带速起跳、Sprint 直接进入 JumpGap、主动 Crawl、特殊地面、攀爬、流体、未知探索、动态实体或主动改变世界。新增能力必须继续沿用同一份地图、目标、动作许可和输入出口。

## 校验

`SHA256SUMS.txt` 覆盖本次公开快照中的所有文件，文件自身除外。正式测试命令和环境版本见 `AGENTS.md`。
