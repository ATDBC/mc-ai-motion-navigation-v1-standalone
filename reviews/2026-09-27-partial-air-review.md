# 部分可见空气修正复审（第十五轮）

日期：2026-09-27
审查对象：`origin/main` 提交 [`4b9e73e`][base]（视觉空气改为“有效露出区域”、四种查询结果、未加载区块、事实优先级、信息视角等待上限、路线依赖复查、解码缓存 LRU、B10 滑行缺样本）
性质：外部审查意见，不属于 `docs/motion_navigation/` 四类正式文档，不改变任何阶段结论或验收门槛
前几轮：[第十四轮](2026-09-27-b12-remediation-review.md)、[第十三轮（D035）](2026-09-26-d035-review.md)，更早的见各文件开头

本文代码链接都指向 `4b9e73e`。本分支仍基于旧快照，请不要按本分支的相对路径查看代码。

## 结论

1. **上一轮的问题大多已按新语义修好，测试通过。**
   - 已核实：
     - 空气改为部分可见判定，结果分为四种；
     - 未加载区块不再给出空气；
     - 已知半砖、楼梯和屏障不会被视觉空气覆盖；
     - 信息视角按传感器返回的状态决定是否转头；
     - “完全遮挡”有等待上限；
     - 路线依赖会复查；
     - 解码缓存改为 LRU：行走 90 秒后每帧 2.32 ms，不用缓存为 3.38 ms；
     - 滑行阶段样本缺失或迟到时进入恢复。
   - 运动导航 475 项测试通过。在 Linux 上用同一份原生源码编译后，12 项原生门禁全部通过。
2. **B09 跨隙矩阵：原因已定位，是测试夹具没有向上看，不是结构性看不到（第二节）。**
   - 起跳时头顶最高到 feet+3.05（跳跃最高点 1.252 加身高 1.8），所以动作查询需要起跳点正上方、脚底往上第 3 格：(0, feet+3, 0)。
   - 这一格从起跳位置抬头就能看到：仰角 30° 或 60° 都能确认；平视时它在视野外，返回的状态是“视野外”，不是“遮挡”。
   - 夹具的预观察都是向下看支撑，跨隙试次又只在平视状态下重发请求，所以这一格始终未知。
   - 只有跨隙需要这一格，受控下降不需要。因此 40 次跨隙全部失败、40 次下降全部通过，与实测一致。
   - 我按夹具实际使用的视角和请求逐一重放，四个方向都只缺这一格。
3. **B07 连续路线：原因没有完全定位，但范围缩小了（第三节）。**
   - 我用仓库的规划、接纳、路线执行器和 1.21 运动计算器搭了闭环：世界完全已知时，Walk—Step—Step—Walk 可以完成；有 1 tick 输入延迟、20% 指令晚到时，900 次也全部完成。
   - 所以原因不在感知，也不在规划或控制器本身。`ordinary_ground_state_lost` 只可能来自某个 Walk 段里身体真的离地或姿态不是站立。
   - 失败那次重复的逐帧样本没有保存：脚本在写入之前就抛出了异常。现有证据无法区分几种可能。
   - 模拟里看到的相关现象：
     - 1 tick 延迟时，Step 会越过半砖冲上更高一格，再往回走、从边缘掉下来，悬空 3 tick；
     - 2 tick 延迟时，第一段 Walk 会冲上半砖，报告“层高不符”。
4. **新问题一（中高）：16 格边界处，完整方块会被当成空气（第四节）。**
   - 空气判定用“格子最近点在 16 格内”，方块判定却要求“露出部分在 16 格内”。
   - 60 个随机世界里有 57 次，客户端对一个装着完整方块的格子给出了视觉空气，全部在 15—16 格。
   - 现在完整方块允许被视觉空气撤销，所以路线复查时，远处的真实支撑可能被删掉。
