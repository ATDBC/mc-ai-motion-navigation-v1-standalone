# 运动导航中层重构 M0—M1 验收记录

正式运行：2026-10-06；收尾记录：2026-10-07。状态：M0 通过；M1 结构止损，不通过。行为一致的动作查询迁移予以保留，后续未授权。

## 1. 正式环境

- 工作区：`D:\My_project\mc_ai`
- 正式平台：Windows
- Linux：只作补充可移植性复核，不参与阶段关闭
- 起点：`92246ce`；M0 行为来源：`51f3759`

## 2. M0 验收表

| 项目 | 结果 | 证据 |
|---|---|---|
| D072 与阶段文档一致 | 通过 | 当前只实施 M0—M1，M2—M5 仍未授权 |
| 后台规划不在控制进程执行 | 通过 | 真实 spawn worker；控制侧执行入口改成断言仍取得 COMPLETE 结果 |
| 控制侧队列操作不阻塞 | 通过 | 队列守卫只允许 put_nowait/get_nowait；不以耗时判断 |
| 两项错误副本能被检查发现 | 通过 | 控制侧计算和阻塞读取分别触发对应断言；worker 检查 15/15 |
| 三个死路径逐项完成变异后删除 | 通过，有方法边界 | 每项新版代表集合 61 场末尾断言调用计数为零；另有既有 S0-R 全量零调用矩阵和生产静态零引用 |
| 结构量尺能发现未分类和隐藏分支 | 通过 | v2 组件 7/7；含比较、映射、match、别名、类名字符串、未知段类型和明确排除边界 |
| seed 15、69、119 的独立覆盖已判定 | 不增加历史种子 | 三者在运动协调中的方法入口、逐函数行跳转弧均没有现行场景之外的新增路径 |
| 两个驱动器边界已直接检查 | READY 缺陷修复；已终态边界当前有界 | 正式拒绝回执触发地面/空中 RED，再通过 Session 失败收尾；控制关闭保留既有结论、来源和身体 owner |
| 完整运动导航正序、逆序 | 1,509/1,509，各一轮 | af0fe41；两轮场景深度哈希不变。随后只读量尺增加三项检查，直接 7/7，不把它们冒充已进入这两轮完整检查 |
| 五组行为集合 | 全部逐项一致，一轮 | 产品 2,000、协调 1,448、故障 16、跟随 10、世界变化 19；与 S0-R 对应索引差异均为零；产品仍 1,718/2,000 |

### M0 量尺与证据

正式结构起点为 `evidence/motion_navigation/redesign-m0/metrics-baseline.json`，口径是 `navigation-design-metrics-v2`：总动作判断位置 79，受控下降 30，未分类位置 0；Session 为 5,046 行、107 个可调用方法、57 个实际字段，另列 30 个属性访问器和 6 个转发写入属性。运动导航包共 38,279 行。M0 之前为 5,072 行、38,316 行；删除后没有动作行为变化。

旧三方数字 70/21 只作旧口径对照。新脚本纳入映射、比较表达式和别名，并单列动作记录及类型联合声明。它不把整个 actions 目录排除，只排除具体受控下降实现。旧量尺报告保留为工具修正前的证据，不与 v2 混用。

两个协调器都有提交、结果轮询、身份绑定、接纳及退出记录职责。量尺列出这些实际入口与依赖。它没有把规划特有快照、运动特有落地重锚或不同 owner 的状态简单算成重复代码。M4 仍未实施。

三个删除候选各有一个带调用计数的断言变异。每项代表集合覆盖产品 10、协调 6、故障 16、跟随 10、世界变化 19。原 S0-R 的五组完整调用矩阵分别记录三项零调用，生产静态扫描也没有调用入口。

首项旧版断言工具额外运行过完整 3,493 场。它没有发现未捕获的异常，但驱动器可能吸收断言，产品又允许有界失败，所以这轮结果不能证明零调用。随后为变异工具补了独立调用计数，并用组件反例证明“捕获断言后仍报告 passed”必须失败；三项新版代表集合均确认计数为零。旧原始结果保留，不改写成新版全量证据。

