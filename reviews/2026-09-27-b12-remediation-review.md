# 表面深度整改复审（第十四轮）

日期：2026-09-27
审查对象：`origin/main` 提交 [`b740098`][base]（表面深度整改：地下容量、遮挡规则、实体可见性、单帧视觉空气、传输成本，以及 B10—B12 的 profile 4 重跑）
性质：外部审查意见，不属于 `docs/motion_navigation/` 四类正式文档，不改变任何阶段结论或验收门槛
前几轮：[第十二轮](2026-09-26-d034-review.md)、[第十三轮（D035）](2026-09-26-d035-review.md)、[整改方案](2026-09-26-d035-remediation-plan.md)、[空气确认设计](2026-09-26-air-visibility-design.md)，更早的见各文件开头

本文代码链接都指向 `b740098`。本分支仍基于旧快照，请不要按本分支的相对路径查看代码。

## 结论

1. **上一轮的主要问题都已落实，实现质量总体不错。**
   - **地下容量：** 剔除被包住的方块，并为存储边缘多读一圈邻居。
   - **空气确认：** 32 格内直接读取 `isAir()` 的旧路径已删除。现在的判定与方块表面在同一次原生调用里完成：整格投影全部可见，并且本帧没有报告方块。
   - **实体可见性：** 16 格内与方块共用遮挡面。
   - **传输：** 去掉了二次序列化。
   - **第十二轮遗留：** 交接帧视角、期限竞态、中性原因和 B10 表述都已修正。
   - 我在 Linux 上用同一份原生源码重新编译，仓库的 10 项原生门禁全部通过，其中包括 500 个随机世界的空气零误判。安装 `numpy` 后，运动导航 472 项测试全部通过。
2. **高优先级问题：“整格可见”规则让所有下行格子都无法确认（第二节）。**
   - 这里的下行格子，指高差下方紧贴台阶的那一格：一格悬崖下方、半砖台阶下方，以及地面上 1×1 的坑。这一格总有一个面贴着脚下的方块，那一面靠下的部分从上层任何位置都看不到。
   - 我用仓库的原生代码实测：站在上层时，44,400 个姿态里没有一个能确认这三类格子。
   - 后果：陌生地形里，受控下降、下半砖或下楼梯都会一直“等待信息”。信息视角对“在视野内但被挡”的格子不转头，导航会话也没有等待超时。
   - B11 和 B12-B 的验收都要让夹具先把机器人移到特殊观察位置，才能通过，这正是这个问题的表现。B07 的下楼梯和 B09 的受控下降还没有在 profile 4 下重验。
   - **这也是我上一轮空气设计估计不足的地方。** 当时把“台阶边缘”归入跨帧累积，但靠近边缘的那一块从上层任何位置都看不到，累积也解决不了。
   - 能解决的办法是：像真人一样走到边缘、眼睛越过边缘往下看（实测 707 个姿态可以确认）。半砖台阶还需要半格粒度的空气事实。
3. **中等问题一：遮挡规则表是手写的，有漏看和误判（第三节）。**
   - 规则表只有 18 个方块 ID 加 7 个后缀。按 1.21 注册表逐一展开后：
     - `muddy_mangrove_roots`（完整不透明方块，红树林沼泽里大量存在）和 20 种花盆被当成可透视，机器人能看到它们后面的东西；
     - 反过来，冰、大部分花、农作物、火把、梯子、铁轨都被当成遮挡。这一方向偏保守，但会减少空气确认。
   - 渲染类型为 `INVISIBLE` 的方块一律被当成“看不见、不遮挡”，所在格还可以被确认为空气。
     - 按我对 1.21 源码的了解，`BlockWithEntity` 默认返回 `INVISIBLE`。所以只由方块实体渲染器绘制的告示牌、旗帜、头颅、末地传送门、末地折跃门、移动中的活塞，很可能都属于这一类。
     - 其中头颅有碰撞；走进末地传送门会被传送走。
     - 我这里没有 Minecraft 运行环境，需要在客户端枚举确认。
