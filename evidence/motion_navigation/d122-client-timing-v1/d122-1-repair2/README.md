# D122-1 第二次限定修正

基于 `6696e57f`，保留上一切片和证据。

默认未完成时，wall／父期限到期不能先交还普通响应。正常 missed／未激活的 future 等到真实默认采样完成才返回；外层硬终止走 reset／许可撤销。这样下一条普通请求无法利用提前返回的响应截短默认。

账本按 v4 事件和真实采样的 tick 顺序维护 current、pending、default 三个成员。accepted cancel 只结束明确成员和自身，不清无关的旧 ambiguous 历史。更高 pending 已取消旧 pending，旧记录当拍以内部 `CANCELLED` 结束。默认耗尽同拍的父身份 neutral 不计作父主输入，也不重新登记默认。

`red.txt` 记录第一组 13 项检查中的 2 项断言失败；`pending-red.txt` 记录追加两个转换缺陷的断言失败。`adjacent-checks.txt` 是追加修复前已通过的 217 项；`final-adjacent-checks.txt` 是最终代码结果。mutation 共 19 种，均以断言失败检出，无错误。v1 四族共 120 个请求逐项一致。

```powershell
.venv/python.exe -m unittest tests.test_d122_python_timing tests.test_d122_python_timing_repair tests.test_action_receipt tests.test_action_arbiter_v1 tests.test_client_behavior_contract tests.test_player_runtime_v1 tests.test_player_runtime_v1_smoke tests.test_fabric_behavior tests.test_deployment_python_transport tests.motion_nav.test_b10_online_motion tests.motion_nav.test_d122_timing_contract tests.motion_nav.test_navigation_design_metrics -v
.venv/python.exe scripts/d122_python_contract_probe.py --output .tmp/d122-1-repair2-mutations.json
```

v1 配对的 reference 参数沿用上一修正 README。未运行 Java、Fabric、完整 Windows、公开快照完整检查或性能验收。正式 v2 host/probe 构造点和当前导出指纹迁移留 D122-3；本切片不启用正式严格动作。
