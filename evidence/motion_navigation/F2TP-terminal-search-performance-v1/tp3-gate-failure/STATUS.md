# F2-TP TP3 门槛结果

状态：未通过，已按 fail-fast 停止。

- Windows 干净提交：`a61a76e6a245cd2f4b808ba3eed581a6dc4722e6`。
- 聚焦检查：183/183。
- worker READY，控制 PID 5456，worker PID 17532。
- phase P95 3.8161 ms，最大 4.1583 ms。
- beam P95 125.8381 ms，最坏 125.9636 ms，满足 `<500 ms`。
- queued CANCELLED／STALE／TIMEOUT 均为 typed 结果，beam 生成节点为 0；运行中取消有组件检查。
- 五个非 oracle 正式链共 142 个控制帧。production P95/P99/max 为 10.2973/22.9633/24.9259 ms，未满足 8/15/30 中前两项。
- full max 44.8677 ms，满足 `<50 ms`。backend 单独报告。
- 因 production 门槛失败，没有运行任何 oracle、remaining9、remaining42、F2-GP、大集合、D061 或 Fabric。
