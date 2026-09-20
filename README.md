# 运动／导航新主线：B01–B09

本仓库是 Minecraft 机器人运动／导航重构在 B09 完成时的独立源码快照。本次从 B05 发布提交 `fdad6e3` 继续更新到本地来源提交 `5d6a0de`。仓库只保存新版实现及其必要文档、配置、测试和运行脚本，不包含旧导航版本。

## 已实现范围

- B01：固定三套旧实现与统一比较口径；
- B02：统一世界会话、时间、未知／空气／方块、三维支撑和连续扫掠；
- B03：固定路线步行、制动、取消和输入失联；
- B04：已知图 A*、后台规划进程、路线接纳和执行走廊；
- B05：经 Fabric 校准的一格上升 `JumpUp`；
- B06：冻结运行环境、目标／资源／动作转换契约和普通材质等价类；
- B07：连续支撑面、非完整碰撞形状、半格台阶和有类型的 `Step`；
- B08：Walk、Sprint、Crouch，以及合法预置后的 Crawl；
- B09：同高一格跨隙 `JumpGap`、相邻一格零伤害下降 `ControlledDrop`，并接入后台选路、路线接纳和执行。

B09 Fabric 验收完成 80/80 次空中动作：跨隙和受控下降各 40 次。两条 `Walk—空中动作—Walk` 混合路线均成功执行。提交后的运动／导航测试共 202 项，全部通过。

## 目录

- `mc2p/motion_nav/`：世界知识、几何、能力查询、规划、路线接纳和动作执行；
- `config/motion-navigation/`：环境身份、材质分类、地面模式和空中动作 Profile；
- `tests/motion_nav/`：核心行为、失败边界和回归测试；
- `scripts/`：配置导出、Fabric 校准和阶段实机验收入口；
- `docs/motion_navigation/architecture/`：模块职责、接口、状态归属和不变量；
- `docs/motion_navigation/stages/`：阶段范围与完成状态；
- `docs/motion_navigation/acceptance/`：场景、指标、结果和证据边界；
- `docs/motion_navigation/decisions/`：设计与验收取舍；
- `reports/current-performance-and-limitations.md`：当前表现和能力边界。

## 推荐阅读顺序

1. `docs/motion_navigation/architecture/mc_motion_navigation_architecture_v1.md`
2. `docs/motion_navigation/architecture/mc_navigation_B06_B15_plan_java_1_21_0.md`
3. `docs/motion_navigation/architecture/movement-transitions-v1.md`
4. `docs/motion_navigation/architecture/support-surfaces-v1.md`
5. `docs/motion_navigation/architecture/ground-modes-v1.md`
6. `docs/motion_navigation/architecture/air-transitions-v1.md`
7. `docs/motion_navigation/acceptance/B09-parameterized-air-transitions.md`

## 范围说明

这仍是负责运动／导航的源码快照，不是可独立启动的完整 Minecraft 工程。运行实机脚本还需要原项目的 contracts、runtime、Fabric 接入和证据工具。仓库没有上传游戏世界、原始轨迹、日志、回放、依赖缓存或构建产物。

当前空中动作只覆盖 Minecraft 1.21、普通地面、低速稳定入口、同高一格跨隙和相邻一格零伤害下降。带速连续接续属于 B10；特殊地面、攀爬、游泳和混合动作效率分别留给 B11–B14。
