# F1 后导航结构整理验收

日期：2026-10-06。当前状态：S0-R 已在正式 Windows 环境完成并签署；S1 可以开始但尚未实施。平台范围见 [D071](../decisions/0071-windows-is-the-formal-project-platform.md)。

## 1. S0 已完成的部分

S0 只增加只读量尺和测试，没有修改生产导航代码。基线来自干净提交 `a1649c2`，生产代码与父提交 `2f0e46b` 相同。

`r28-structure-trajectory-v1` 会保留字段、列表顺序和数据类型，只把有限浮点数取到小数点后 9 位，并把随机身份按出现顺序改成局部编号。

| 集合 | S0 结果 | 说明 |
|---|---:|---|
| v7 产品清单 | 2,000 项；1,718 项完成 | 零异常、零安全事件；未完成项留在分母中 |
| 协调清单 | 1,448/1,448 | 中断、目标修订和异步交付组合 |
| 补充故障 | 4/4 | 不计入产品成功率 |

三方在 Linux 上复跑后，三组签名与 Windows 索引逐项一致。因此保留签名格式和 S0 历史证据。

## 2. S0 为什么不能直接进入 S1

S0 的删除清单只有三个零引用函数：

- `NavigationSession._admit_async_event`；
- `NavigationSession._cell_fact_id`；
- `MotionRouteCoordinator._upcoming_air_index`。

它们合计只有 34 行，没有覆盖 F1 增长最多的接纳与复核代码。原三组基线也没有走到 `admit_ground_direct` 的主要路径。只删除这三项就签署结构通过，会漏掉本阶段真正要解决的问题。

三方还复现了四类基线缺口：

1. 模块级共享场景会被测试改写；
2. 石头与草方块不再触发行走恢复，两个测试的前提已经失效；
3. `seed 163` 在空中把 `needs_state` 误当成明确无解；
4. 后端 I/O 失败后，驱动器终态被覆盖并复用旧帧，最终抛出契约异常。

真实墙钟还会改变部分单元测试结果。以上问题都要先处理，否则结构差异没有可信量尺。

## 3. S0-R 验收门槛

下面记录正式 Windows 环境的验收结果。Linux 只用于补充可移植性复核：

| 编号 | 验收内容 | 当前状态 |
|---|---|---|
| A1 | Windows 完整运动导航检查正序、逆序均为 0 失败、0 错误 | 通过；最终正逆序 1499/1499（`3dd6bc1`） |
| A2 | `expectedFailure` 为 0；`seed 163` 已修复并保留回归 | Windows 通过；expectedFailure=0，seed 163 回归成功 |
| A3 | 完整套件前后，共享场景深度哈希相同 | Windows 通过；新正逆序与先前四次1491检查hash均相同 |
| A4 | 改写的测试断言目标路径实际进入，旧前提反例会明确失败 | Windows 通过；目标入口断言与旧材料反例均通过 |
| A5 | 点任务与跟随任务的 I/O 故障族均给出类型化终态，无异常逃出和终态后输入 | Windows 通过；12 项 I/O 故障族，扩充集合共16项 |
| A6 | 单元测试不靠真实墙钟判定通过；并行负载不改变功能失败集合 | Windows 通过；确定性时钟专项及两份并行1491项检查全绿 |
| A7 | 产品、协调、故障、跟随、世界变化五组索引在 Windows 连续两次一致 | 通过；五组两轮逐项一致 |
| A8 | 路径矩阵由脚本生成，覆盖 v2 清单中的每个候选 | 通过；完整独立矩阵覆盖16个审查项×五组 |
| A9 | v2 清单覆盖 K1—K6 和 F1 新增协调代码，每项字段完整 | 通过；K1—K6及F1变化已登记，S1／S2变异未执行 |

`seed 163` 不允许用预期失败绕过。它位于后续要整理的恢复链上；不能修好时，S0-R 直接记为未通过。

## 4. S0-R 要冻结的五组集合