5. **新问题二（中）：远处缺失格让信息等待失去上限（第五节）。**
   - 原生结果 2 同时表示“视野外”和“超出 16 格”，都被报告为 `outside_view`。
   - 信息视角会一直朝这个格子转头，每帧都重置 40 帧计数，所以永远不会超时。
   - 用上游测试夹具复现：200 帧里提出了 200 次转头，状态始终是“需要信息”。
6. **新问题三（中低）：**
   - **“稳定区域”阈值其实是数值误差 `1e-12`。** 约 1.2% 的空气判断，依据的露出面积不到 854×480 画面的一个像素。
   - **下行格子的底部危险：** 从远处只看到下行格子的上部就能确认空气；低矮的流动熔岩或篝火可能藏在底部看不到的部分。建议下行目标要求底部也有露出区域。
   - 花盆被整体改成可透视，方向反了；规则测试把蓝冰写成非不透明方块。
   - 注释里的“40 帧”实际约为 200 个控制帧。
   - 公开仓库缺少 `tests/test_client_observation_payload_v3.py`。

## 审查范围

- 阅读 `4b9e73e` 相对 `b740098` 的全部代码改动、修正计划、D004、D035 和验收记录。
- 在 Linux 上用仓库原生源码（只替换 Windows 导出宏）编译测试程序，检查新的空气判定。
- 用仓库的规划、执行器和运动计算器搭闭环模拟，定位 B07；按 B09 夹具的真实视角和请求重放，定位 B09。
- 运行仓库测试（第九节）。没有 Minecraft 运行环境，涉及原版渲染类型的问题沿用上一轮的说明。

## 一、已核实的修复

| 第十四轮问题 | 现在的做法 | 核实方式 |
|---|---|---|
| 下行格子永远无法整格确认 | 假想方块在裁掉视野外部分和遮挡后，只要还有露出面积，同格也没有报告方块，就判为空气 | 读代码；原生门禁通过；第二节重放里，下行和坑格都能确认 |
| 未加载区块被当成空气 | 调用原生之前检查区块已加载、高度在世界范围内（[`SurfaceSensor.java:163`][chunk]） | 读代码 |
| 视野外、遮挡、未加载无法区分 | 返回四种结果，Python 按状态处理 | 读代码；契约测试 |
| 已知方块从不复查 | 路线依赖每帧最多 16 格、每格 20 tick 复查一次 | 读代码 |
| 屏障会被来回改写 | 屏障、光源方块、结构空位和非完整方块不会被视觉空气覆盖（[`observed_block_adapter.py:16`][hidden]） | 读代码 |
| 信息视角按格心判断 | 只对传感器报告为 `outside_view` 的格子转头；遮挡或未加载不转头 | 读代码；另见第五节 |
| 无限等待 | 完全遮挡超过上限后返回“需要更换观察位置” | 上游测试；另见第五节 |
| 解码缓存满后变慢 | 改为 LRU | 用上轮脚本在新代码上重测：行走后 2.32 ms，不用缓存 3.38 ms |
| 滑行缺样本当中性 | 缺失或迟到都进入恢复 | 上轮脚本在新代码上重跑：两种情况都进入 `recovering` |
| `tests/test_segmented_trace.py` 缺失；`numpy` 未说明 | 已补文件；D023、README 和打包说明已写明 NumPy 2.4.6 | 读文件 |
| `muddy_mangrove_roots` 可透视 | 先读取 Minecraft 的 `isOpaqueFullCube`，完整不透明方块一律遮挡 | 读代码 |

## 二、B09 跨隙矩阵缺信息的原因

### 结论

缺的是起跳点正上方、脚底往上第 3 格，这一格需要抬头才能看到。夹具从来没有抬头。

### 依据

