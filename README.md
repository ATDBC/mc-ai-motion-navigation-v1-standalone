# Minecraft 机器人运动、导航与战斗执行底座

这个仓库是主项目按固定清单生成的源码快照。它包含当前正式实现、共享契约、配置、测试、Fabric 客户端代码和四类现行设计文档。`EXPORT-METADATA.json` 记录来源提交，`SHA256SUMS.txt` 覆盖导出的每个文件。

默认分支 `main` 是项目方唯一的公开发布线，只接收固定导出脚本生成并校验通过的整理后快照。主项目的完整开发分支和提交历史不会直接推入本仓库。三方审查分支可以单独存在，用于保存审查报告和复现材料；它们不改变 `main` 的正式状态。

2026-10-09，按D090—D092完成F2-R主线恢复，R0—R4已签署。Windows源树和独立公开快照正序、逆序各1661／1661，零失败／错误／跳过；43组Fabric、46个实际试次全部通过，20次首条晚1由真实应用确认，旧成功退步0。区域几何40／64；柱顶24项仍按D091保留为安全有界的Walk→JumpUp能力RED。修订8 ms目标与worker资源债未关闭。正式结果见 [F2REC阶段](docs/motion_navigation/stages/F2REC-rebuild-from-f2r.md)和[验收](docs/motion_navigation/acceptance/F2REC-rebuild-from-f2r.md)。

R2按D092同机222帧配对签署；修订8ms仍是性能债。R3已删除没有正式生产者的潜行防坠状态机，探边、Crouch和原版潜行物理保留。Runtime关闭后的Session worker资源债仍保留。原停止、夹具失败与历史能力结果不回写。当前入口为 [F2REC 阶段](docs/motion_navigation/stages/F2REC-rebuild-from-f2r.md)、[验收](docs/motion_navigation/acceptance/F2REC-rebuild-from-f2r.md)及 `evidence/motion_navigation/F2REC-recovery-v1/`。以下较早结果保留各自历史范围。

撤出的 D076—D088、F2-S 至 F2-RH 阶段与证据保留在公开历史提交 [2bad9bf](https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/tree/2bad9bf)。该提交保存归档材料，不代表当前 main 的正式能力。

2026-10-08，F2-R 已按冻结范围通过。外角、对角柱和杂乱地形中的完成区域，从原目标内的安全精确矩形中选择面积最大的一块；不使用外接矩形，不扩大原 `GoalState`。UNKNOWN 按整格最坏障碍裁剪，只允许完全已知的安全子区域。Windows 完整正逆各 1647/1647；v7 为 1998/2000，旧成功退步 0；v8 为 3817/3904，87 项失败全部保留。Fabric 两族四方向正常／首条晚1共 16/16，8 次晚1实际应用，零安全与期限违规，来源全部注销。

共享模拟器等价短路后，单次正式 D061 完整路径最大 40.1764 ms、deadline miss 为 0；原超限记录保留，夹具性能修复不记作机器人能力提升。`SNEAK_EDGE_GUARD` 仍只有组件证明，正式路线没有生产者。下一阶段为路线优化器，必须成为第一个正式区间生产者，并补必须潜行的实机证明；若更换阶段，先删除不可达状态机。现行范围与结果见 [F2-R 阶段](docs/motion_navigation/stages/F2R-piecewise-completion-and-v8.md)、[F2-R 验收](docs/motion_navigation/acceptance/F2R-piecewise-completion-and-v8.md)及 [D075](docs/motion_navigation/decisions/0075-repair-piecewise-completion-and-bound-edge-guard.md)，紧凑证据位于 `evidence/motion_navigation/F2R-piecewise-completion-v1/`。以下 F2 及更早记录保留各自的历史范围。

