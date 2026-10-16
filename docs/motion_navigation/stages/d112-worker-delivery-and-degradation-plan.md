# D112 后台动作交付与跨设备降级计划

日期：2026-10-16。决定见 [D112](../decisions/0112-separate-reference-calibration-from-runtime-delivery.md)。

当前状态：**D112-0 至 D112-4 已完成；D112-5 未通过，D112 FAILED／STOPPED。**

## 1. 交付目标

本阶段把性能门槛放到玩家实际经历的控制链上：控制线程保持实时，后台结果在许可窗口内转成实际输入；较慢设备来不及时，机器人安全等待或有界结束。

## 2. 实施顺序

### D112-0：只读基线和校准规则

不改运动逻辑：

- 从 D111 原始 trace 重算控制准备耗时；
- 从默认协调链重算候选首次取用、首输入窗口和实际生效 tick；
- 增加固定单候选 R4 求解校准，预热后分别在正式运行前后采样；
- 记录机器与电源计划，不读取程序命令行；
- 根据成对数据冻结校准漂移容差，再进入 D112-1。

实际结果：D111 的 5,399 个控制准备样本为 P95 3.5100 ms、P99 29.6744 ms、最大 42.3297 ms；188 个 coordinator 帧的 `proposal_elapsed_ns + runtime_outside_backend_ns` 为 P95／P99／最大 4.2588／4.6382／4.7747 ms。10 个正式候选均至少提前 1 tick 被取用，首输入距 latest 均剩 1 tick，20 条 verified 命令窗口错过为 0。两轮当前短校准 wall P50 为 24.8475／24.7543 ms，工作量和输入签名一致；前后 P50、P95 漂移门槛冻结为各不超过 10%。

停止条件：字段不足以形成明确口径，或不同现有证据使用的计时区间无法对齐。发生时先修量尺，不修改动作行为。

### D112-1：冻结 RED 与封住同步回退

先增加失败检查，再实现：

- 受支持动作缺少 `precomputed` 时返回 `PRECOMPUTED_REQUIRED`；
- patch 求解器为异常也不能触发内联求解；
- B10 同步求解超过 30 ms 只产生警戒，候选和 tick 上限仍阻断；
- D111 历史失败断言保持不变。

实际结果：控制路径已经删除两个内联求解入口。缺少后台结果时统一返回 `PRECOMPUTED_REQUIRED`。同步 B10 探针只报告墙钟警戒；候选数和命令长度仍是硬门槛。

### D112-2：补齐端到端交付证据

按 computation identity 记录 worker submit、worker elapsed／完成、receiver observed、registry publish、first consume、grant、requested earliest/latest 和 actual effect tick。

成功门槛使用 tick：

- 首次 consume 领先首条 earliest 至少 1 tick；
- 首输入 `actual_tick <= latest_tick`；
- 窗口错过为 0；
- 毫秒分段只用于诊断。

实际结果：worker 结果增加完成时刻；receiver 记录 submit、worker elapsed／完成、observed、publish 和 consume。正式 coordinator 帧同时记录 grant、执行窗口与 actual effect tick，可以按同一 computation identity 重算完整链。

### D112-3：确定性迟到矩阵

先跑 33 个组件场景：

- gap、jump-up、controlled-drop × 0／2／5／10 tick；
- gap 其余三个方向 × 0／5／10 tick；
- 三种动作各一个 alive-never-return；
- 三种证明各在首／中／末安全边界丢失下一命令。

任何伤害、盲目起跳、Runtime 失败、owner 泄漏或无界等待都立即停止。`release_safe_command_indices` 只作为确定性 oracle；不能写成正式执行器已经消费该字段。

实际结果：33／33 通过。0／2／5 tick 三种动作和 gap 四方向全部完成；10 tick 全部完成或安全有界结束；三个 alive-never-return 和九个安全边界丢失下一命令场景均零伤害、无盲启，空中 owner 连续，终态释放输入源。旧 4 tick 已验证中性准备上限是 5 tick 固定延迟失败的直接原因，现统一为 5 tick；10 tick 上限没有扩大。

### D112-4：Windows 回归门禁

依次运行：

