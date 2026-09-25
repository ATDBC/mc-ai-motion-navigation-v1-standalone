# Minecraft 机器人运动、导航与战斗执行底座

这个仓库是主项目按固定清单生成的源码快照。它包含当前正式实现、共享契约、配置、测试、Fabric 客户端代码和四类现行设计文档。`EXPORT-METADATA.json` 记录来源提交，`SHA256SUMS.txt` 覆盖导出的每个文件。

当前已完成 B01 至 B10、C1-A 至 C1-C，以及 C1-R 的 R0 至 R5。最新一轮工作统一了逐帧控制入口、战斗与导航会话、外力恢复和带速度的一格跨隙，并建立了可独立核验的源码快照。正式 Fabric 结果与适用边界写在 `docs/motion_navigation/acceptance/`。

## 先读什么

1. `AGENTS.md`
2. `docs/motion_navigation/stages/C1R-runtime-navigation-convergence.md`
3. `docs/motion_navigation/acceptance/C1R-runtime-navigation-convergence.md`
4. `docs/motion_navigation/architecture/runtime-navigation-convergence-v1.md`
5. `docs/motion_navigation/architecture/B10-motion-solving-continuous-execution-v1.md`

## 环境

- Python 3.11
- OpenJDK 21
- Minecraft 1.21
- Fabric Loader 0.15.11
- Fabric API 0.100.6+1.21

创建 Python 3.11 虚拟环境后安装：

```text
python -m pip install -r requirements.txt
```

Java 可以由 `JAVA_HOME` 指向 JDK 21，也可以把 JDK 21 的 `java` 与 `javac` 放入 `PATH`。检查命令不会依赖原项目的 `.venv/Library` 路径：

```text
python scripts/export_motion_navigation_standalone.py check-java
```

## 可重复检查

先验证文件集合和哈希：

```text
python scripts/export_motion_navigation_standalone.py verify --root .
```

运行动作、几何、规划与执行检查：

```text
python -m unittest discover -s tests/motion_nav -p "test_*.py" -v
```

运行 C1 正式证据读取和离线重放检查：

```text
python -m unittest tests.test_c1_melee_evidence tests.test_c1_moving_melee_evidence tests.test_c1_external_motion_evidence tests.test_c1_fixed_melee_runtime tests.test_c1_moving_melee_runtime tests.test_c1_external_motion_runtime -v
```

运行公共控制帧、仲裁、失败处置、交战记忆和战斗驱动检查：

```text
python -m unittest tests.test_action_arbiter_v1 tests.test_action_receipt tests.test_player_runtime_v1 tests.test_runtime_failure_disposition tests.test_engagement_memory tests.test_fixed_melee tests.test_fixed_melee_driver tests.test_melee_strike_driver tests.test_moving_melee tests.test_moving_melee_driver tests.test_external_motion_recovery_driver tests.test_c1_navigation_session -v
```

运行不依赖 Minecraft 类库的 Java 控制门禁：

```text
python -m unittest tests.test_standalone_java_gates -v
```

## 能说明什么

- 已知地形中的地面路线、支撑面、台阶、有限空中动作和动作接续有组件与实机证据；
- Runtime 是唯一逐帧推进者，导航、视角和攻击通过同一控制帧仲裁；
- 状态锚点、真实输入应用账本、后台求解、候选接纳和逐 tick 执行使用同一身份链；
- 带速度的一格同高跨隙已在三个入口速度带和四个正方向完成冻结验收；
- C1 战斗纵切片已覆盖固定目标、移动目标和真实受击后的恢复。

## 不能说明什么

这个快照不包含世界存档、原始轨迹、完整日志、Gradle 缓存、Minecraft 依赖 JAR 或构建产物，因此不能只靠本仓库重跑游戏内正式实验。验收文档保存精简结果和运行 ID，原始证据仍由主项目保管。

未知区域探索、攀爬、游泳、主动 Crawl、特殊地面、任意宽度跨隙和所有动作的带速衔接仍未完成。CraftGround 适配代码为历史兼容保留，后续正式实现与实机验收以独立 Fabric 为准。

公开快照能够复现其声明的源码检查，不代表项目已经没有剩余问题。