2026-10-07，F2 通用非中心地面路线执行与终点接近已按冻结范围验收通过。普通开阔路线保留快速路径，已知碰撞或支撑边界每帧最多复核三个候选；潜行只由路线区间授权，完成区域保持在原 `GoalState` 内。正式 Windows 完整正逆各 1627/1627，五组通过，产品 1998/2000，旧成功退步 0；玩家站位 187/400 → 400/400。Fabric 冻结清单 93/93 符合各自判定，80 正例完成、40 次首条晚一 tick 实际应用，三组路线／持续输入用时比为 1.000／1.235／1.074，连续 10 tick 非中性停滞为 0。首个静止普通 Walk 补一 tick 中性等价证明，命令租约与 Session／Runtime 生命周期未改。

范围见 [F2 阶段](docs/motion_navigation/stages/F2-non-center-ground-route-execution.md)、[架构](docs/motion_navigation/architecture/continuous-ground-route-execution-v1.md)和 [D074](docs/motion_navigation/decisions/0074-generalize-ground-route-execution-before-terminal-approach.md)，结果与限制见 [F2 验收](docs/motion_navigation/acceptance/F2-non-center-ground-route-execution.md)。紧凑证据位于 `evidence/motion_navigation/F2-ground-route-v1/final/`，原失败、逐项索引、来源和哈希均保留。外力仅按冻结试次判断；移动活塞观察、动态避障、路线优化器、疾跑路线和新动作尚未交付。

Windows 是正式开发、自动验收和 Fabric 实机平台；Linux 仅用于补充可移植性复核。公开快照不包含本地 `.tmp`、`artifacts`、凭据、世界存档或原始大型轨迹。F2 的详细轨迹比较和 Fabric 原始流审计仍需主项目保管的原始证据，紧凑摘要不能代替完整观察。`EXPORT-METADATA.json` 的来源提交标识本次公开整理；历史验收采集提交和当时的 dirty 状态仍按原记录保存。

历史 M0 通过，M1 结构止损，未通过；动作接口迁移保留，后续接口加固和 Driver 枚举生命周期整改已经完成。原 S1—S4 不再实施，M2—M5 未授权。原结果分别见 [M0／M1 验收](docs/motion_navigation/acceptance/motion-navigation-middle-layer-M0-M1.md)、[加固验收](docs/motion_navigation/acceptance/action-spec-hardening-driver-lifecycle.md)和 [D073](docs/motion_navigation/decisions/0073-accept-action-spec-and-retire-size-gates.md)，历史失败不回写。

历史 F1 按“功能通过、结构未通过”保存；R28 按“时间盒结束，部分交付通过”保存。历史 S0-R、v1—v7、R27 和连续高度 M3 的结果与边界均保留在各自验收及 `evidence/motion_navigation/` 中，不用 F2 的新结果覆盖。历史入口见 [F1 验收](docs/motion_navigation/acceptance/F1-known-world-following.md)、[R28 验收](docs/motion_navigation/acceptance/navigation-coordination-convergence.md)和 [S0-R 验收](docs/motion_navigation/acceptance/post-F1-navigation-structure-cleanup.md)。

当前已完成 B01 至 B10、C1-A 至 C1-C、C1-R 的 R0 至 R6、B11 固定放置与有限搭桥，以及 B12-A、B12-B。B12-A 固定了每次攻击的证据和分类重试，并接入 Minecraft 1.21 伤害来源事实。B12-B 让战斗先确定本帧视角，导航再按最终视角计算普通地面移动；追逐阶段会持续给出目标视角，导航同时记录每帧的移动决定原因。活动目标和真实墙体场景补齐了持续瞄准、遮挡后的导航、补看和权限撤销。动作证明的生效窗口修正和客户端额外推进一个 tick 时的局部安全恢复也已包含。正式 Fabric 结果与适用边界写在 `docs/motion_navigation/acceptance/`。

导航协调的 S0 至 S5 已实施。2026-10-01，R27 复审整改 A0—A5 完成，S5 按本轮冻结范围重新关闭。规划、motion 和放置复用同一生命周期入口；业务终态后仍可请求身体停止；旧信息通知自行携带身份；四个模拟工具共同拒绝缺证据。完整运动导航 847/847，原 1000 序列及独立新增 256 个异步组合符合各自冻结检查，8 个旧错误副本均被行为断言发现。完整父层 B11 实机停止及正常对照 20/20、原正例 60/60、反例 24/24。当前分组、失败记录和实机边界写在协调验收第 20 节，原检查和独立复审保留在第 17、18 节。