4. **中等问题二：未加载区块会被当成空气（第四节）。**
   - 客户端对未加载区块返回 `void_air`，表面存储里没有任何几何。16 格内落在未加载区块的空气候选，会被判为“整格可见且为空”。
   - 这违反“未知不能当作空气”。而且被确认的格子如果之后被遮住，这条假空气会一直留在世界知识里。
5. **中等问题三：两处功能只在测试里成立（第五、六节）。**
   - **撤销消失的方块：** 客户端能把已知方块更新为视觉空气，但正式的 `air_request` 只收未知格，导航从不重查已知方块。这项能力只在测试脚本手工构造请求时出现。所以“支撑方块被挖掉、知识不更新”在正式链里仍然存在。
   - **信息视角：** 用格子中心判断“已在视野内”，而原生判定要求 8 个角都在视锥内。实测：平视时，正前方脚边那格被判为“在视野内”，不会转头，但它要低头至少 20° 才能确认；斜前方相邻那格既不会触发转头，也永远无法确认。B12-B 第二次失败遇到的正是这类“紧贴身体、轮廓超出视野”的格子。
6. **中等问题四：C1-B 和 C1-C 通过性能门槛，依赖一个会失效的缓存（第七节）。**
   - Python 解码缓存以“含坐标的整条方块记录”为键，上限 4,096 条，永不淘汰。
   - 固定场地里很有效。但我模拟连续行走约 90 秒后缓存就满了，之后每帧解码反而比不用缓存更慢：480 个方块时 4.75 ms，不用缓存是 3.45 ms。
   - 固定场地的 P95 不能代表巡游时的开销。
7. **低优先级（第八节）：**
   - B10 滑行检查把缺失或迟到的输入样本当成中性；
   - 公开仓库没有 `tests/test_segmented_trace.py`，但 AGENTS.md 和本次验收命令都引用它；
   - 新增的 `numpy` 依赖没有写进版本锁定说明；
   - B10 晚到率的分母只有 40 个严格窗口命令帧（文档如实写了）；
   - 我上一轮提出的“控制往返 P95 < 50 ms”触发条件区分力不足，建议改记提交余量。

## 审查范围

- 阅读 `b740098` 相对 `3e0c64b` 的全部代码改动，以及新增的阶段计划和验收记录（B12 表面深度整改）。
- 重点对照第十三轮审查和整改方案，逐项核对实现。
- 在 Linux 上用仓库原生源码（`cache.cpp`、`geometry.hpp`，只替换 Windows 导出宏）编译测试程序，检查空气判定在导航关键格子上的覆盖情况。
- 运行仓库的 Python 测试和原生门禁（结果见第九节）。
- 没有 Minecraft 运行环境，涉及原版渲染类型的结论已注明需要实机确认。

## 一、已核实的修复

| 第十三轮问题 | 现在的做法 | 核实方式 |
|---|---|---|
| 地下 33,264 块超上限 | 剔除六面都被遮挡完整方块包住的方块；边缘多读一圈邻居；区分三类容量失败 | 读代码；原生等价性门禁通过；验收记录地下隧道只打包 1,854 块 |
| 32 格内直接读 `isAir()` | 删除；同一次原生调用内做整格可见判定；Java 合并时同格有其他来源就不输出空气（[`ClientBlockObservationV3.java:214`][air-merge]） | 读代码；Linux 编译后原生门禁 10/10 通过 |
| 熔岩、浮冰、蓝冰、含水方块可透视 | 改为遮挡；含水方块按方块本身判断 | 读代码；规则测试。另见第三节 |
| 实体可见性规则不一致 | 16 格内用包围盒可见区域；16—32 格五点检查也改用同一张规则表 | 读代码 |
| 每帧解析后再序列化 | `DeploymentSampleEncoder` 直接嵌入观察字节 | 读代码 |
| 相同方块重复建几何 | 按“方块、流体、碰撞”共享几何；变化格在写入前直接比较 | 读代码。共享几何的键不含坐标，没有第七节的问题 |
| 交接帧战斗视角 | 导航本帧提案之后读取当前动作；路线动作拥有视角时撤回战斗视角 | 读代码 |
| 期限竞态抛异常 | 期限已过走同一条安全收尾路径 | 读代码 |
| B10 滑行不核对输入 | 滑行阶段读取实际输入样本 | 读代码；另见第八节 |
| 失效模块 | 移出导出清单；新增逐模块导入门禁 | 测试通过（需要 `numpy`） |