- **动作确实需要这一格。** 跨隙轨迹最高上升 1.2522（`air-motions-b09-v1.json`），头顶到 feet + 1.2522 + 1.8 = feet + 3.052，比 feet+3 高出 0.052。扫掠检查遇到未知格时返回“需要信息”。
- **这一格不是结构性看不到。** 我用仓库原生代码，在起跳位置试遍所有偏航和俯仰（[`b09_stance_visibility.cpp`](2026-09-27-partial-air-repro/b09_stance_visibility.cpp)）：
  - 附近空格里，只有起跳支撑正下方那一格永远被遮住，而动作查询并不需要它；
  - 所有看得到的空格加上身体接触的两格都已知时，四个方向的查询全部可行（[`b09_gap_query_missing.py`](2026-09-27-partial-air-repro/b09_gap_query_missing.py)）。
- **夹具看不到它：**
  - 预观察从起跳原点看向脚下的支撑和四个目标，全是向下看，请求范围也只到 feet+2（[`air_motion_runtime.py:159`][volume]）；
  - 跨隙试次在平视下连续 8 帧重发缺失格（[`air_motion_runtime.py:286`][trial]），但不改变视角。
- **重放结果：** 按夹具实际的视角和请求重放，四个方向都只剩 `(0, feet+3, 0)` 未知。
  - 把请求范围恢复到旧版的 feet+3，结果不变；
  - 起跳时抬头 30° 或 60° 各看一帧，这一格就能确认。
- **与实测一致：** 受控下降不上升，不需要这一格。所以只有 40 次跨隙失败，下降全部通过。

### 修法

- **夹具：** 缺失格状态为 `outside_view` 时，按正式信息视角转头（或者至少抬头看一帧）。不要自己写一个“原地重发 8 帧”的循环。
- **记录：** 试次记录里写入每个缺失格的查询状态。这次如果记了，会直接显示 `outside_view`，而不是被理解成“结构性不可见”。
- **规划：** 跨隙和上跳都要用到头顶空间。规划层在提出信息视角时，要把“头顶一格”也纳入候选；正式导航会话目前已经能做到，夹具绕开了它。

## 三、B07 连续路线：排除了什么，还缺什么

### 已排除

我用仓库的 `astar_surface_plan`、`RouteAdmitter`、`ActionRouteExecutor` 和 `physics_1_21.step` 搭闭环，复制 B07 夹具的几何（[`b07_continuity_closed_loop.py`](2026-09-27-partial-air-repro/b07_continuity_closed_loop.py)）：

| 条件 | 结果 |
|---|---|
| 世界完全已知，没有输入延迟 | 42 tick 完成，全程着地 |
| 1 tick 延迟，每条指令有 20% 概率再晚 1 tick（300 个种子 × 3 个朝向） | 900/900 完成 |
| 固定 1 tick 延迟，另加起点偏移、朝向偏差和丢帧 | 60 组中 52 组完成；其余为 Step 超时 6 组、停滞 2 组 |
| 固定 2 tick 延迟 | 60 组中只有 5 组完成；其余为层高不符 16 组、Step 超时 27 组、停滞 2 组、达到帧数上限 10 组 |

所以：
- 感知不是原因：模拟里世界完全已知，结果与感知无关。B07 的支撑都在几格之内，同格方块可见时 Java 不会给出空气，第四节的边界问题也波及不到它们。
- 规划和控制器在 1 tick 延迟内也能正常完成。
- 按代码，`ordinary_ground_state_lost` 只在普通 Walk 段遇到“不在地面或不是站立姿态”时产生（[`fixed_route.py:396`][ground]）；而 Step 只在着地、低速时才算完成（[`step_transition.py:286`][step-done]）。
- 因此失败一定发生在 Walk 段内部：身体真的离开了地面。

### 在这个夹具里，Walk 段离地只有三种可能

1. **从高台阶往回退：** 在上层方块上往回走，越过边缘掉到半砖上（下落 0.5）。1 tick 延迟的模拟里，Step 就出现过这种“冲过头再退回、悬空 3 tick”的情况，只是那时处在 Step 段内，被 Step 自己的落地处理接住了。
2. **终点冲过头：** 出口方块另一侧比它低一格，身体中心越过边缘 0.3 格就会掉下去。
3. **侧向掉落：** 整条路线宽 1 格，两侧是 3 格以上的空洞。

### 缺的证据

