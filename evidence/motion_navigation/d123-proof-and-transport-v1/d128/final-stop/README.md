# D123-3 最终停止

D123-3 的最终状态为 **PARTIAL／STOPPED**。有效 smoke 为 0／3，matrix 未运行，`smoke-run-12` 未获授权。

`smoke-run-10` 和 `smoke-run-11` 都保留为 `INVALID_HARNESS_RUN/fixture-scheduling`。两次运行均没有应用被测输入或 default，身体没有进入被测行为，health 没有下降。因此它们没有产品通过或失败结论。run10 与 run11 的原始封存分别见提交 `60f58c2c`、`3f5c3804`；本目录不改写任何历史证据。run11 仍只使用独立只读因果归因，不声称 run10 专用 D129 validator 已覆盖它。

只读设计复核测得：

- `_replay_verified_commands` 约 49.8—52.2 ms；
- `bind_fixed_successor` 约 16.4—17.0 ms；
- 当前 action-boundary revalidation 会完整重放，现有串行路径的保守下限超过一个 50 ms movement tick。

现有正式 API 没有已授权的轻量 retime。harness 手工平移 proof 会改变证明时间身份，也未获授权。继续需要新决定授权正式 proof retime／relative-time certificate，或改变 trial 启动结构，并重新冻结 exact-window 注册点、身份绑定和回归门禁。在此之前禁止 run12 和 matrix。