M0 第一次完整检查为 1,509 项、4 个错误，原因是共享 FakeNavigationSession 没有补新增的端口成员。修复消费者后，跟随驱动器直接检查 19/19；最终正逆序均全绿。首次失败保存在 `checks-forward.json`，最终结果分别保存为 `checks-forward-final.json` 与 `checks-reverse-final.json`。

行为索引、摘要和哈希清单位于同一证据目录的 `baseline-manifest.json`。五组实际来源均为干净提交 `51f3759`，生产指纹相同。原始轨迹留在 `.tmp/redesign-m0-*`，没有加入 Git。只读量尺后续修正不改变这些索引文件。

## 3. M1 实际结果：结构止损，不通过

M1 的动作查询迁移已实施，行为与安全证据支持保留这些提交。Session 从 5,046 行减到 4,994 行，只减少 52 行，未达到 100 行门槛。剩余代码仍承担入口快照、信息等待、probe/grant、作业或身体责任。主线程确认不再为行数拆这些职责，M1 按结构止损结束，M2—M5 未获授权。

| 项目 | M0 基线 | 最终候选 | 判定 |
|---|---:|---:|---|
| 受控下降外部类型分支位置 | 30 | 0 | 通过 |
| 总动作类型分支位置 | 79 | 42 | 减少 37，低于 49，通过 |
| Session 行数 | 5,046 | 4,994 | 减少 52，未达到 100，失败 |
| Session 受控下降引用 | 存在 | 0 | 通过 |
| 受控下降运行时外部文件 | 5 | 1 | 只剩 route_admission 规划边转换，通过 |
| 运动导航生产代码 | 38,279 | 38,431 | 净增 152，不超过 300，通过 |
| 未分类动作位置 | 0 | 0 | 通过 |
| Session 方法 / 长期状态字段 | 107 / 57 | 107 / 57 | 未迁移协调状态 |
| 安全声明 | 既有行为 | 五种动作显式填写；缺少声明时拒绝登记 | 通过 |
| 通用恢复 | 通用 motion coordination | NEEDS_STATE、等待落地、重锚仍由原 owner 处理 | seed 163、条件触发检查通过 |
| 扩展探针 | 无 | 测试专用新身份只增加实现与登记 | 核心协调模块没有为它修改，通过 |

量尺版本为 `navigation-design-metrics-v2`，具体报告为 `metrics-final.json`。动作记录和 `RouteAction` 联合声明另列，不冒充运行时特判。`actions/existing.py`、registry 和 contracts 均未排除。旧 70/21 只作为预修工具口径，不参与本次差值。

当前仅下降提供入口观察查询，其余风险、安全、求解和控制器声明均由五种动作显式填写。没有新增下降专属的 Session/协调接口或 M2—M5 空壳。扩展探针复用既有下降几何与控制器，只证明接口可接入一种新身份，不代表新增运动能力。

### 3.1 动作级等价证据

生产旧链来源为 `3b99f96`，测试采集器保存于 `b6a56b2`。采集覆盖五组 61 个代表入口和 drop-normal、drop-late 各 200 场，去重后共 459 场。共有 403 个实际下降实例、9 种几何记录。比较计数如下：

| 查询 | 实际旧链 / spec 比较次数 |
|---|---:|
| 声明、风险、身体承诺、停止保护 | 403 个实例 |
| 前提和离地前复查条件 | 19,544 |
| 方向 | 3,870 |
| 落点区域 | 1,732 |
| 伤害记账 | 7,194 |
| 九种语料的直接几何回放 | 9 方向、9 落点、45 完成边界 |

差异均为零。端到端采集中没有发生动态完成查询；完成判断的证据来自九种几何在落地、空中、错高度、远离落点和边缘重叠下的 45 次直接比较。不能把它写成 459 场均有动态完成判断，也不能声称完成了 3,493 场动作级等价采集。

测试工具先发现 `frozenset` 无法编码，保留失败日志；补反例后修正。比较器还增加场景末尾的差异累计断言，专门证明驱动器吸收异常不能使错误 spec 通过。此前两个全量采集尝试分别因工具错误失败和按裁定取消，均未冒充通过结果。

