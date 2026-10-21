# D122-1 审查修正

基于 `423b5bd1`，只修 Python 合同、组件采样和输入账本。原 `d122-1/` 证据不回写。

修正默认的实际采样时长、耗尽事实的完整性，以及取消／替换的账本结束。accepted cancel 使用内部 `CANCELLED`；六类 wire timing status 不变。取消清空当前、pending 和默认，不能算作身体安全收尾。

Windows 结果：相邻 214／214，最终修正模块 11／11（其中 10 项已在相邻集合），共 215 个不同检查通过。14 项 mutation 全部因断言失败被检出，没有导入或 fixture 错误。v1 四族共 120 个请求的状态、观察、回执和实际输入逐项一致。

`red.txt` 保存初始 7 项中 5 项失败；`cancel-red.txt` 保存取消分类和整份安排取消的 2 项失败。`adjacent-checks.txt`、`final-repair-checks.txt` 和 `mutations-and-v1-pair.json` 为最终结果。`manifest.json` 保存代码和证据哈希。

```powershell
.venv/python.exe -m unittest tests.test_d122_python_timing_repair -v
.venv/python.exe -m unittest tests.test_d122_python_timing tests.test_d122_python_timing_repair tests.test_action_receipt tests.test_action_arbiter_v1 tests.test_client_behavior_contract tests.test_player_runtime_v1 tests.test_player_runtime_v1_smoke tests.test_fabric_behavior tests.test_deployment_python_transport tests.motion_nav.test_b10_online_motion tests.motion_nav.test_d122_timing_contract tests.motion_nav.test_navigation_design_metrics -v
.venv/python.exe scripts/d122_python_contract_probe.py --output .tmp/d122-1-repair-mutations.json
```

v1 配对还需 `--v1-reference-source` 指向 `fa2d3127` 的 `tests/sim/backend.py` 原字节，SHA-256 为 `3a93a5dac30d8d4fb4e68b436297813593a82741dbb00444dc0fbfe805737733`。原始旧源码不另复制进证据。

未修改 Java、Session、规划器、物理或证明阈值，未接 D121 producer。正式 Fabric host／probe v2 迁移留 D122-3；本切片不能启用正式严格动作。未运行 Java、Fabric、完整 Windows、公开快照完整检查或性能验收。

额外导出单元模块 26／28：两项仍要求 D122-1 以前的 Runtime／在线账本当前指纹，详见 `export-checks.json`。历史证据不回写；当前指纹迁移留 D122-3，不能由本中间切片声称公开发布全绿。