没有实施的两项（事件驱动刷新、增量协议）都给出了测量依据：方块重读 P95 最高 0.359 ms；可见方块 P95 最高 644，低于 800。这符合“先测量、后改契约”的顺序。

## 二、整格规则让下行格子永远无法确认

### 原理

以一格悬崖为例：上层地面在 x<0，下层地面低一格；要下去，就需要确认悬崖下方紧贴崖壁的那一格 (0,0,0) 是空的。

- 眼睛在上层时（x<0），这一格朝向眼睛的面（x=0）正好贴着崖壁方块。
- 视线要到达这个面靠下的部分，必须先穿过崖壁方块，所以这一部分永远被挡住。
- 整格规则要求整个投影都可见，所以这一格永远无法确认。
- 半砖台阶下方的那一格、地面上 1×1 的坑，都是同样的几何。

### 实测（仓库原生代码，未改算法）

测试程序 [`downward_air_coverage.cpp`](2026-09-27-b12-remediation-repro/downward_air_coverage.cpp)：
- 站立位置：上层 10 个 x × 5 个 z；
- 朝向：24 个偏航角 × 37 个俯仰角；
- 共 44,400 个姿态。

| 格子 | 能确认的姿态数 |
|---|---|
| 一格悬崖下方、紧贴崖壁的格子 | **0** |
| 同一悬崖再往外一格 | 3,785 |
| 悬崖边上方那格 | 6,191 |
| 对照：平地上的普通格子 | 7,107 |
| 半砖台阶下方、紧贴半砖的格子 | **0** |
| 地面上 1×1 的坑（在坑周围站立，13,320 个姿态） | **0** |

子区域测试 [`downward_subcell_probe.cpp`](2026-09-27-b12-remediation-repro/downward_subcell_probe.cpp) 复用仓库的同一套判定，只把整格换成任意盒子：

| 盒子 | 能确认的姿态数 |
|---|---|
| 悬崖格上半部 | 0（它的近面同样贴着崖壁） |
| 悬崖格离崖壁 0.3 格以外的部分 | 578 |
| 悬崖格整格，潜行探身、眼睛越过边缘 | **707** |
| 半砖下方格子的上半部（半砖顶面以上） | 2,927 |
| 半砖下方格子整格，眼睛越过边缘 | 652 |

### 影响

- 受控下降的检查会扫过这一格（[`air_motion.py:328`][drop-sweep]），扫到未知格时返回“需要信息”（[`geometry.py:156`][sweep-unknown]）。下半砖、下楼梯同理。
- 这一格在视野里，只是被挡住，所以信息视角不会转头。导航会话没有等待超时，只能一直等，或者由上层任务期限结束。
- 在陌生地形里，所有向下的移动都规划不出来，除非这一格以前从别的位置（下方或越过边缘）看到过。
- 验收记录里已经出现了这个问题：
  - B11 写明“相邻地面会遮住缺口立方体的一部分”，只能让夹具先把机器人移到能完整看到缺口的位置；
  - B12-B 的前两次失败都是路线格子看不全（后两次是为此加的观察准备出了问题），最后同样靠夹具先观察。
  - 这些做法作为验收准备是合法的，但说明正式导航本身还做不到。
