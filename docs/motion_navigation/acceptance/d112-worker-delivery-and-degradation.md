# D112 后台动作交付与跨设备降级验收

日期：2026-10-16。状态：**FAILED／STOPPED。**

## 1. 冻结范围

本验收只覆盖控制准备、后台动作求解交付、输入窗口、同步回退封口和较慢 worker 的安全降级。连续两跳、新动作和求解器优化不在范围内。

## 2. 预登记门槛

| 项目 | 门槛 |
|---|---|
| 控制准备 | P95 ≤ 8 ms，P99 ≤ 15 ms，最大值 < 30 ms；正式区间由 D112-0 固定 |
| 首次候选取用 | 领先首条 earliest 至少 1 movement tick |
| 输入实际生效 | 首输入不晚于 latest；固定矩阵窗口错过 0 |
| 同步探针 | 墙钟只报告；候选、命令 tick、队列和字节上限仍阻断 |
| 2／5 tick worker 延迟 | 任务完成、零伤害、Runtime 可用、来源释放 |
| 10 tick worker 延迟 | 完成或有类型的安全有界结果 |
| worker 永不交付 | 不起跳、不离边、零伤害、有界失败、owner／mailbox 清空 |
| 校准 | 工作量与输入签名一致；正式运行前后 wall P50、P95 漂移各 ≤10%；只判断性能证据能否比较 |

## 3. 验收结果

### 3.1 D112-0 只读基线

只读分析对象为 D111 本地原始批次 `20261015T151355333152Z-0f9aee96`：

| 指标 | 结果 |
|---|---|
| trace 控制准备，5,399 帧 | P50 1.7619 ms；P95 3.5100 ms；P99 29.6744 ms；最大 42.3297 ms；超过 50 ms 为 0 |
| coordinator 计算，188 帧 | `proposal + runtime outside backend` P95／P99／最大 4.2588／4.6382／4.7747 ms |
| 首次候选取用 | 10／10 完整；领先 earliest 1—3 tick，中位 1 tick |
| 首输入窗口 | 10／10 在 expected tick 生效；距 latest 均剩 1 tick |
| 全部 verified 输入 | 20 条，窗口错过 0；其中 10 条精确窗口命令合法地在 latest 生效 |

固定 R4 单候选短校准的两轮 wall P50 为 24.8475／24.7543 ms，P95 为 25.7323／25.1173 ms；输入签名和候选、命令、轨迹、依赖工作量逐项一致。结合另一次独立短采样约 0.5% 的自然波动，正式前后 P50 与 P95 漂移各 ≤10%。该条件只决定性能数据是否可用于签署，不限制设备。

D111 trace 的 P99 和最大值没有达到既有 15／30 ms 门槛。因为该运行没有前后校准且已知同时运行其他程序，本节不把它改写为通过或产品失败；它只冻结 D112 的正式计时口径。

### 3.2 组件矩阵

33／33 通过：

- gap、jump-up、controlled-drop 的 0／2／5 tick 延迟全部完成；
- gap 四方向的 0／5 tick 延迟全部完成；
- 10 tick 延迟全部完成或得到有类型的安全有界结果；
- 三个 alive-never-return 场景没有起跳或离边，均安全有界结束；
- 九个首／中／末安全边界丢失下一命令场景均恢复完成。

所有场景零伤害、无盲启、空中 owner 连续、终态释放输入源。`release_safe_command_indices` 只用于确定注入点，正式执行器没有读取它来决定动作。

### 3.3 Windows 回归

| 检查 | 结果 |
|---|---|
| 受影响与相邻 | 224／224；夹具修正后的聚焦检查 62／62 |
| 源树正序／逆序 | 各 188／188 个模块、2347 项 |
| 公开快照正序／逆序 | 各 187／187 个模块、2323 项 |
| 失败、错误、超时、跳过、cleanup 异常 | 四组均为 0 |
| Java | 源树、快照各 3／3 |
| 独立快照 verify | 2338 个文件 |

完整矩阵在正式 Fabric 前完成。Fabric 失败后只修正一处夹具字段名，并重跑 62 项受影响检查；没有把早先完整矩阵写成修正后最终字节的完整矩阵。

结果文档、夹具期限修正与精简证据收尾后，重新导出并校验当前公开快照，`verify` 为 2371 个文件；该检查不替代正式 Fabric 前已经完成的完整矩阵。

### 3.4 Fabric

