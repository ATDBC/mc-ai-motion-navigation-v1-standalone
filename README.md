# 运动／导航新主线：B01–B05

本仓库是 Minecraft 机器人运动／导航重构在 B05 完成时的独立源码快照，来源提交为 `1114d84`。仓库只保存新版实现及其必要文档、配置、测试和运行脚本，不包含旧导航版本。

## 已实现范围

- B01：固定三套旧实现与统一比较口径；
- B02：统一世界会话、时间、未知／空气／方块、三维支撑和连续扫掠；
- B03：固定路线步行、制动、取消和输入失联；
- B04：已知图 A*、后台规划进程、路线接纳和执行走廊；
- B05：经 Fabric 校准的一格上升 `JumpUp`，以及 Walk—JumpUp—Walk 动作路线。

B05 的正式结果是 100/100 次有效上跳成功。四个正方向各 25 次，最大落地水平误差为 `0.168552` 格。1542 个控制计算样本的 P95 为 `0.3452 ms`，P99 为 `0.3623 ms`，最大值为 `0.4412 ms`。

## 目录

- `mc2p/motion_nav/`：世界知识、几何、地面控制、固定路线、已知图规划、后台进程、路线接纳和 JumpUp；
- `config/motion-navigation/`：普通地面和 JumpUp 的版本化实测参数；
- `tests/motion_nav/`：核心行为、失败边界和回归测试；
- `scripts/`：校准、基准和 Fabric 实机运行核心；
- `docs/motion_navigation/architecture/`：模块职责、接口、状态归属和不变量；
- `docs/motion_navigation/stages/`：B01–B05 的阶段范围和完成状态；
- `docs/motion_navigation/acceptance/`：场景、指标、结果和证据边界；
- `docs/motion_navigation/decisions/`：设计与验收取舍。
- `reports/current-performance-and-limitations.md`：当前表现、已修问题和能力边界。

## 推荐阅读顺序

1. `docs/motion_navigation/architecture/mc_motion_navigation_architecture_v1.md`
2. `docs/motion_navigation/architecture/world-knowledge-geometry-v1.md`
3. `docs/motion_navigation/architecture/fixed-route-walk-v1.md`
4. `docs/motion_navigation/architecture/known-map-background-planning-v1.md`
5. `docs/motion_navigation/architecture/jump-up-v1.md`
6. `docs/motion_navigation/acceptance/B05-minimal-height-transition.md`

## 范围说明

这仍是负责运动／导航的源码快照，不是可独立启动的完整 Minecraft 工程。运行实机脚本还需要原项目的 contracts、runtime、Fabric 接入和证据工具。仓库没有上传游戏世界、原始轨迹、日志、回放、依赖缓存或构建产物。

当前 JumpUp 只支持 Minecraft 1.21、站立、普通草方块、低速和水平方向相邻一格上升。它不证明跨坑、下降、跑跳、连续跳跃、半砖、楼梯、冰面或动态障碍已经可用。