- AGENTS.md 仍把“相邻一格零伤害下降”和 B07 的楼梯上下列为已接入能力，但它们都没有在 profile 4 下重验。

### 对上一轮设计的更正

我在[空气确认设计](2026-09-26-air-visibility-design.md)里把“柱子后、台阶边缘”都归为跨帧累积可以解决的情况。柱子后成立；台阶边缘不成立：靠崖壁的下方八分块从上层任何位置都看不到，累积也补不齐。需要的是换视点，而不是多看几帧。

### 建议

1. **增加“探边观察”这一信息动作。** 这是真实玩家的做法：走到边缘，潜行探身，往下看。
   - 前提：边缘上方那格必须已经确认（它可以整格确认，实测 6,191 个姿态）。
   - 探身后，悬崖格整格可见（707 个姿态）。
   - 这一动作只在局部进行，不需要完整的观察站位搜索。
2. **允许半格粒度的空气事实。** 至少上下半格。
   - 半砖台阶的探身姿态，身体会进入下方格子的上半部；这一半可以从台阶上确认（2,927 个姿态），但整格规则不接受。
   - 规划的扫掠检查需要能按子区域读取空气事实。
3. **等待原因要分开。** “缺失格在视野外”和“当前站位结构性看不到”应该给出不同的原因码。后者应当触发探边观察，或者交给上层，而不是一直等待。
4. **补重验。** 在 profile 4 下重跑 B07 的楼梯和半砖下行、B09 的受控下降，并增加一个“不预先观察的陌生地形下行”场景。
5. **不建议的做法：**
   - 放宽为“大部分可见即可”。无法证明不出错。
   - 继续用夹具预先观察来通过验收。

## 三、遮挡规则表

### 手写表与原版不一致

[`SurfaceVisibilityRules.java:11`][rules] 只列出 18 个方块 ID，再用 7 个后缀（`_sapling`、`_tulip`、`_mushroom`、`_roots`、`_fungus`、`_flower`、`_bush`）判断“植物”。其余方块默认遮挡。

用仓库自带的 1.21 注册表逐一展开（[`visibility_rules_registry_scan.py`](2026-09-27-b12-remediation-repro/visibility_rules_registry_scan.py)，直接从 Java 源码读取名单）：

| 方向 | 方块 | 后果 |
|---|---|---|
| 不透明却被当成可透视 | `muddy_mangrove_roots`（完整方块，红树林沼泽的地表和水下大量存在） | 能看到它后面的方块、空气和实体，违反合法观察 |
| 不透明却被当成可透视 | 20 种花盆（`potted_*_sapling`、`potted_*_tulip` 等） | 能看穿花盆本身，影响较小 |
| 可透视却被当成遮挡 | 冰、大部分花（绒球葱、蓝花美耳草、滨菊、矢车菊等）、农作物、火把、梯子、铁轨、红石线、竹子、锁链、灯笼等 | 偏保守，不违规；但花田和农田后面的空气无法确认 |

阶段记录写“遮挡规则与 1.21 固定表一致”，但这张表是手写的，不是由原版数据生成的。

### `INVISIBLE` 渲染类型的方块

[`SurfaceWorldSource.java:26`][invisible] 把渲染类型为 `INVISIBLE` 的方块全部排除：既不导出、也不遮挡，所在格还可以被确认为空气。设计本意是只针对屏障、光源方块和结构空位。

但按我对 1.21 源码的了解，`BlockWithEntity`（Mojang 名 `BaseEntityBlock`）的默认渲染类型就是 `INVISIBLE`；箱子、潜影盒、床等方块会改写它，只由方块实体渲染器绘制的方块不会改写。按注册表统计，受影响的可能有：