最终代码已删除旧逻辑副本和临时比较器，仅保留冻结事实语料及契约、扩展检查。语料的 transition 和 entry_window 原记录保留；直接几何回放只使用几何，不授予控制器入口资格。正式行为及性能采集没有安装动作级路径探针。

### 3.2 Windows 完整检查与行为

最终生产来源为 `b39da9a7acaa0b6d8ac4f7945dc29f8b7921746c`，工作树干净，完整生产指纹为 `611e00f79d6d0c1fbc59086cb794589c89d0e2c55f851f5359ee309958de063e`。正序、逆序均为 **1,528/1,528**，场景深度哈希均不变。完整检查来源列表已覆盖新 `actions/` 子目录。

| 正式集合 | 第 1 轮 | 第 2 轮 | 与 M0 比较 |
|---|---:|---:|---|
| 产品 | 1,718/2,000 | 1,718/2,000 | 2,000 个行为签名逐项一致；异常和安全事件均为 0 |
| 协调 | 1,448/1,448 | 1,448/1,448 | 逐项一致 |
| 扩充故障 | 16/16 | 16/16 | 逐项一致 |
| 正式跟随 | 10/10 | 10/10 | 逐项一致 |
| 世界变化 | 19/19 | 19/19 | 逐项一致 |

两轮对应索引哈希也相同。每组完成后立即比较 M0，未改归一化或签名口径。紧凑索引和原来源摘要分别保存在 `round1/`、`round2/`；原始轨迹留在忽略目录，位置由最终清单记录。清单保存正式摘要、索引及紧凑证据的哈希。

### 3.3 Windows 性能

使用现行 D058、D061 默认正式参数，未用 test-mode 或缩短样本。性能在其他正式集合结束后独立运行。

| 项目 | 实测 | 判定 |
|---|---|---|
| D058 路线复核各组 P95 | 最大组 0.2845 ms | 全部通过 |
| D058 完整准备 | 保留 3413；P95 5.0832、P99 5.1685、最大 14.2280 ms | 通过 |
| D061 长会话准备 | 保留 4096；P95 5.2844、P99 5.4341、最大 15.5603 ms | 通过 |
| D061 完整控制路径 | 4096 帧，最大 44.8098 ms；期限错失 0 | 通过 |
| D061 GC / pacing / trace / 身份 / 行为 / 安全 / 终态 | 正式门槛全部通过；保留区间包含 Gen2 及其后完整观察 | 通过 |

性能 JSON 保存全部原始计时样本和门槛。D061 原记录约 12 MB，主要是 17,337 条详细 GC 事件。紧凑版保留全部 82 条 Gen2 事件，其他 GC 保留原计数和判定；未改任何计时样本、统计或门槛。原文件完整保留在忽略目录并记录哈希。紧凑版最大文件为 306,067 字节。

最终归档发现 M0/M1 原先没有沿用 S0-R 的 `-text` 规则：32 个已跟踪证据的工作区字节与 Git blob 因行尾转换不同。现已为两个证据目录补齐规则，重新暂存原 Windows 字节，并逐个核对 blob 与清单。未改证据内容或结果，也未重跑生产行为。该修正保证后续检出仍能按清单复核原证据哈希。

正式采样并未把准备时间当成完整控制路径时间。没有做性能局部调整。此轮没有新增 Fabric 验收，也不扩大任何已有能力。

## 4. fail fast 与提交记录

