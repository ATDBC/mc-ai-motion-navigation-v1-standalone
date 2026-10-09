# F2REC R3 清理证据

R3已按清理范围签署。Windows正序、逆序各1657／1657，零失败、错误、跳过和预期失败；共享场景哈希不变，生产文件指纹逐项相同。基线提交为 `5a6cf463`，实际删除后的生产实现由完整记录中的文件哈希绑定。工作树非净包括本轮待提交生产修改。

只删除没有正式生产者的 `SNEAK_EDGE_GUARD`。普通完成区域、停止尾迹、未声明边缘负例、Crouch、探边及原版潜行物理继续保留。Runtime关闭后的worker资源债明确保留，不因安全检查通过而改成已修复。

- `references-before.txt`：删除前真实引用；
- `deletion-inventory.json`：逐模块删除、保留及未来资源契约；
- `red-cleanup.json`：删除前新门禁真实RED；
- `component-comparison.json`：最终三个生产模块原字节哈希，104现行组件逐项不变、8退休组件单列；
- `mutation-check.json`：删除支撑阈值被行为检查发现；
- `worker-lifecycle.json`：四项动态安全检查，资源债没有改为通过；
- `production-semantic-hashes.json`：本轮三个生产模块的LF归一哈希；
- `full-forward.json`／`full-reverse.json`：完整Windows签署检查；
- `signature-summary.json`：范围、真实数量和签署结论；
- `focused-checks.json`／`final-targeted.log`：定向结果及仅测试名整改后的35项检查；
- `tool-command-errors.json`：错误命令及纠正记录，不算生产回归；
- `design-metrics.json`：动作分支42、未分类0，Session未修改。

15项退休能力专用测试删除、4项当前检查新增，因此完整计数由1668变为1657。旧F2输入清单和历史证据原字节不变，不能把8个退休组件原通过结果称为当前通过。

复跑：`.venv/python.exe -m scripts.f2rec_r3_cleanup_probe --output <独立目录>`。该脚本保留原F2清单，不覆盖历史证据。完整检查使用 `.venv/python.exe -m scripts.run_motion_navigation_checks --output <文件>`，逆序另加 `--reverse`。

R4、Fabric和公开最终恢复尚未实施。