| 类别 | 数量 | 碰撞 | 当成空气的后果 |
|---|---|---|---|
| 告示牌（含悬挂式） | 44 | 无 | 看不见告示牌，也能看穿它 |
| 旗帜 | 32 | 无 | 能看穿一整块 1×2 的旗帜 |
| 头颅 | 14 | 有（半格高） | 规划认为可以通过，撞上后才由接触纠正 |
| 末地传送门、末地折跃门 | 2 | 无 | 规划认为是普通空气，走进去会被传送 |
| 移动中的活塞 | 1 | 有 | 推动过程中短暂当成空气 |

**需要实机确认：** 在客户端遍历 `Registries.BLOCK` 的全部状态，输出渲染类型为 `INVISIBLE` 的方块。

### 建议

- 在 1.21 客户端里一次性枚举全部方块状态，记录渲染类型、渲染层、`isOpaqueFullCube` 和外形。据此生成规则表并提交，`visibility_rules_id` 使用表格哈希。
- 门禁比较分类器与这张表，任何不一致都要显式登记为策略（例如“树叶固定遮挡”“冰按遮挡”）。
- “看不见”只保留真正的隐藏方块（屏障、光源方块、结构空位）。由方块实体渲染的方块按外形处理：可见，并且按实际材质决定是否遮挡。

## 四、未加载区块会被确认为空气

- [`SurfaceWorldSource.java:18`][world-read] 对 `isAir()` 的方块不建记录。客户端对未加载区块返回 `void_air`，所以未加载区块在存储里就是“什么都没有”。
- `SurfaceSensor.sample()` 没有检查空气候选所在区块是否已加载（[`SurfaceSensor.java:154`][air-candidates]）。候选格前面没有任何几何，就会被判为整格可见。Java 随后直接写成 `minecraft:air`，不再读取世界。
- **什么时候会发生：** 进入世界、重生、换维度、传送之后，16 格内的区块还没到达的那几帧。
- **后果：** 这些格子被确认为空气。区块到达后，如果这些格子被地表挡住，就再也不会被纠正。地下可能因此留下一片不存在的“空洞”。
- **修正：**
  - 只回答所在区块已加载、并且所在瓦片已经读取过的候选；
  - 超出世界高度的格子单独处理；
  - 补一个“区块尚未到达时请求空气”的组件测试。

## 五、已知方块的撤销没有接入正式链

- 阶段记录把“允许同一判定把已知方块更新为视觉空气”标为完成。验收里“石块被移除后更新为空气”的结果，来自 `scripts/visual_air_runtime.py` 手工构造的请求。
- 正式请求路径是 `NavigationSession.observation_request()` → `NavigationObservationAdapter.air_request()`。后者只保留未知格（[`runtime_adapter.py:159`][air-unknown]），而导航的缺失列表本来也只有未知格。
- 复现 [`known_block_never_rechecked.py`](2026-09-27-b12-remediation-repro/known_block_never_rechecked.py)：已知石块格的正式空气请求为空。
- **后果：** 别人挖掉路线上的支撑方块后，只要机器人没有碰到它，世界知识就一直认为它还在，路线会照旧走上去。
- **修正：**
  - 让导航以较低配额，重查“当前路线依赖、并且在视野内”的已知方块；
  - 同时排除规则表中“看不见”的方块（例如屏障）。否则一个通过身体接触确认的屏障，会被视觉空气反复改回空气，机器人会一次又一次撞向同一面屏障墙。目前因为已知格从不重查，这种来回还不会发生；接入撤销时必须一起处理。

## 六、信息视角判定与请求排序

### 信息视角

[`navigation_session.py:1551`][info-look] 用格子中心的偏航、俯仰误差（各 ≤60°）判断缺失格“已在视野内”；原生判定要求 8 个角都在视锥内（[`geometry.hpp:68`][cell-visible]）。

复现 [`information_look_center_test.cpp`](2026-09-27-b12-remediation-repro/information_look_center_test.cpp)：平地站立、平视 +x 方向。