| Task | 提交 | RED / 修正 / GREEN |
|---|---|---|
| 1：量尺 | ad83d82、51f3759、233e6d1 | 模块不存在及映射/字段/声明反例 RED；修正口径后 7/7 GREEN |
| 2：后台不变量 | 826f548 | 控制侧计算、阻塞读取错误副本均被发现；原生产 worker 15/15 GREEN |
| 3：死路径 | 1fe4486 | 三项逐项计数变异；新版代表入口每项 61 场零调用，结合原全量矩阵和静态零引用后删除；直接 73/73 |
| 4：种子与驱动器 | b1922be、af0fe41 | 正式 READY 拒绝回执触发状态分裂 RED；最小 Session 同步及即时 body_handoff 后 34/34；完整检查暴露 fixture 协议缺失，补齐后 follow 19/19 |
| 5：M0 起点 | 233e6d1 | 最终正逆各 1,509；只读量尺最终新增组件直接 7/7；五组一轮 3,493 个签名一致 |
| 6：动作契约 | 3b99f96 | 契约不存在 RED；显式声明、不可变和未知拒绝 5/5；直接集合 37/37 |
| 7：等价语料 | b6a56b2 | 错误伤害 spec、吞异常和 frozenset 编码反例；组件 8/8，459 场比较及直接几何回放零差异 |
| 8：风险/前提/安全 | 4ab6c2a | spec 消费、冻结边界、一次伤害记账和身体承诺正确 RED；直接 71/71 |
| 9：求解 | 0797eba | 修改登记的求解类别/方向先 RED；迁移后直接 66/66，保留通用恢复 |
| 10：执行/完成 | 25ef3e0 | 新身份先被旧白名单拒绝，完成查询消费先 RED；执行直接 116/116；缺 AIR 工厂声明 RED 后契约/扩展 18/18 |
| 11：收尾与验收 | b39da9a 及本次文档提交 | 删除比较副本后直接 16/16；正逆 1,528、五组两轮和两项性能全部通过；Session -52 仍使 M1 结构失败 |

没有降低 Session 的 100 行门槛，也没有把仍有职责的协调代码移入 spec。独立复核无未关闭 P0/P1/P2；其结论支持行为一致时保留本候选，不等于签署 M1 通过。

## 5. 最终命令

实施时把实际运行命令和结果写在这里。不得用计划中的预计数字代替真实输出。

M0 正式命令通过 `D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python` 调用：

```text
-m unittest tests.motion_nav.test_planner_worker -v
-m unittest tests.motion_nav.test_runtime_backend_io tests.motion_nav.test_navigation_supervised_interruptions -v
-m unittest tests.motion_nav.test_navigation_session tests.motion_nav.test_motion_baseline_recovery tests.motion_nav.test_navigation_design_metrics tests.motion_nav.test_navigation_dead_path_probe -v
-m unittest tests.motion_nav.test_known_world_follow_driver -v
-m unittest tests.motion_nav.test_navigation_design_metrics -v
scripts/run_motion_navigation_checks.py --output evidence/motion_navigation/redesign-m0/checks-forward-final.json
scripts/run_motion_navigation_checks.py --reverse --output evidence/motion_navigation/redesign-m0/checks-reverse-final.json
scripts/navigation_s0r_evidence.py --set <product|coordination|faults|follow|world_changes> --output .tmp/redesign-m0-<set> --workers 4
scripts/navigation_design_metrics.py --output evidence/motion_navigation/redesign-m0/metrics-baseline.json
scripts/navigation_historical_seed_probe.py
scripts/navigation_dead_path_probe.py --target <each frozen target> --representative --output <new counted evidence file>
```

其中完整检查为最终两轮 1,509 项；量尺最后增加的三项检查另以直接 7/7 签署。每组索引都用 `compare_indexes` 与 S0-R 原对应索引比较，差异均为零。


M1 最终生产来源 b39da9a，正式命令：

```text
-m unittest tests.motion_nav.test_action_specs tests.motion_nav.test_action_spec_integration
scripts/run_motion_navigation_checks.py --output evidence/motion_navigation/redesign-m1/checks-forward.json
scripts/run_motion_navigation_checks.py --reverse --output .tmp/m1-checks-reverse.json
scripts/navigation_s0r_evidence.py --set <product|coordination|faults|follow|world_changes> --output .tmp/redesign-m1-round<1|2>-<set> --workers 4
scripts/benchmark_d058_route_revalidation.py --output .tmp/redesign-m1-d058
scripts/benchmark_d061_long_session.py --output .tmp/redesign-m1-d061
scripts/navigation_design_metrics.py --output evidence/motion_navigation/redesign-m1/metrics-final.json
```

正序报告随后移入忽略目录，避免下一组采集因待提交证据显示工作树脏；最终统一复制两份原报告。完整行为采集直接调用现有 collect，每组立即使用 compare_indexes 比较 M0。性能各自保存实际 COMMAND.txt。最终证据完整性见 final-manifest.json；性能原始轨迹位置及文件哈希见各自 raw-location.json。