- 连续路线的循环在出错时直接 `raise`，失败那次重复的 `samples` 没有写入 `b10-step-continuity.jsonl`（[`step_transition_runtime.py:378`][raise]）。
- 样本本身也只记位置，不记是否着地、速度、姿态，以及上一段指令实际生效的 tick。

### 建议

1. **补证据：** 先把失败那次的样本写盘再抛出，并补上着地、速度、姿态和输入账本的实际生效 tick。然后按失败时的位置判断是上面哪一种：z 接近上层方块后沿 → 第 1 种；越过出口 0.3 格 → 第 2 种；x 偏出 → 第 3 种。
2. **加固（与原因无关，也值得做）：**
   - 普通 Walk 遇到“短时离地、下方是已知支撑、高差在台阶范围内”时，像 Step 那样进入落地确认，而不是立刻判为 `UNSUPPORTED`；
   - 动作交接时，等上一个控制器已经发出的指令全部生效或过期，再开始下一段的地面控制。模拟里 2 tick 延迟下的“层高不符”和“冲过头再退回”都来自交接时还在路上的旧指令。

## 四、16 格边界处完整方块会被当成空气

- 空气判定在 [`geometry.hpp:69`][range] 只检查“格子最近点在 16 格内”，之后按整格投影判断有没有露出。
- 方块要被报告，露出的那部分必须在 16 格内（`in_range`）。
- 于是，一个完整方块如果近侧被挡住、只有 16 格以外的部分露出：方块本身不报告，同格的空气判定却给出“有露出”。Java 合并时同格没有方块记录，于是给出视觉空气。
- 复现 [`air_block_consistency.cpp`](2026-09-27-partial-air-repro/air_block_consistency.cpp)：60 个随机世界、160,981 个完整方块格：
  - 7,535 次空气判定为“有露出”，但方块也被报告了，Java 正确丢弃；
  - **57 次方块没被报告，客户端会给出视觉空气；全部位于 15—16 格。**
- **后果：**
  - 未知格被错记成空气；
  - 完整方块现在允许被视觉空气撤销，路线复查可能把 15—16 格处真实存在的支撑删掉，引起反复重规划。
- 这违反 D004 的硬门槛“不能取得当前视角不可能获得的信息”：这里用的是 16 格以外的视觉。
- **修法：** 空气的露出区域也按方块的规则裁到 16 格内，即对差集结果调用同一个 `in_range`，只有在范围内的露出才算。门禁增加“完整方块格上视觉空气为零”的随机检查。

## 五、远处缺失格让信息等待失去上限

- 原生对“超出 16 格”和“完全在视野外”都返回 2（[`geometry.hpp:66`][status]），Java 都映射为 `outside_view`（[`SurfaceSensor.java:186`][map]）。
- [`navigation_session.py:1541`][info-look] 对 `outside_view` 会转头；只要有转头候选，就把 40 帧计数清零（[`:1590`][reset]）。
- 机器人面向远处的缺失格以后，状态仍然是 `outside_view`，转头角度约为 0，等待永远不会结束。
- `air_request` 本身不按距离过滤，所以远处的目标支撑会一直被请求。
- 复现 [`far_cell_information_wait.py`](2026-09-27-partial-air-repro/far_cell_information_wait.py)：沿用上游的有界等待测试，只把状态改为 `outside_view`，200 帧后仍是 `needs_information`，提出了 200 次转头；状态为 `occluded` 时，40 次后正确失败。
- **修法：**
  - 把“超出范围”单独列为一种结果，不再并入“视野外”；
  - 超出范围的格子不转头，计入等待上限，或者直接返回“信息超出感知范围”；
  - `air_request` 只提交 16 格内的候选。

## 六、“稳定区域”阈值

- 计划和 D004 写的是“剩余面积大于稳定误差阈值”，实现用的是 `EPS = 1e-12`，也就是数值误差量级。
- 统计 [`air_sliver_areas.cpp`](2026-09-27-partial-air-repro/air_sliver_areas.cpp)（100 个随机世界、10,564 个视觉空气判断，按每格最大的单面露出面积；整个视野是面积 4 的正方形）：