登记 56 个固定场景。现有 Fabric 后端无法按 movement tick 推迟已提交输入的实际生效，8 个 late1 场景记录为 `fabric_input_late1_injection_unavailable`，不伪造结果；D094 的四方向真实晚一 tick 保留为历史参照。本阶段原定实际运行 48 场：baseline 20、worker 2／5／10 tick 延迟各 8、worker 永不交付 4。

首次尝试目录为 `artifacts/fabric-deployment/20261016T033758422187Z-0bf7d2cd`。第一次命令在构建前因 shell 未设置 `JAVA_HOME` 停止，没有启动游戏或产生试次，不计入运行。补入项目内 OpenJDK 21 后的尝试结果为：

- 82 个既有 B10 求解试次已写盘；
- 第一个 D112 coordinator 试次产生 20 个控制帧，但尚未写出试次终态；
- 夹具读取 `_receiver_diagnostic_summary()` 时使用了不存在的 `delivery` 字段，实际字段为 `deliveries`，因此触发 `KeyError`；
- D112 coordinator 试次记录为 0，48 场矩阵没有形成可验收结果；
- ordered source 为 `explicitly_released`，cleanup failure 为 0；
- 错误修正后 62 项聚焦检查通过。

该次尝试前后校准的工作量和输入签名一致。wall P50 平均值由 25.0685 ms 变为 24.7302 ms，变化 −1.35%；P95 由 25.1982 ms 变为 24.8358 ms，变化 −1.44%。机器状态满足 10% 可比条件，但没有 D112 coordinator 结果。

该尝试不包含可供门槛判定的 D112 结果，因此登记为 `INVALID_HARNESS_RUN`，原始证据保持。夹具异常不等同于机器人行为失败。

字段修正后的第二次尝试目录为 `artifacts/fabric-deployment/20261016T054616381907Z-d14c19f7`。它完成 20 个基线、8 条不支持记录和 8 个 2-tick 延迟场景；28 个可执行场景全部完成，identity 链完整，窗口错过和伤害为 0。随后在首个 5-tick 场景中，客户端只暴露脱敏 `IllegalStateException`，Python 等待下一帧观察超时。调查时发现外层 600 秒没有传给客户端的独立期限缺口并予以修正；最终运行仍复现后续客户端异常，因此不能把第二次失败归因于期限。该次尝试不能覆盖冻结矩阵，登记为 `INVALID_HARNESS_RUN`。前后校准 P50／P95 漂移为 −1.43%／−2.07%。

客户端期限传递修正通过 111 项聚焦与相邻检查。最终正式目录为 `artifacts/fabric-deployment/20261016T055758879589Z-f137ddfd`，结果如下：

| 场景 | 结果 |
|---|---|
| baseline | 20／20 完成 |
| first-input late1 | 8／8 明确记录为 Fabric 不支持，没有伪造 |
| worker delay 2 | 8／8 完成 |
| worker delay 5 | 第一场 0／1 完成；安全有界失败 |
| worker delay 10 | 未运行 |
| worker never return | 未运行 |

5-tick 失败场景为 `coordinator-worker-delay-5-0-0`。三个后台结果都在提交后 5 movement tick 被 receiver 看见，但每次取用时都没有新的启动余量。三次重试后终态为 `motion_retry_exhausted:new_motion_grant_has_no_start_slack`。身体保持着地，伤害为 0，来源显式释放。这是安全结果，但违反“2／5 tick 必须完成”的门槛。

28 个完成动作的证据链全部完整；首次 consume 均领先 earliest 1 tick，首输入距 latest 均剩 1 tick，56 条 verified 输入窗口错过为 0。`proposal + runtime outside backend` 的 P95／P99／最大为 3.1500／3.3558／3.4960 ms。完整 trace 控制准备的 P95／P99／最大为 4.8791／22.4488／29.9488 ms，P99 超过 15 ms 门槛。

正式运行前后校准输入和工作量签名一致。wall P50 平均值变化 +0.09%，P95 变化 −0.14%，因此性能数据可用于判断。

记录第一项 5-tick 行为失败后，后续等待观察以客户端脱敏的 `IllegalStateException` 和 Python `deadline_exceeded` 结束。该夹具问题影响未运行的后续矩阵，但不改变已经出现的 5-tick 与性能阻断结果。D112 因两项冻结门槛未通过而 **FAILED／STOPPED**，不再重跑。

## 4. 历史关系

D109、D110 和 D111 的原始结论不回写。D112 保持 FAILED／STOPPED。D110-SOLVE-01 保持打开。连续两跳、零等待交接和新动作仍未授权。
