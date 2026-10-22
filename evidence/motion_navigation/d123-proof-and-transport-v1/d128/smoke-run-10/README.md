# D128 smoke-run-10

状态：`INVALID_HARNESS_RUN/fixture-scheduling`。

正式进程已启动 Minecraft／Fabric，并正式 dispatch 第一个 movement request。该请求是 parent 的第 0 条命令：request 7、`forward=1`、`sprint=true`、`jump=false`。harness 在 movement tick 7 的 live anchor 上把 exact window 固定为 `[8,8]`，随后同步完成 proof／prepared worker 工作；请求到达客户端时已是 tick 34，并在 tick 35 记录 `window_missed`。

客户端没有应用任何非中性输入。physics sidecar 中 tick 8—125 的位置始终为 `(0.5, 64.0, -5.17)`，身体一直着地，health 从 20 保持到 20。parent 行为没有开始，parent safe default 没有安装，successor 也没有提交。driver 随后保守保持 body responsibility，90 帧上限触发的 release 没有冒充成功。

结果组装还有一个独立错误：row 按“首个带 `first_effect_window` 的请求”寻找 successor，因此把 request 7 的 parent 命令误写成 successor，并写出不成立的 `transport_outcome=successor_applied`。该字段不构成产品证据。

本次已经越过“首个 movement trial 提交前”的 D129 pre-trial 边界，因此不归类为 `INVALID_PRETRIAL_RUN`。它只在严格 post-submit invalid-harness 条件下允许修复重试：fixture 必须破坏正式注册前提；被测输入必须 0 次应用；身体不得进入被测行为；失败不得来自产品拒绝或安全逻辑。run-10 满足这些条件，没有产品结论。新的正式运行等待设计审查，matrix 未运行。

`manifest.json` 逐字节绑定四个 launcher 原始文件和 `source-run/` 中 46 个原始 source-run 文件。封存包含失败 trial row、worker／client 日志、时序、trace、F-TICK、physics、fixture、teleport、cleanup 和 server 日志；只排除生成的 server 安装、依赖库和 world 目录。
