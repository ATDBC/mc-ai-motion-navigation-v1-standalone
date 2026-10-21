# D122-3 正式组件链与转换矩阵

本目录记录 D122-3 的 Windows 组件验收。没有运行 Fabric，也没有改写 D121 历史证据。

## 正式 Runtime 代表链

`formal-matrix.json` 曾通过正式 Runtime → RuntimeNavigationDriver → NavigationSession → ActionRouteExecutor → prepared successor 链运行。四个方向各覆盖：

- `timely_l1`：后继在 `L+1` 首次真实采样并完成；
- `late_l2`：后继在物理前得到 `window_missed`，跳跃真实采样为 0，父 default 连续运行证明给出的 9 tick；
- `never_delivered`：请求经过 Runtime 的唯一 backend 出口后在到达客户端前丢失，客户端不依赖新的 Python 观察，自主执行 9 tick default 并安全停住。

独立审查发现，这份初始矩阵的 `never_delivered` 4 场不能判为通过：后端完成默认后抛出 transport error，probe 手写的 prepare／control／adopt 循环没有走 driver 的 `discard_prepared()`；四场均留下 `source_released=false`、driver `stopping` 和未清 prepared frame。`formal-matrix.json` 保留为生命周期误判的失败证据，不回写。

修复后的 `formal-matrix-final.json` 让计时 probe 和正式 `RuntimeNavigationDriver.tick()` 共用 `advance_prepared()`。Runtime 调用、异常时丢弃 prepared proposal 和结果接纳只保留一套。never 在客户端自主执行完整默认后返回 typed `successor_transport_not_delivered` rejection；Runtime 和 driver 随后完成既有收尾。

正式 producer 只在收到 `L=26` 的落地观察后产生后继，因此不存在 `L` 之前的 future-pending formal 场景。最终正式矩阵按四方向覆盖五个可达状态：按时、晚到、失联、明确拒绝和任务取消，共 20／20 通过。early 继续由同一 ClientTimingSampler／v4 receipt／ledger 转换表检查覆盖，不伪造 proof 或提前生成正式后继。所有正式场均无异常、source 已释放、prepared frame 已清空，driver 有界终结，零伤害。取消走现有身体责任收尾，不冒充 default 执行。

最终矩阵的 708 个同步控制样本中，提交最大值 11.3745 ms；控制 P95／P99／最大值为 0.6061／3.9415／4.4735 ms。合法响应等待继续单列。

## 扩展转换矩阵

`lifecycle-matrix-tests.txt` 运行现有 v2 sampler、v4 receipt 和正式 InputApplicationLedger，覆盖 future pending、higher-sequence pending 替换、reset、已接纳 cancel、未接纳 reject、替换后新 pending miss，以及 pending／armed 不能算应用。6 项全部通过。第一次命令拼错测试类名的结果保留在 `lifecycle-matrix-tests-red.txt`，它不是产品失败。

## 有限入口与 mutation

`d120-default-replay.json` 是 D120 的 972 个有限入口物理尾迹重放，再加 D122 代表链的客户端真实采样。它不是 972 条完整 Runtime 运行，也不构成连续安全包络。972／972 都在第 8 个潜行物理样本达到 D120 的停稳条件。正式链不把这个 8 写进代码；它采用已接纳 proof 中更保守的 9 个潜行输入，并在第 10 个边界用独立的中性 release witness 结束责任。

`mutations.txt` 覆盖：把窗口放宽到 `L+2`、把 proof-selected default 改为中性、把 pending／armed 当作应用。3 项都被现有断言检出。

## 能力和期限

Runtime reset 后把握手得到的 action schema 固化到新 InputApplicationLedger。默认 v1 ledger 不投影首效窗口或 default；D122 formal probe 显式请求 v2，并使用 1 s owner 窗口。现有普通 250 ms／500 ms 路径没有全局放宽。父 default 的动作、激活 tick 和持续时间来自已接纳的 `SNEAK_EDGE_STABLE` fallback proof；Session 只投影，不自行判断安全或硬编码 8。

第一次完整正序分片保留在 `source-forward.json`。它按 fail-fast 停在旧输入失联恢复检查。逐字段核对发现，第 69 项／movement tick 71 只有 Runtime ledger 的 `input_window` 元数据变化：旧值为 requested 71、latest 72、last 71，新值一度变成 requested／latest／last 71；位置、速度、着地、实际输入和动作种类逐项相同。该结果暴露出 v1 兼容边界放得过早。最终实现保留 ActionIntent 和 Runtime ledger 的既有内部窗口，只在 `_backend_step` 前去掉 v1 wire 的新字段，因此冻结前缀重新逐项一致；D122 专项还同时断言内部窗口存在、backend 实收的 v1 action 没有窗口或 default。

第二次完整正序分片保留在 `source-forward-final.json`。它继续暴露两个相邻回归后停止：Session 一度从具体 executor 读取 timing capability，使未初始化 prepared 私有字段的取消测试替身报错；同一过早 capability gate 还删掉普通已证明静止步行的一拍内部 slack。最终实现改为从当前正式 `self._control_ledger` 读取能力，并采用上述内部窗口／v1 wire 分界。两个失败模块和 D122 定向检查合计 134／134 通过。两次失败记录与历史证据都没有改写。

审查修复前的源码树正序、逆序结果分别保存在 `source-forward-final2.json` 和 `source-reverse-final.json`：每个方向均为 204／204 个模块、2675 项检查通过。审查修复改变了 driver 与测试组件，因此这两份结果只保留为中间记录。审查修复后的 D122 定向集合为 64／64 通过，见 `d122-directed-final2.txt`；最初的 never 生命周期 RED 保存在 `never-lifecycle-red.txt`。

第二轮独立代码审查通过后，最终源码树正逆分别记录在 `source-forward-postreview-final.json`、`source-reverse-postreview-final.json`：各 204／204 个模块、2680 项通过。独立 standalone 正逆分别记录在 `standalone-forward-postreview-final.json`、`standalone-reverse-postreview-final.json`：各 203／203 个模块、2656 项通过。四份完整分片的失败、错误、超时、跳过、预期失败、意外成功和清理失败均为 0。

`java-postreview-final.txt` 是没有注入项目 JDK 时的 `INVALID_ENVIRONMENT`，没有进入编译；`compile-java-postreview-final.txt` 是从子目录使用错误相对路径的 `INVALID_ENVIRONMENT_COMMAND`，没有进入 Gradle。使用 `.venv/Library/lib/jvm` 后，JDK 发现见 `java-postreview-final2.txt`，94／94 Java／Python 相邻门禁见 `java-gates-postreview-final.txt`，成功的 overlay 编译见 `compile-java-postreview-final2.txt`。standalone 首次导出与校验日志分别为 `standalone-export-postreview-final.txt`、`standalone-verify-postreview-final.txt`；文档和最终证据索引写回后重新生成，最终索引为 2969 个文件并通过哈希校验。

`production-semantic-hashes.json` 只覆盖生产语义。`tests/sim/backend.py`、`tests/sim/client_timing.py` 和正式组件 probe 属于组件量尺与证据生成支持，已在记录中显式列为排除项，不冒充生产字节。

`final-gates.json` 汇总最终四份完整分片、Java、overlay、组件结果及其 SHA-256。它明确记录两次环境命令无效运行没有进入代码检查，也明确记录本阶段没有运行 Fabric。

## 边界

D122-3 只签署组件与转换表。Fabric D122-4 未运行。四格正式产品规划、连续入口包络和新的动作能力仍未授权。
