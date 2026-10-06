# D068：普通直线步行的晚启动必须绑定输入账本窗口

日期：2026-10-06。状态：代码、正式链检查和单场 Fabric 已完成；两轮独立复审均没有未关闭的 P0／P1／P2。`first_input_late_one_tick_2_0` 已正式通过，F1-D 关闭。

## 1. 要解决的问题

第一次运行 `first_input_late_one_tick_2_0` 时，摘要写成通过：

`artifacts/f1-known-world-following/20261005T1707251056118Z-first-input-late-one-tick-2-0/`

逐帧复核发现首个移动命令请求在 tick 9 生效，实际在 tick 10 才生效，但输入账本允许的最晚 tick 仍是 9。账本状态应为 `APPLIED_OUTSIDE_WINDOW`。旧 runner 只比较了“正常偏移 1 tick、注入后偏移 2 tick”，没有核对正式许可窗口，因此误写了 `passed=true`。

原目录和原摘要保持不变，但不能作为 F1-D 通过证据。

## 2. 决定

普通步行的首条一 tick 输入可以容忍晚一 tick，但只限同时满足以下条件的动作：

- 身体着地、standing，水平速度接近零；
- 当前决定是 `RUNNING`，输入非中性，原控制器已经给出至少两 tick 的安全租约；
- 路线恰好是两点直线；
- 没有连续高度 traversal proof；
- transition 为空或明确为 `WALK`；
- 当前观察带有正式 movement tick。

动作 owner 产生 `[t+1, t+2]` 的 typed 启动窗口。Session 只把窗口放进移动意图，不保存第二份许可。Runtime 只有在该移动意图赢得仲裁后才把窗口登记到输入账本。

拐角、已经在移动的身体、制动、中性输入、单 tick 租约、Crouch、Sprint、连续高度、严格动作和空中动作都不能取得该窗口。以后若要支持这些情况，必须分别给出计算器或实机证据，不能扩大这条白名单。

## 3. 验收口径

最后一场只有同时满足以下事实才算晚一 tick 成功：

- 输入账本状态为 `APPLIED`；
- 命令 `valid_for_ticks=1`；
- requested first／last 都是 `t+1`；
- latest 恰好是 `t+2`；
- actual application ticks 恰好是 `[t+2]`；
- runner 的正常偏移为 1 tick，注入后偏移为 2 tick。

扩大 latest、重复应用、窗口外应用或只看墙钟延迟都不能通过。

组件和正式链检查覆盖白名单、持续移动反例、意图仲裁和 Runtime 输入账本。相关集合 111/111 通过；独立复审另跑聚焦集合 39/39 和相邻正式链 24/24，没有发现 P0／P1／P2。`git diff --check` 通过。

## 4. 实机结果

新目录为：

`artifacts/f1-known-world-following/20261005T1754012106271Z-first-input-late-one-tick-2-0-d068/`

来源提交为 `cf0aa13`。首条请求 seq7 的 requested first／last 为 9，latest 为 10，actual ticks 严格等于 `[10]`，账本状态为 `APPLIED`，`valid_for_ticks=1`。tick 9 仍是旧请求的 neutral／lease exhausted，tick 10 才由真实 Fabric receipt 应用 request 7 的 forward 输入。身体开始移动后，后续 route decision 的 expected／latest 立即恢复为空。

全场 186 帧连续。稳定样本 27，目标均速 1.873496 格／秒；修订响应 P95 为 3 tick；规划提交／修订为 1/11；stable excess mean／P95／max 为 0.008421／0.030496／0.196880 格；末距离 1.766775 格。task recovery 和安全违规为 0。目标满足后保持 `EXECUTING/QUIESCENT` 中性待命 126 帧，没有空闲 `STOPPING`。终态取消、source、host、time 和 cleanup 全部通过。

独立复审重算了 11 条分段流的记录数、字节数、segment SHA、manifest SHA 和完整流 SHA，结果全部一致。该场正式签署，F1-D 关闭。F1 整体仍需完成 F1-E 的结构与产品验收。