`evidence/motion_navigation/r27-v1/` 提供本轮结构化汇总，以及一份完整 B11 通过归档和一份尾迹超时失败归档，两份压缩数据合计约 220 KiB。原有 `representative-v1` 归档保持不变。新归档保留试次、控制帧、实际应用采样、释放依据及尾迹；测试注入和源码冻结边界分别说明，便于外部核对。

连续高度 M3 已关闭。完整 Fabric 矩阵覆盖 800 个唯一试次：正常输入 400/400 完成；20% 随机晚一帧为 384/400 完成，另外 16 场均按冻结口径有界失败；伤害额度超出和不变量违规均为 0。半砖、楼梯、土径、地毯和不超过半格的积雪统一作为普通步行候选，再由同一个 1.21 运动计算器验证真实高度、净空和扫掠结果。

2026-09-26 起，正式 Fabric 方块视觉只接受 profile 4 表面深度。旧 profile 3 稀疏射线只能读取历史记录，不能启动正式会话，也不能形成新的验收结论。`evidence/motion_navigation/representative-v1` 保留九份带有 `historical_legacy_ray_profile3` 标签的旧归档，并加入一份 `current_surface_depth_profile4` 的 B12-B 当前代表批次。具体边界见 `docs/motion_navigation/decisions/0035-formal-surface-depth-only.md`。

主仓库仍保留 CraftGround、`legacy_ray_profile3` 和旧轨迹读取代码，以便复现历史结果。它们的采集器、运行入口、导航审计和测试不会进入这个当前实现仓库。V3 仍复用少量早期版本中已经冻结的通用数据类型和 JSON 校验函数；正式后端随后强制检查 profile 4，不能因此启动旧射线。正式 Fabric 构建门禁还会检查旧采集器没有进入客户端 JAR。

## 先读什么

1. `AGENTS.md`
2. [F2REC 阶段](docs/motion_navigation/stages/F2REC-rebuild-from-f2r.md)
3. [F2REC 验收](docs/motion_navigation/acceptance/F2REC-rebuild-from-f2r.md)、[D090](docs/motion_navigation/decisions/0090-rebuild-main-from-f2r-and-recover-only-validated-parts.md)和 [D091](docs/motion_navigation/decisions/0091-sign-region-geometry-and-defer-column-top-handoff.md)
4. `docs/motion_navigation/architecture/continuous-ground-route-execution-v1.md` 和 D074；基础历史见 [F2 阶段](docs/motion_navigation/stages/F2-non-center-ground-route-execution.md)与 [F2 验收](docs/motion_navigation/acceptance/F2-non-center-ground-route-execution.md)。
5. `docs/motion_navigation/architecture/navigation-coordination-v1.md`（第 17 节包含当前 R28 目标和已实施的持续目标语义）
6. 当前实现和证据读 R27 阶段文档、`navigation-coordination-refactor.md` 第 20 节与 `defect-ledger.md`。
7. 连续高度与物理范围读 M3、B09-R、B10 的对应 architecture 和 acceptance。
8. 需要检查战斗或部分观察时，再读对应的 B12、C1 文档。

## 环境

- Python 3.11
- NumPy 2.4.6
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

正式 Fabric 客户端还需要 CMake、C++ 构建器和 JDK 21。下面的命令会先从仓库内固定源码构建表面深度原生库，执行正式 JNI 冒烟检查，再离线构建 Fabric 模组。当前阶段只验收 Windows。离线 Fabric 构建还要求仓库根目录已经有固定的 Gradle 8.8 与 Minecraft 依赖缓存；公开快照不上传这部分缓存。没有缓存时仍可运行下面的源码、Python 和纯 Java 检查，但不能据此声称已经重建 Fabric 模组。

