# D122-1：Python 合同与组件采样

日期：2026-10-20。范围：Python contract、Runtime、Fabric 协商入口和 CalculatorBackend。Java 生产实现、D121 producer、完整 Windows／独立快照、Fabric 与新性能签署均未运行。

## 已检查的行为

- 新检查 23 项；连同 action、回执、Runtime、Fabric Python backend、transport、在线账本和设计量尺相邻集合，共 204／204 通过。
- 六种 mutation 全被断言发现，错误数为 0：丢窗口、早到替换当前、miss 清默认、普通更新后旧默认复活、默认预算从父到达起算、miss 后过早返回响应。
- v1 calculator 与 D122-0 的原 backend 比较，四族共 120 个请求的观察、物理状态、回执和实际输入逐项一致。v1 wire 的原键集合与编码字节另有直接检查。
- `red-before-projection.txt` 使用 D122-0 原仲裁器和现在的输入合同：本应保留的精确窗口仍为 `None`，真实断言失败，没有依赖“新类型不存在”。传输 late／never 的端到端 RED 继续引用未改写的 D122-0 记录。

## 当前字段

`SafeDefaultV1` 只允许 movement、绝对 activation tick、1—20 tick 和最多 30 秒墙钟额度。snapshot 增加 `first_effect_window` 与 `safe_default`；arbiter 只从获胜 movement 携带。普通 v1 编码去掉两个空字段，需要新字段但未协商时，在发送前抛出 `ActionCapabilityUnsupported`。

Fabric 默认继续 v1。显式请求 `mc2p.client_action.v2` 时，发送 `mc2p.deployment_session.v3`，包括 `action_schema_version` 和 observation schema。首个身份、观察和诊断均有效的 v4 idle sample 才确认协商；后续降级拒绝。CraftGround 没有被声明为支持 v2。

v4 receipt 保留 v3 所有字段，增加 `timing_events`。每项有 typed status、movement tick、episode 和 request sequence。`fallback_armed`／`window_pending` 不是应用；`window_applied` 仍需真实 input sample。默认采样使用 `input_application.state=default_active`，归父请求，且不会冒充父主输入的第二次 stream 应用。父主输入结束后，账本仍保留默认责任，直到默认耗尽或真实新输入接管。

CalculatorBackend 显式 `action_timing_v2=True` 后使用同一份有界转换表。未来请求持有响应到窗口；missed 请求继续默认到有界结束。`free_tick` 自主推进不增加观察 sequence，未造出 Python 中间观察。`response_timing.waited_ticks/waited_ns` 单列名义等待，不能当作同步计算性能结论。

## 复跑

```powershell
.venv/python.exe -m unittest tests.test_d122_python_timing tests.test_action_receipt tests.test_action_arbiter_v1 tests.test_client_behavior_contract tests.test_player_runtime_v1 tests.test_player_runtime_v1_smoke tests.test_fabric_behavior tests.test_deployment_python_transport tests.motion_nav.test_b10_online_motion tests.motion_nav.test_d122_timing_contract tests.motion_nav.test_navigation_design_metrics -q
.venv/python.exe scripts/d122_python_contract_probe.py --output .tmp/d122-1-mutations.json
```

配对运行还需要明确提供 D122-0 的 `tests/sim/backend.py`：加 `--v1-reference-source <该文件>`。报告记录实际参考 SHA-256，不从网络取代码，不把一份旧实现放进生产树。

这些结果只签署本切片的 Python 合同和组件支持。v2 Java 尚未实现，不能据此启用正式客户端 v2 或声称 D122 产品三态已通过。
