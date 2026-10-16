# D112 精简证据

D112 的组件和 Windows 门禁已完成，正式 Fabric 结论为 **FAILED／STOPPED**。

- `windows-regression/summary.json`：源树正逆各 188 个模块、2347 项；独立快照正逆各 187 个模块、2323 项；四组全绿。Java 各 3／3，快照 verify 2338 个文件。
- `fabric-b10/invalid-attempt/`：第一次尝试的校准、原始目录哈希、生命周期和 `KeyError`。该次尝试没有写出 D112 coordinator 试次，所以不判断机器人通过或失败。
- `fabric-b10/invalid-client-exception/`：第二次尝试的校准和已完成场景；客户端以脱敏 `IllegalStateException` 退出，Python 等待观察超时。期限传递缺口已修，但最终运行复现后续异常，所以不把该次失败归因于期限。
- `fabric-b10/valid-run/`：修正期限传递后的正式批次、端到端量尺和最终失败结果。
- `published-production-semantic-hashes.json`：本阶段修改文件按 CRLF→LF 归一化后的语义哈希。

第一次尝试的原始目录为 `artifacts/fabric-deployment/20261016T033758422187Z-0bf7d2cd`；第二次为 `artifacts/fabric-deployment/20261016T054616381907Z-d14c19f7`。两次均保留为 `INVALID_HARNESS_RUN`。

最终正式目录为 `artifacts/fabric-deployment/20261016T055758879589Z-f137ddfd`。baseline 20／20、2-tick worker 延迟 8／8 完成。5-tick 第一场安全失败，违反必须完成门槛；控制准备 P99 22.4488 ms，也超过 15 ms。前后校准可比。10-tick 和 never-return Fabric 场景未运行。

D111 保持原结论，D110-SOLVE-01 保持打开。连续两跳、零等待交接和新动作仍未授权。
