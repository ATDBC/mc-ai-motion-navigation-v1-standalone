# D122-2 被拒响应等待限定修正

基于 `2db6e16b`，生产与测试修正提交为 `3eb13576`。没有进入 D122-3。

stage 前被拒绝的窗口后继不会立即释放响应。只要父 fallback 尚未完成，响应保持未就绪，直到父默认真实采样完整 duration 并报告 exhaustion。没有 fallback 时立即返回。硬许可释放使用 typed `HARD_RELEASE`，不靠 reason 字符串判断。

组合检查把真实 Java sample/event 组成 v4 receipt，送入 Python ledger。最终 successor 为 `REJECTED`，parent 收到 8 个默认 sample 并有界结束。最终相邻检查 94／94，Java mutation 19／19，Fabric overlay `compileJava --rerun-tasks` 通过。

未运行完整 Windows 分片、公开快照、性能验收、D122-3 或 Fabric。