```text
python scripts/build_fabric_deployment_probe.py
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

快速检查 F2 的路线、潜行区间、原目标完成区域和正式启动窗口：

```text
python -m unittest tests.motion_nav.test_f2_ground_route_gates tests.motion_nav.test_f2_non_center_ground_route tests.motion_nav.test_f2rec_r3_cleanup tests.motion_nav.test_f2_ground_completion_region tests.motion_nav.test_f2_ground_start_window -q
```

重新生成 F2 的组件与玩家站位量尺时，必须使用新的空输出目录：

```text
python scripts/f2_ground_route_evidence.py --output output/f2-ground-route --workers 4
```

F2 的完整历史命令与采集环境保存在 `final/README.md`。其中 `reproduce/run_windows_checks.py` 沿用主项目 `.venv/python.exe` 路径；公开环境可按上述命令直接运行源码检查。历史详细对比与实机审计需要未公开的原始流，不属于仅凭公开快照可重跑的检查。

快速检查 D055 的真实准备、后台复核、启动交付及首条晚到：

```text
python -m unittest tests.motion_nav.test_motion_start_delivery tests.motion_nav.test_action_continuity_formal tests.motion_nav.test_async_gap_revalidation_delivery -q
```

v6 原始归档的完整对比与 Fabric 命令维护在验收第 14.7 节。Fabric 仍需本地固定依赖和独立测试服务器，公开快照不含游戏世界或启动凭据。

重新生成连续高度正式调用链组件矩阵和规划代价对照时，必须使用新的空输出目录：

```text
python scripts/run_continuous_height_matrix.py --output output/continuous-height-matrix --condition both --scope all
python scripts/run_continuous_height_planning_matrix.py --output output/continuous-height-planning-matrix
```

运行固定的导航随机事件序列时，也必须使用新的空输出目录：

```text
python -m tests.sim.run_navigation_event_sequences --manifest tests/sim/manifests/navigation-coordination-event-sequences.json --output-root output/navigation-event-sequences
```

运行 R27 新增的异步组合和验证门禁检查：

```text
python -m unittest tests.motion_nav.test_async_work_verification -v
python -m tests.sim.run_async_work_sequences --manifest tests/sim/manifests/navigation-async-work-r27-hardening.json --output-root output/navigation-async-work-sequences
python -m tests.sim.run_async_admission_mutations --output output/navigation-async-admission-mutations
```

任务结果和证据结论分别报告。任务已经完成但缺少应有异步记录时，仍不得通过验收；没有异步工作的场景必须预先声明，并由实际调用记录核对。

运行 C1 正式证据读取和离线重放检查：

```text
python -m unittest tests.test_c1_melee_evidence tests.test_c1_moving_melee_evidence tests.test_c1_external_motion_evidence tests.test_c1_fixed_melee_runtime tests.test_c1_moving_melee_runtime tests.test_c1_external_motion_runtime -v
```

运行公共控制帧、仲裁、失败处置、交战记忆和战斗驱动检查：

```text
python -m unittest tests.test_action_arbiter_v1 tests.test_action_receipt tests.test_player_runtime_v1 tests.test_runtime_failure_disposition tests.test_engagement_memory tests.test_fixed_melee tests.test_fixed_melee_driver tests.test_melee_strike_driver tests.test_moving_melee tests.test_moving_melee_driver tests.test_external_motion_recovery_driver tests.test_c1_navigation_session -v
```

运行 B10、B11、B12 的正式探针契约与部署入口检查：

```text
python -m unittest tests.test_b10_runtime_probe tests.test_b11_world_change_runtime tests.test_b12_attack_evidence_runtime tests.test_b12a_fabric_runtime tests.test_b12a_runtime_injection_acceptance tests.test_b12b_partial_combat_runtime tests.test_b12b_runtime_injection_acceptance tests.test_fabric_deployment_probe -v
```

运行不依赖 Minecraft 类库的 Java 控制门禁：

```text
python -m unittest tests.test_standalone_java_gates -v
```

不依赖 Minecraft／Gradle 缓存的表面 Java、协议和空气候选门禁：

```text
python -m unittest tests.test_surface_depth_tiles tests.test_surface_observation_v3 tests.test_visual_air_runtime -v
```

当前阶段的原生表面核心只在 Windows 验收。先构建固定源码，再运行容量剔除、遮挡和 500 个随机世界空气零误判检查：

```text
python -c "from scripts.surface_depth_probe.build import build_native; print(build_native())"
python -m unittest tests.test_surface_depth_cache -v
```

`tests.test_client_block_observation_v3` 会编译接入 Minecraft 1.21 类型的观察类，因此和完整 Fabric 构建一样，需要仓库根目录已有固定 Gradle 与 Minecraft 依赖缓存；具备缓存时把它作为正式门禁一并运行。缺少缓存时可以运行上面的源码、协议和原生核心检查，但不能声称已经重建 Fabric 观察入口。

这些检查直接覆盖当前 profile 4 实现。旧射线采集器和退役诊断入口不在公开清单中；公开包中的第一方 Python 模块还会由 `tests/motion_nav/test_standalone_export.py` 逐个导入，避免留下缺少依赖的失效入口。

核对十个代表性真实运行批次：

```text
python scripts/public_runtime_evidence.py verify --root evidence/motion_navigation/representative-v1
```

前六个批次分别覆盖 B10-C 跨隙、C1-B 移动近战和 C1-C 外力恢复，每类各有一个完整通过批次和一个完整失败批次。第七至第九个批次分别保存 B11、B12-A 和旧 B12-B 的历史结果。第十个批次是当前 profile 4 的 B12-B 完整通过记录，保留 34 个场景、两种活动目标试次、导航决定原因、真实墙体遮挡、控制事件和分段 Runtime 轨迹。验证器会检查归档哈希、安全边界、视觉版本、完整试次数量、持续追击指标、汇总、时延门槛和失败分类。压缩归档合计不超过 30 MiB。

## 能说明什么

- 已知地形中的地面路线、支撑面、台阶、有限空中动作和动作接续有组件与实机证据；
- Runtime 是唯一逐帧推进者，导航、视角和攻击通过同一控制帧仲裁；
- 状态锚点、真实输入应用账本、后台求解、候选接纳和逐 tick 执行使用同一身份链；
- 带速度的一格同高跨隙已在三个入口速度带和四个正方向完成冻结验收；
- C1 战斗纵切片已覆盖固定目标、移动目标和真实受击后的恢复；
- B11 已覆盖单块放置、一至三格直桥和十二类边界与反例；
- B12-A 已覆盖单次攻击证据、分类重试和伤害来源，B12-B 已覆盖按最终战斗视角计算的普通地面移动、活动目标持续瞄准和部分观察边界；
- 导航协调层的身体责任、前置条件、重试与伤害额度已经有正式路径闭环门禁；
- 连续高度组件矩阵已覆盖已声明的小高差与整格动作，并保留随机迟到造成的有界安全失败；
- 公开仓库附带十个结构化真实运行批次，可以重新统计历史结果，并核对 B12-B 当前 profile 4 结果。

## 不能说明什么

这个快照不包含世界存档、完整普通日志、画面、Gradle 缓存、Minecraft 依赖 JAR 或构建产物，因此不能只靠本仓库重跑游戏内正式实验。公开的十个真实批次是经过筛选的结构化轨迹，可以核对历史结果、当前 B12-B 结果和读取链，但不能替代新的 Fabric 实机运行。其他原始证据仍由主项目保管。

未知区域探索、攀爬、游泳、主动 Crawl、特殊地面、任意宽度跨隙和尚未声明的动作接续仍未完成。当前独立仓库只包含正式 Fabric 路径；CraftGround 适配和旧射线兼容代码只在主仓库保留。

公开快照能够复现其声明的源码检查，不代表项目已经没有剩余问题。
