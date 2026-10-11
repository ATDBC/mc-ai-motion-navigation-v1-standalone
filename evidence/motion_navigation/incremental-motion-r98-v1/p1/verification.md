# D098 P1 检查记录

本批先写 13 项失败检查。首次运行因四个 P1 模块与量尺入口不存在而 13／13 报错。独立复审后又先补失败检查，再修复完整请求绑定、唯一预算 owner、精确伤害数值、真实 payload 重算攻击和参考签名 provenance。最终直接检查为 24／24。

```text
.venv/python.exe -m unittest tests.motion_nav.test_incremental_motion_table tests.motion_nav.test_incremental_motion_controller tests.motion_nav.test_incremental_motion_proof tests.motion_nav.test_incremental_motion_performance -q
Ran 24 tests ... OK

.venv/python.exe -m unittest tests.motion_nav.test_trajectory_proto_m0_probe tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment tests.motion_nav.test_trajectory_proto_reference_search tests.motion_nav.test_trajectory_proto_primitive_matrix tests.motion_nav.test_standalone_export -q
Ran 113 tests ... OK

.venv/python.exe scripts/generate_d098_reference_signatures.py --check
通过；签名文件记录基线提交、输入哈希、scanner 源哈希和生成命令。

git diff --exit-code d01d378f -- experiments/motion_navigation/trajectory_proto/commitment.py
通过；独立 scanner 在 P1 差异中没有修改。

git check-attr text -- experiments/motion_navigation/trajectory_proto/decision.py experiments/motion_navigation/trajectory_proto/reference_signature_generator.py scripts/generate_d098_reference_signatures.py
三个文件均为 `text: unset`，即 `.gitattributes -text` 已生效。现有 manifest 哈希与工作树原字节逐项一致。
```

Windows 代表量尺命令：

```text
.venv/python.exe scripts/measure_incremental_motion_r98.py --limit 6 --rounds 20 --output evidence/motion_navigation/incremental-motion-r98-v1/p1/windows-decision-baseline.json
```

量尺覆盖 A／C／D 共 16 个代表样本，重复 20 轮。A／C 明确使用冻结输入，查表和控制器为 no-op；D 才运行真实查表和控制器。三组都执行 trace、证明和许可核对，物理步数覆盖所有阶段。320 次完整决定的 P50／P95／P99／最大值为 53.8193／66.4301／66.6814／66.7190 ms。本数字只冻结 P2 前的未优化成本。

`mc2p` 相对基线没有变化；没有运行 Fabric。实验源码与数据没有导入 `reviews/`、`.tmp/` 或测试模块。完整字节哈希见 `manifest.json`。
