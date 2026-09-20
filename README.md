# 运动／导航新主线：B01–B09-R

本仓库是 Minecraft 机器人运动／导航重构在 B09-R 完成时的独立源码快照。当前实现来源于本地提交 `5dad918`，并保留远端原有提交历史。仓库保存新版实现及其必要文档、配置、测试和诊断工具，不包含旧导航版本、游戏世界、原始轨迹、日志或构建产物。

## 已实现范围

- B01–B05：统一数据与几何、固定路线步行、已知图后台规划和一格上升；
- B06–B07：版本契约、普通材质、连续支撑面、非完整碰撞形状和半格台阶；
- B08：Walk、Sprint、Crouch，以及合法预置后的 Crawl；
- B09：同高一格跨隙 `JumpGap` 和相邻一格零伤害下降 `ControlledDrop`；
- B09-R：给定完整身体状态、逐 tick 输入和已知世界，计算下一 tick 或一段固定输入的运动后果。

B09-R 是无副作用的运动计算器。它返回位置、速度、姿态、碰撞事件、世界依赖和明确的失败分类；它不搜索输入、不选择路线，也没有接管在线控制。

## B09-R 实测结果

- B08 Walk／Sprint／Crouch：1,298 tick，81 段开环，无分叉；
- B08 合法 Crawl：602 tick，23 段开环，无分叉；
- B09 空中转换：1,948 tick，115 段开环，无分叉；
- B09 最大开环位置误差约 `5.44e-7` 格，最大速度误差约 `3.13e-8` 格／tick；
- 运动／导航专项 252 项通过，B09-R 直接相关检查 41 项通过。

完整数值、证据边界和尚未补采的场景见 `docs/motion_navigation/acceptance/B09R-physics-calculator.md`。

## 目录

- `mc2p/motion_nav/`：世界知识、几何、能力查询、规划、动作执行和 B09-R 计算器；
- `config/motion-navigation/`：环境身份、材质分类、地面模式和空中动作 Profile；
- `tests/motion_nav/`：核心行为、失败边界和回归测试；
- `scripts/`：配置导出、Fabric 校准、物理差分和性能基准；
- `docs/motion_navigation/architecture/`：模块职责、接口、状态归属和不变量；
- `docs/motion_navigation/stages/`：阶段范围与完成状态；
- `docs/motion_navigation/acceptance/`：场景、指标、结果和证据边界；
- `docs/motion_navigation/decisions/`：设计与验收取舍；
- `tools/navigation_replay/`：只读回放工具，包括 B09-R 实机／计算结果对照页。

## 推荐阅读顺序

1. `docs/motion_navigation/architecture/mc_motion_navigation_architecture_v1.md`
2. `docs/motion_navigation/architecture/physics-calculator-1_21-v1.md`
3. `docs/motion_navigation/stages/B09R-physics-calculator.md`
4. `docs/motion_navigation/acceptance/B09R-physics-calculator.md`
5. `docs/motion_navigation/decisions/0011-b09r-calculator-before-continuous-handoff.md`

## 回放页

原工程存在封存的 B08／B09 Fabric 证据时，可以启动 `tools/navigation_replay/server.py` 并打开 `/physics.html`，逐 tick 对比实机轨迹和计算轨迹。页面显示俯视轨迹、高度变化、放大误差、输入、姿态和碰撞事件。

仓库没有上传原始实机证据，因此单独克隆本仓库后只能查看实现和测试，不能直接重放历史实验。

## 范围说明

这仍是负责运动／导航的源码快照，不是可独立启动的完整 Minecraft 工程。实机脚本还需要原项目的 contracts、runtime、Fabric 接入和证据工具。

B09-R 当前只计算给定输入的后果。输入搜索、带速连续动作接续、特殊地面、攀爬、游泳和完整资源时钟仍属于后续阶段。
