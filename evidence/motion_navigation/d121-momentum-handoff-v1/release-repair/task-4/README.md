# Task 4：更正 transport 诊断和入口状态

本任务只改脚本、测试和文档。D121 仍为 PARTIAL／STOPPED，D122 未开始。

- 先保存 RED：never 场的兼容字段原来是 true，新断言明确失败。修复后 Task 5 诊断 12/12 通过。
- v2 诊断把三种事实分开：及时应用、过期应用、未送达。未送达和及时应用没有进行过期拦截，兼容布尔字段均为 null（不适用）；晚到实际应用为 false。
- matrix-v2.json 是三场新运行，聚合仍为 FAILED_STOPPED。与旧 review-repair/matrix-final.json 比较，三场的输入、实际 tick、终态位置／速度、伤害、来源释放和失败原因均不变。
- 旧 Task 5 目录没有修改；history-integrity-and-comparison.json 保存全部旧文件字节哈希与逐字段比较。
- 包装入口统一为 PARTIAL／STOPPED。历史 S2 的命令只指向固定 3889d10e 检出；当前公开快照不再包含该脚本。源树保留的历史脚本未重新启用。
- 相邻 handoff、设计量尺和导出检查共 87 项，85 项通过。2 项失败都是 Task 3 公开 registry 修复后的生产指纹尚未登记；失败日志完整保留。这两项由 Task 5 最终签署集中追加当前指纹，不能更新旧历史记录。
- 设计量尺为动作分支 43、未分类 0。本任务没有修改生产文件。
- 显式使用 3889d10e 字节基线导出新独立快照，verify 2829 文件通过；2387 个等价文件复用旧字节。此处只验证导出完整性，不替代 Task 5 全量测试。

所有现有修改文件原本均为 LF，本任务保持 LF。早期文档自检使用 Windows 默认 GBK 读 UTF-8 后报工具错误，随后改为显式 UTF-8；另核实到“删除脚本”只发生在公开快照，源树仍保留历史脚本，因此文档按实际范围描述。

完整 Windows 与独立快照正逆序门禁留给 Task 5；本任务不声称发布已全绿，也未运行 Fabric 或推送。

复跑诊断使用新的输出文件，不能覆盖本目录已有证据：

```powershell
& "<Python 3.11 解释器绝对路径>" -m unittest tests.motion_nav.test_d121_transport_late -v
& "<Python 3.11 解释器绝对路径>" scripts/d121_transport_late_probe.py --output <新的输出文件>
& "<Python 3.11 解释器绝对路径>" -m unittest tests.motion_nav.test_d121_handoff_execution tests.motion_nav.test_navigation_design_metrics tests.motion_nav.test_standalone_export -q
```