| 缺失格 | 格子中心的偏角 | 信息视角的判断 | 原生确认所需的俯仰 |
|---|---|---|---|
| 正前方脚边 (1,1,0) | 俯仰 48° | 在视野内，不转头 | ≥ 20° |
| 前方第二格 (2,1,0) | 俯仰 29° | 在视野内 | 0°—70° 都可以 |
| 斜前方相邻 (1,1,1) | 偏航 45°，俯仰 38° | 在视野内，不转头 | 不转偏航时任何俯仰都不行 |

**修正：** 信息视角的“在视野内”判定使用与原生相同的规则：8 个角都在视锥内，并且在 16 格以内。

### 请求排序

`air_request` 按坐标字典序取前 128 个候选。在旧的直读方式下，请求几乎都会成功，顺序无关紧要。现在大量候选因为在视野外或被挡住而失败，并进入重试等待。建议先筛掉 16 格外和不在视锥内的候选，再按距离或路线顺序排序。

## 七、C1-B 和 C1-C 的性能通过依赖一个会失效的缓存

- 解码缓存以整条方块记录为键，其中包含坐标；上限 `MAX_CACHED_BLOCK_FACTS_V3 = 4096`（[`client_observation_payload_v3.py:26`][decode-cache]），满了以后不再加入、也不淘汰；只在后端 `close()` 时清空（[`fabric_behavior.py:321`][cache-clear]）。
- C1-B 和 C1-C 的验收记录都说明，最终 P95 低于 8 ms 靠的是“复用完全相同的已校验方块事实”。这两个批次都在固定场地里运行。
- 复现 [`block_decode_cache_drift.py`](2026-09-27-b12-remediation-repro/block_decode_cache_drift.py)（仓库正式解码函数；每帧 480 个方块，与森林场景的可见 P95 接近）：

| 情况 | 每帧解码 P50 |
|---|---|
| 不用缓存 | 3.45 ms |
| 静止场地 | 1.9 ms |
| 行走 90 秒（每 5 帧移动一格）：前段 | 1.94 ms |
| 行走 90 秒：缓存满了之后 | **4.75 ms** |

缓存满了之后，每个方块都要构造键、查找失败、再完整解码，所以比不用缓存还慢。

**建议：**
- 键里不要带坐标（只缓存与坐标无关的部分），或者改为有界淘汰；
- 性能门槛增加一个“连续行走数分钟”的场景，不要只在固定场地测。

## 八、其他

1. **B10 滑行把缺失或迟到的样本当成中性。**
   - `_coast_input_changed` 只检查账本里已有的样本（[`motion_candidate.py:487`][coast]），检查过的 tick 之后不再复查。账本自己的注释要求“缺失的 tick 保持缺失，不能当成中性”（[`online_motion.py:178`][ledger]）。
   - 复现 [`coast_missing_or_late_sample.py`](2026-09-27-b12-remediation-repro/coast_missing_or_late_sample.py)（沿用上游测试夹具）：
     - 样本按时到达：进入恢复；
     - 同一个非中性样本晚一帧到达：一直滑行，不会发现；
     - 连续两个 tick 没有样本：照样滑行。
   - **建议：** 缺失的 tick 按“未知”处理，至少等样本补齐再推进检查位置。
2. **`tests/test_segmented_trace.py` 不在公开仓库里。**
   - AGENTS.md 第 6 节和本次验收的检查命令都引用了它，本次还改了 `mc2p/runtime/segmented_trace.py`。
   - 在公开仓库里运行这条命令会报导入错误。
3. **新增依赖 `numpy==2.4.6`。** 只用于诊断加载器和原生门禁，但会让运动导航的导出导入门禁失败（没装 `numpy` 时 472 项中有 1 项失败）。AGENTS.md 的版本锁定清单里没有它，建议补上说明，或者让诊断模块不进入导出导入检查。
4. **B10 晚到率的统计口径。**
   - 分母只有 40 个严格窗口命令帧，Wilson 上界 8.76%，文档如实写了。
   - 我在上一轮方案里提出“控制往返 P95 仍 ≥ 50 ms 才考虑增量协议”，这个条件区分力不足：两轮 P95 为 48.7 ms，P99 为 49.1、49.6 ms，最大值 50.2 ms 时仍然按时生效，说明这个量受 tick 边界影响。
   - 建议改为记录“提交余量”，即命令到达客户端时，距离许可 tick 截止还剩多少毫秒。它能直接反映离晚到还有多远。