| 露出面积 | 占比 |
|---|---|
| 1e-10 到 1e-8 | 0.06% |
| 1e-8 到 1e-6 | 0.21% |
| 1e-6 到 1e-5（不到 854×480 画面的一个像素） | 0.92% |
| 1e-5 以上 | 98.82% |

- **建议：** 阈值取像素量级，比如 `1e-5`，写进 D004，并在元数据里记录。只提高空气阈值、方块仍用 `EPS`，规则 (b) 的一致性不受影响：空气露出面积超过阈值时，同格方块的露出面积也一定超过 `EPS`。

## 七、下行目标底部的隐藏危险（设计建议）

- 部分可见规则让悬崖下方的格子可以从上层确认，这是这次修正的目的。但从远处只能看到这一格靠上的部分。
  - 例如站在边缘后 4.5 格，只能看到下行格子远侧 0.64 以上的部分。
  - 这时低于 0.56 格的流动熔岩、篝火这类低矮危险，完全在看不到的部分里。
- D004 允许隐藏非完整方块造成视觉误判，并由后续观察或接触纠正；但落进熔岩以后，接触纠正已经来不及了。
- 走到边缘时，大部分底部会露出来，这时表面深度会报告危险、触发重规划。前提是：离开边缘之前，这次重新观察一定已经发生，并且已被路线接纳层处理。
- **建议：**
  - 从上方进入的格子（受控下降、下半砖、下楼梯的落点格），要求“底部半格”有露出区域才算确认。原生可以直接判断任意子盒子，代价很小；
  - 或者要求下行动作起跳前，这些格子的空气事实来自离边缘 1 格以内的最近观察。

## 八、其他

1. **花盆方向反了。** 上一轮指出 20 种花盆被当成可透视、属于泄漏；这次改成所有 `potted_` 前缀都可透视（[`SurfaceVisibilityRules.java:59`][rules]），共 34 种，方向相反。花盆的外形只有盆身，盆身不透明，应当遮挡。
2. **蓝冰。** 规则表把 `packed_ice`、`blue_ice` 列为可透视（[`:31`][rules-exact]），测试也按“蓝冰不是不透明完整方块”来断言（[`SurfaceVisibilityRulesTest.java:23`][rules-test]）。实际游戏中 `isOpaqueFullCube` 为真，会先走遮挡分支，所以运行结果正确；但这条名单和测试写的事实是错的，容易误导以后的修改。
3. **“40 帧”的实际含义。** 计数只在本帧带回这些格子的查询结果时增加。默认每格 5 tick 才重新请求一次（[`runtime_adapter.py:127`][retry]），所以实际约为 200 个控制帧，也就是约 10 秒。建议改为按 tick 或时间计数，或者把文档写准确。
4. **由方块实体渲染的方块**（告示牌、旗帜、头颅、末地传送门等）仍按“看不见”处理。验收记录已如实列为未关闭项。
5. **`tests/test_client_observation_payload_v3.py`**（包括新的 LRU 和 `air_query_results` 解码测试）在开发提交里有，但公开快照里没有。

## 九、检查结果

| 检查 | 结果 |
|---|---|
| `python -m unittest discover -s tests/motion_nav -p 'test_*.py'` | 475 项全部通过 |
| `tests.test_surface_depth_cache`（原生门禁，Linux 编译同一份源码） | 12 项全部通过 |
| 表面、观察、运行时相关 14 个模块（含 `tests.test_segmented_trace`、Java 门禁） | 156 项，2 项错误：缺少 loom 缓存（环境原因）；`tests.test_client_observation_payload_v3` 不在公开仓库 |
| 上一轮复现脚本在新代码上重跑 | 滑行缺失和迟到样本进入恢复；LRU 行走后 2.32 ms（不用缓存 3.38 ms） |
| 本轮复现 | 见各节 |

