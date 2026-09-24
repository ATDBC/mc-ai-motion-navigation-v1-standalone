# Minecraft 机器人运动／导航与 C1 战斗纵切片

本仓库保存运动／导航主线的独立源码快照。当前内容同步自本地 `refactor/motion-navigation-v1` 分支提交 `dd30c36`。仓库包含现行实现、必要共享契约、配置、测试、Fabric 接入代码、四类设计文档和精简正式实验报告；不包含世界存档、完整原始轨迹、日志或构建产物。

## 当前进度

B01 至 B10 已在冻结范围内完成：

- B01–B05：参照版本、统一数据与几何、固定路线步行、已知图后台规划和一格上升；
- B06–B09：普通材质、连续支撑面、Walk／Sprint／Crouch／合法预置 Crawl、同高一格跨隙和相邻一格零伤害下降；
- B09-R：给定身体状态、世界事实和逐 tick 输入，计算并验证运动后果；
- B10：状态锚点、输入应用账本、动作求解、规划接纳、后台协调和连续执行。

C1 战斗纵切片也已完成当前冻结范围：

- C1-A：固定可见目标的单次近战；
- C1-B：移动目标的追击、重复攻击和死亡确认；
- C1-C：真实受击后的动作失效、唯一恢复责任、重新锚定和继续追击。

2026-09-24 的当前源码正式结果：

- C1-B：20／20 正例、10／10 反例通过；控制计算 P95 5.7939 ms，P99 7.1828 ms；
- C1-C：20／20 正例、10／10 反例通过；控制计算 P95 5.7396 ms，P99 7.4549 ms；受击到恢复输入实际应用 P99 49.3256 ms；
- 运动／导航专项 345 项、B10—C1-C 相关回归 282 项、Java 输入与执行门禁 6 项通过；
- Astra 最终独立审查通过，没有剩余阻断问题。

## 推荐阅读顺序

1. `AGENTS.md`
2. `docs/motion_navigation/stages/C1-fixed-visible-melee.md`
3. `docs/motion_navigation/architecture/C1-combat-vertical-slice-v1.md`
4. `docs/motion_navigation/acceptance/C1B-moving-target-melee.md`
5. `docs/motion_navigation/acceptance/C1C-external-motion-recovery.md`
6. `docs/motion_navigation/architecture/B10-motion-solving-continuous-execution-v1.md`

文档只在以下四个目录维护：

- `architecture/`：职责、接口、状态归属和长期不变量；
- `stages/`：阶段范围、完成状态和进入下一阶段的条件；
- `acceptance/`：场景、指标、结果和证据边界；
- `decisions/`：设计顺序和验收门槛的变更原因。

## 目录

- `mc2p/motion_nav/`：世界知识、几何、规划、动作求解、候选接纳和执行；
- `mc2p/skills/`：目标、导航、近战和恢复驱动器；
- `mc2p/runtime/`：动作仲裁、唯一输入出口和运行记录；
- `mc2p/backends/`：正式观察、Fabric 行为协议和客户端实现；
- `deployment/fabric-c1-fixture-server/`：固定种子、移动目标和受控原版攻击的验收夹具；
- `tests/`：行为、错误边界、重放和 Java 门禁；
- `reports/fabric-acceptance/`：精简正式报告、运行清单与重放结论；
- `reviews/`：外部审查意见，不属于四类正式文档，不改变阶段结论。

## 结论边界

本仓库仍是源码快照，不是可以单独启动的完整 Minecraft 工程。实机脚本需要原项目的 Fabric 启动、服务器和证据环境。

现有结论只覆盖 B10 已声明的动作组合，以及已知完整平地中的单个普通成年僵尸。它不证明复杂地形、多敌人、实体挤压、活塞、爆炸、远程攻击或未知区域探索已经安全。

## 校验

`SHA256SUMS.txt` 覆盖本次公开快照中的所有文件，文件自身和 `.git` 目录除外。正式命令和版本见 `AGENTS.md`。