| 集合 | 预定内容 | 最低覆盖要求 |
|---|---|---|
| 产品 | 固定 v7 2,000 项 | 维持现有产品结果、原因、输入、轨迹、风险与后台作业签名 |
| 协调 | 固定 1,448 项 | 中断、修订、异步交付和身体交接 |
| 补充故障 | 修正后的原 4 项，加 I/O 故障族 | 世界变化恢复、运行时失败和安全退场 |
| 正式跟随 | F1 的 10 个模拟场景 | `admit_ground_direct`、持续修订、长期待命和取消 |
| 世界变化 | 点任务与跟随任务中的路线障碍变化 | 行走复核的继续与停止分支 |

五组行为签名与路径计数分开运行。路径探针只能证明入口被走到，不能作为产品性能或行为证据。

## 5. v2 清单的关闭规则

每项必须记录分类、重复或迁移关系、保留 owner、五组调用次数、变异检查、预计变化、风险和关闭结果。

允许的关闭结果只有：

- `deleted`；
- `merged`；
- `retained:independent_responsibility`；
- `retained:unprotected`；
- `retained:merge_changes_behavior`；
- `retained:out_of_time`。

相同条件不能直接当作重复。若首次接纳和运行时复核需要在不同时点各自保护同一不变量，应记录为独立职责并保留。

## 6. S1—S4 的行为不变门槛

每个候选按下面顺序检查：

1. 直接相关测试；
2. 正序和逆序完整运动导航检查；
3. 五组 S0-R 行为索引逐项比较；
4. 路径矩阵和清单关闭状态。

任何一步不一致，立即回退当前候选。不能把差异解释成改进后重写基线。

最终签署还要求：

- 清单闭合率 100%；
- K1—K5 中没有 `retained:unprotected` 或 `retained:out_of_time`；
- 十二个核心协调文件加 `route_validation.py` 相对 S0-R 总行数和决定分支减少；
- 没有新增生产状态字段、公共接口或文件搬迁；
- 跟随结构审计继续通过；
- 五个已签署 Fabric 目录的摘要与哈希不变；
- 新证据只保存索引、摘要和差异，单个文件小于 1 MB。

## 7. 计划中的检查入口

实施时至少记录这些命令的原始结果：

```powershell
python -m unittest discover -s tests/motion_nav -p "test_*.py"
python scripts/navigation_coordination_metrics.py baseline --manifest tests/sim/manifests/navigation-product-r28-v7.json --output <new-output> --workers 4
python scripts/navigation_structure_baseline.py index product --source <new-output> --output <new-index>
python scripts/navigation_structure_baseline.py compare --baseline <s0r-index> --candidate <new-index>
python scripts/run_f1_known_world_following.py --output <new-follow-output>
git diff --check
```

逆序完整检查、世界变化集合、扩充后的故障集合和路径矩阵需要在 S0-R 中补成正式入口。验收记录必须写出最终命令，不能只引用本地临时脚本。

## 8. 证据边界

- 本轮新增 Windows 证据，S0-R 已按正式环境签署。
- 没有运行 Fabric。S0-R 只修改模拟、测试和两个正式缺陷；是否需要 Fabric，由修复是否改变正式输入与动作语义决定。I/O 失败至少要核对 Fabric 后端的异常映射。
- 原 S0 索引和失败证据继续保留。S0-R 使用新目录，不能覆盖旧目录。
- Linux 尚未复跑，因此本轮不声明跨平台一致；这不影响 Windows 阶段结论。

## 9. S0-R 的实际结果与命令

| 集合 | 分母 | Windows 两轮结果 | 两轮秒数（含索引） | 行为来源 |
|---|---:|---|---|---|
| product | 2000 | 1718 完成，0 异常／安全事件 | 348.485／348.041 | `c546119` |
| coordination | 1448 | 1448/1448 符合各自判定 | 147.534／147.771 | `f3551c3` |
| faults | 16 | 16/16 符合各自判定 | 1.266／1.246 | `f3551c3` |
| follow | 10 | 10/10 符合各自判定 | 12.592／12.566 | `c546119` |
| world_changes | 19 | 19/19 符合各自判定 | 3.373／3.359 | `c546119` |