## 十、建议（按优先级）

1. **B09：** 夹具按缺失格状态转头（至少抬头），试次记录写入缺失格状态；重跑四方向矩阵。
2. **16 格边界：** 空气露出区域同样裁到 16 格内；门禁增加“完整方块格上视觉空气为零”。
3. **远处缺失格：** 单列“超出范围”结果；超出范围的格子不转头，计入等待上限；`air_request` 只提交 16 格内的候选。
4. **B07：** 失败样本先写盘再抛出，补齐着地、速度、姿态和实际生效 tick，按位置判断是哪一种离地；同时加固 Walk 的短时离地处理和动作交接的指令排空。
5. **阈值和下行安全：** 阈值取像素量级；下行落点格要求底部半格露出，或者离边缘 1 格内的最近观察。
6. **小项：** 花盆改回遮挡；删除规则表里 `packed_ice`、`blue_ice` 的错误条目并修正测试；把“40 帧”写准确；公开 `tests/test_client_observation_payload_v3.py`。

## 十一、复现脚本

目录：[`2026-09-27-partial-air-repro/`](2026-09-27-partial-air-repro/)。

- C++ 程序：在 `4b9e73e` 检出目录的 `deployment/surface-depth-diagnostic/native` 下编译。编译命令见文件头，只替换 Windows 导出宏。
- Python 脚本：在仓库根目录运行。

| 文件 | 运行方式 | 结果 |
|---|---|---|
| `b09_stance_visibility.cpp` + `b09_gap_query_missing.py` | 见文件头 | 起跳位置所有视角：只有支撑正下方一格被遮住，四个方向的查询都可行；按夹具视角重放：四个方向都只缺 `(0, feet+3, 0)`；抬头 30° 或 60° 即可确认 |
| `b07_continuity_closed_loop.py` | `PYTHONPATH=. python -B <脚本>`；加参数 `jitter` 做随机晚到测试 | 无延迟完成；1 tick 加随机晚到 900/900 完成；2 tick 出现层高不符和超时，没有 `ordinary_ground_state_lost` |
| `air_block_consistency.cpp` | 见文件头；参数 `60` | 160,981 个完整方块格中 57 次给出视觉空气，全部位于 15—16 格 |
| `far_cell_information_wait.py` | `PYTHONPATH=. python -B <脚本>` | `outside_view`：200 帧仍在等待，提出 200 次转头；`occluded`：正确失败 |
| `air_sliver_areas.cpp` | 见文件头；参数 `100` | 约 1.2% 的视觉空气判断，依据的露出面积不到一个像素 |

[base]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/tree/4b9e73e9191f52d4ca4aa8468b32c671db223b87
[chunk]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface/SurfaceSensor.java#L158-L167
[map]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface/SurfaceSensor.java#L181-L187
[hidden]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/mc2p/motion_nav/observed_block_adapter.py#L16-L18
[volume]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/scripts/air_motion_runtime.py#L159-L173
[trial]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/scripts/air_motion_runtime.py#L286-L297
[ground]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/mc2p/motion_nav/fixed_route.py#L394-L396
[step-done]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/mc2p/motion_nav/step_transition.py#L286-L298
[raise]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/scripts/step_transition_runtime.py#L356-L395
[range]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/deployment/surface-depth-diagnostic/native/geometry.hpp#L66-L78
[status]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/deployment/surface-depth-diagnostic/native/geometry.hpp#L66-L77
[info-look]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/mc2p/motion_nav/navigation_session.py#L1541-L1608
[reset]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/mc2p/motion_nav/navigation_session.py#L1590-L1593
[rules]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface/SurfaceVisibilityRules.java#L56-L67
[rules-exact]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface/SurfaceVisibilityRules.java#L31-L36
[rules-test]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/tests/java/SurfaceVisibilityRulesTest.java#L23
[retry]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/4b9e73e9191f52d4ca4aa8468b32c671db223b87/mc2p/motion_nav/runtime_adapter.py#L127-L135
