# D123-0 独立审查修正 round 1

这里只修正两项 P2：正式依赖门禁漏查 mismatch，以及验收第 2 节仍写“未运行”。没有扩大物理实验，没有生产改动或 Fabric。

`formal-dependency-gates.json` 对原有 396 行直接复用 `scripts.d122_fabric_timing.inspect_fabric_dependency_knowledge()`，保存 known／unknown／mismatch 数量和坐标。typed 结果为 52 PASS、160 FACT_MISMATCH、100 NEEDS_WORLD、84 NEEDS_WORLD_AND_FACT_MISMATCH。unknown 或 mismatch 任一非零即拒绝。

原 `run-1/result.offline_dependency_gate` 只查 UNKNOWN，属于 **knowledge-only 历史**，不能称正式依赖门禁或正式接纳。追加文件将它明确标为 `historical_knowledge_only_gate`。原 run-1、根 README、旧最终索引和旧自检保留原字节；其中旧字段或“接纳”描述应按本修正说明理解。修正后的正式 checker 结果保存在 `formal_dependency_gate`。完整石块对照即使物理轨迹相同，也因与冻结 AIR 不符而拒绝。

四份 D120 原始 coverage 再次按历史哈希核验；972 入口／2916 后缀逐项重算相等，旧成功退步 0。对比没有换用小样本。

D123-0 的 STOPPED 仅表示删除证明未签署。现有证据支持 D123-1 保留全部 12 格并做 actor-only 观察校准，不表示观察路线不可行。本批没有运行该校准。

命令：

```powershell
& 'D:/My_project/mc_ai/.venv/python.exe' scripts/d123_dependency_gate_repair.py --raw evidence/motion_navigation/d123-proof-and-transport-v1/d123-0/run-1 --output evidence/motion_navigation/d123-proof-and-transport-v1/d123-0/repair-1 --d120-source D:/My_project/mc_ai/.tmp/d120-entry-s3-prerequisite-v1
& 'D:/My_project/mc_ai/.venv/python.exe' -m unittest tests.motion_nav.test_d123_dependency_gate_repair tests.motion_nav.test_d123_dependency_probe tests.motion_nav.test_d120_momentum_entry_calibration -v
```

输出目录必须尚不存在。日志、自检和新 manifest 均追加在本目录；不会刷新历史索引来掩盖差异。