1. 直接与相邻检查；
2. 源树正序、逆序分片；
3. 独立公开快照正序、逆序；
4. Java、导出和 `verify`。

实际结果：源树正序、逆序各 188／188 个模块、2347 项；独立公开快照正序、逆序各 187／187 个模块、2323 项；四组均零失败、错误、超时、跳过和 cleanup 异常。源树与快照 Java 各 3／3；独立快照 `verify` 为 2338 个文件。

### D112-5：唯一一次正式 Fabric

只有 D112-0 校准合格且 D112-1—D112-4 全过才运行。一个正式会话内登记 56 个 coordinator 场景：

- 基线：四方向各 5 次，共 20；
- 首条输入晚 1 tick：四方向各 2 次，共 8；
- worker 延迟 2／5／10 tick：每档四方向各 2 次，共 24；
- worker 永不交付：四方向各 1 次，共 4。

现有 Fabric 后端没有按 movement tick 推迟一条已提交输入实际生效的测试接口。8 个 late1 场景因此保留为明确的 `fabric_input_late1_injection_unavailable` 记录，不用 `sleep()` 伪造，也不在本阶段扩展客户端协议。D094 已有四方向真实晚一 tick 的历史证据，本阶段新增执行其余 48 场。

worker 延迟按 movement tick 注入，不用 `sleep()` 决定行为。2／5 tick 必须完成；10 tick 可完成或安全有界结束；永不交付必须得到有类型的安全失败。全部实际执行场景要求零伤害、零窗口错过、Runtime 可用、来源和 worker 有界释放。

正式运行前后各做一次固定校准。校准不合格时保留功能结论，性能不签署，也不追加重跑。

第一次尝试因 `delivery`／`deliveries` 字段错误没有写出 D112 coordinator 终态，记为 `INVALID_HARNESS_RUN`。第二次完成 28 个可执行场景后，以客户端脱敏 `IllegalStateException`／等待观察超时结束，也记为 `INVALID_HARNESS_RUN`。调查时发现外层 600 秒没有传入部署客户端的独立缺口并予以修正；最终运行仍出现后续客户端异常，因此不把第二次失败归因于期限。两次原始证据均保留。

修正期限传递后的最终正式批次取得：

- 基线 20／20 完成；
- 2-tick worker 延迟 8／8 完成；
- 首输入 late1 的 8 条仍明确记录为 Fabric 不支持；
- 5-tick worker 延迟第一场在三次结果交付后以 `motion_retry_exhausted:new_motion_grant_has_no_start_slack` 安全失败，着地、零伤害、来源显式释放；
- 28 个已执行成功动作的 identity 链完整，首次 consume 均领先 earliest 1 tick，verified 窗口错过为 0；
- 控制准备 trace P95／P99／最大为 4.8791／22.4488／29.9488 ms；P99 未达到 15 ms；
- 前后校准 P50／P95 漂移为 +0.09%／−0.14%，可比较。

5-tick 完成门槛和控制准备 P99 门槛都未通过，因此 D112-5 失败并停止。随后出现的客户端 `IllegalStateException`／等待观察超时单独登记；10-tick 和 never-return 的 Fabric 场景没有运行，不能写成通过。

## 3. 复杂度和停止规则

- 不修改求解算法；只统一已验证准备前缀的 5 tick 上限；
- 不新增 Session 状态、等待或重试类型；
- 延迟 worker 只存在于验收夹具；
- 不扩展到连续两跳；
- 最多一轮限定修正。若正式 worker 链需要新的生产状态机才能通过，停止并报告。

## 4. 完成条件

- D112-0 的校准口径和容差已经冻结；
- 控制线程不存在同步动作求解入口；
- 端到端 tick 链完整且可按 identity 独立复算；
- 33 个组件场景通过；
- Windows 与公开快照完整检查通过；
- 唯一 Fabric 固定矩阵的 48 个可执行场景满足各自预登记结果，8 个未支持场景保持显式记录；**未达到**；
- D110-SOLVE-01 仅在上述条件全部通过后关闭。

D110-SOLVE-01 保持打开。连续两跳、零等待交接和新动作仍未授权。