5. **两个 C1 脚本整文件改成了 CRLF。** `scripts/c1_external_motion_runtime.py` 和 `scripts/c1_moving_melee_runtime.py` 的约 2,700 行差异全部是换行符变化，没有内容改动。
6. **传输测试在 Linux 上失败。** `test_real_framed_io_and_idempotent_close_release_port` 关闭后立刻重新绑定同一端口，在 Linux 上会因为 TIME_WAIT 失败，属于平台差异。正式平台只验收 Windows，可以不改，但门禁在其他平台上会误报。

## 九、检查结果

| 检查 | 结果 |
|---|---|
| `python -m unittest discover -s tests/motion_nav -p 'test_*.py'` | 装 `numpy` 前：472 项 1 失败（导出导入门禁缺 `numpy`）；装后 472 项全部通过 |
| `tests.test_surface_depth_cache`（原生门禁，Linux 编译同一份源码） | 10 项全部通过，包括 500 个随机世界的空气零误判、实体遮挡、剔除等价性 |
| 表面、观察、运行时相关 13 个测试模块 | 147 项，3 项错误：缺少 loom 缓存（环境原因）、`tests.test_segmented_trace` 不存在、Linux 下 TIME_WAIT |
| `tests.test_standalone_java_gates` | 3 项通过 |
| 本轮复现（第二、三、五、六、七、八节） | 结果见各节 |

## 十、对照总体设想

- **方向正确：** 方块、空气和近处实体共用一份表面投影；合法性由同一套几何保证；关键结论都带着规则版本和成本数据。这比射线时代清楚得多。
- **还缺一环：** 真人判断“能不能下去”靠的是走到边上看一眼。现在的规则只接受单帧整格可见，又没有探边动作，所以机器人在陌生地形里上得去、下不来。对“2P 伙伴”来说，跟随、追击、挖矿回程都离不开下行，这个缺口比性能更优先。
- **验收口径：** B11、B12-B 靠夹具先观察才能通过。这可以作为分层验收的一步，但不能替代“机器人自己获取信息”的端到端证据。建议在恢复后续阶段之前，先补一个不预先观察的下行场景。

## 十一、建议（按优先级）

1. **下行格子：** 增加探边观察动作和半格空气事实；区分“视野外”和“结构性看不到”两种等待原因；在 profile 4 下重验 B07 下行和 B09 受控下降，并加一个不预先观察的场景。
2. **合法性：**
   - 由 1.21 客户端枚举生成遮挡表，修正 `muddy_mangrove_roots` 和花盆；
   - 确认并修正由方块实体渲染的方块被当成 `INVISIBLE` 的问题；
   - 空气候选只在区块已加载时作答。
3. **正式链补全：**
   - 导航以低配额重查路线依赖的已知方块，同时排除看不见的方块，避免屏障来回；
   - 信息视角改用与原生一致的视野判定；
   - 空气请求先筛选、再排序。
4. **性能证据：** 解码缓存去掉坐标或改为有界淘汰；性能门槛增加连续行走场景；增加提交余量指标。
5. **小项：** 滑行缺失样本按未知处理；补上或移除 `tests.test_segmented_trace` 的引用；说明 `numpy` 依赖。

## 十二、复现脚本

目录：[`2026-09-27-b12-remediation-repro/`](2026-09-27-b12-remediation-repro/)。

- C++ 程序：在 `b740098` 检出目录的 `deployment/surface-depth-diagnostic/native` 下编译运行，编译命令见各文件开头。
- Python 脚本：在 `b740098` 检出目录的仓库根目录运行。