工具与场景先做最小样本：产品10项、协调6项、故障16项、跟随10项、世界变化19项，两轮签名均相同。工具组件与 B1／F1 检查18/18通过；verification v2 的8项工具检查通过，status、coverage、gaps 的任一变化都改变签名。先在干净 `c546119` 运行五组两轮，再在干净 `f3551c3` 重采受影响的协调／故障两轮；预修记录不改写。

正式命令均从仓库根目录通过 `D:/Miniforge3/Scripts/conda.exe run --prefix D:/My_project/mc_ai/.venv --no-capture-output python` 调用：

```text
scripts/navigation_s0r_evidence.py --set product --workers 4 --output <new-product-run>
scripts/navigation_s0r_evidence.py --set coordination --workers 4 --output <new-coordination-run>
scripts/navigation_s0r_evidence.py --set faults --workers 4 --output <new-fault-run>
scripts/navigation_s0r_evidence.py --set follow --workers 4 --output <new-follow-run>
scripts/navigation_s0r_evidence.py --set world_changes --workers 4 --output <new-world-run>
scripts/navigation_structure_baseline.py compare --baseline <run1/index.json> --candidate <run2/index.json>
scripts/navigation_structure_paths.py --workers 4 --output <new-path-matrix.json>
scripts/navigation_structure_inventory.py --matrix <path-matrix.json> --output <new-inventory.json>
```

完整独立路径运行用时 484.918 秒，仅为收集器耗时，不作为性能门槛。路径场景分母为 product=2000、coordination=1448、faults=16、follow=10、world_changes=19。

世界变化同时覆盖点任务和正式跟随，在4／10／18 tick施加整块障碍、等价普通材料和下半砖变化。独立探针实际记录 replay 的 feasible=15、blocked=16。行为摘要另保存正式 diagnostics 的非零 queries_used、continue/revalidated 与 stop/blocked，以及实际恢复次数；入口依据不是终态猜测。

新证据位于 `evidence/motion_navigation/post-f1-structure-s0r/`，逐文件哈希与提交来源见 baseline-manifest。产品与旧 S0 的2,000个行为签名全相同，完成数仍1718；其余集合新增或升级签名格式，不冒充与旧格式逐项相同。路径探针与行为签名分进程采集，所有证据文件小于1MB；大轨迹不进入Git。

v2冻结14个待审候选及2个audit_only审查项。原三个死路径只是静态／动态零入口候选，S1断言变异尚未执行。K1／K2／K3／精确K4／K5仍待S2 owner与变异审查；零入口明确保留不改。S0-R冻结清单不等于完成S2关闭。

WSL 无法启动，错误为 `HCS_E_HYPERV_NOT_INSTALLED`。这只说明本机没有 Linux 补充结果。Windows A1—A9 已满足，S0-R 完成；S1 可以开始但尚未实施。


## 10. 最终工具版本的完整检查与证据自校验

最终生产与测试源码来自 `3dd6bc1`。正序、逆序各1499/1499，用时330.300／330.943秒；failure、error、expectedFailure、skipped均为0。两次共享场景前后深度哈希均为 `05cfbd29ba3067b4ed2eece3f59b5afbbff619cb611fe9f0c30a776b4b813161`，生产文件指纹相同。

原四次1491项运行保留为A6的并行与隔离证据。新增工具检查后，A1的Windows结果使用本节1499项。运行元数据的worktree_clean=false是未提交的文档／证据包装；生产与测试源码没有未提交差异。行为集合仍按各组的c546119／f3551c3记录，final_verifier_commit单独记录3dd6bc1。

正式入口：

```text
python scripts/run_motion_navigation_checks.py --output <new-final-forward.json>
python scripts/run_motion_navigation_checks.py --reverse --output <new-final-reverse.json>
python scripts/navigation_structure_baseline.py verify --root evidence/motion_navigation/post-f1-structure-s0r
```

只读 verifier 检查 manifest 中的两轮 index、path matrix、inventory、compact summaries 全部引用，并逐项核对 SHA256SUMS；SHA 文件不自哈希。原始大轨迹未放入公开目录。Windows A1—A9 已全部通过，S0-R 完成。Linux 复核仍可继续，但不阻塞 S1。