| 文件 | 运行方式 | 结果 |
|---|---|---|
| `downward_air_coverage.cpp` | 见文件头 | 悬崖下方、半砖下方、1×1 坑：44,400 或 13,320 个姿态中确认 0 次；探身时悬崖格 707 次 |
| `downward_subcell_probe.cpp` | 见文件头 | 悬崖格上半部 0；离崖壁 0.3 格以外 578；半砖下方格子上半部 2,927 |
| `information_look_center_test.cpp` | 见文件头 | 正前方脚边格平视时被判为在视野内，但要低头 ≥20° 才能确认；斜前方相邻格不转头就永远无法确认 |
| `visibility_rules_registry_scan.py` | `python -B <脚本路径>` | 94 个方块被判为可透视，其中包括 `muddy_mangrove_roots` 和 20 种花盆；由方块实体渲染的 93 个方块清单 |
| `known_block_never_rechecked.py` | `PYTHONPATH=. python -B <脚本路径>` | 已知石块格的正式空气请求为空 |
| `block_decode_cache_drift.py` | `PYTHONPATH=. python -B <脚本路径>` | 不用缓存 3.45 ms；静止场地 1.9 ms；行走 90 秒缓存满后 4.75 ms |
| `coast_missing_or_late_sample.py` | `PYTHONPATH=. python -B <脚本路径>` | 样本按时到达会恢复；晚一帧或缺失时一直滑行 |

[base]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/tree/b740098a4c6edba24b61cc1fce1ec64b9a3f9c46
[air-merge]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/b740098a4c6edba24b61cc1fce1ec64b9a3f9c46/mc2p/backends/runtime_overlays/mc121_observation/ClientBlockObservationV3.java#L214-L215
[drop-sweep]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/b740098a4c6edba24b61cc1fce1ec64b9a3f9c46/mc2p/motion_nav/air_motion.py#L328
[sweep-unknown]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/b740098a4c6edba24b61cc1fce1ec64b9a3f9c46/mc2p/motion_nav/geometry.py#L156-L159
[rules]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/b740098a4c6edba24b61cc1fce1ec64b9a3f9c46/mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface/SurfaceVisibilityRules.java#L11-L58
[invisible]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/b740098a4c6edba24b61cc1fce1ec64b9a3f9c46/mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface/SurfaceWorldSource.java#L26
[world-read]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/b740098a4c6edba24b61cc1fce1ec64b9a3f9c46/mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface/SurfaceWorldSource.java#L18
[air-candidates]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/b740098a4c6edba24b61cc1fce1ec64b9a3f9c46/mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface/SurfaceSensor.java#L154-L161
[air-unknown]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/b740098a4c6edba24b61cc1fce1ec64b9a3f9c46/mc2p/motion_nav/runtime_adapter.py#L148-L166
[info-look]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/b740098a4c6edba24b61cc1fce1ec64b9a3f9c46/mc2p/motion_nav/navigation_session.py#L1522-L1565
[cell-visible]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/b740098a4c6edba24b61cc1fce1ec64b9a3f9c46/deployment/surface-depth-diagnostic/native/geometry.hpp#L68-L86
[decode-cache]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/b740098a4c6edba24b61cc1fce1ec64b9a3f9c46/mc2p/backends/client_observation_payload_v3.py#L26
[cache-clear]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/b740098a4c6edba24b61cc1fce1ec64b9a3f9c46/mc2p/backends/fabric_behavior.py#L321
[coast]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/b740098a4c6edba24b61cc1fce1ec64b9a3f9c46/mc2p/motion_nav/motion_candidate.py#L475-L492
[ledger]: https://github.com/ATDBC/mc-ai-motion-navigation-v1-standalone/blob/b740098a4c6edba24b61cc1fce1ec64b9a3f9c46/mc2p/motion_nav/online_motion.py#L176-L180
