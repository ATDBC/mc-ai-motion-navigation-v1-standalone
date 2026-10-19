# TP 原型材料公开整理记录

日期：2026-10-10。平台：项目 Windows `.venv/python.exe`。

公开快照现在包含隔离的 TP 原型源码、三个测试模块，以及方案、组件报告、时序修正和独立审查。第三方可以从快照根目录运行组件检查。正式 actor、Runtime 和脚本入口仍不导入原型；本次没有修改 `mc2p/` 或原型行为。

Task 3／4 的组件证据保留。Task 5A 的 `343d10de` 仍是候选，独立审查 `f2fbd475` 中未关闭的 P1／P2 和准时支去重的覆盖边界没有修复。Task 5A 未签署，P0 未通过，不进入 5B。公开材料不改变旧验收结论，也不授权继续实施。

## 整理范围

- 固定清单加入 `experiments/motion_navigation/trajectory_proto/*.py`、三个 TP 测试和本目录的 Markdown；删除原型的两项排除规则，保留可为空的 `exclude_globs` 机制。
- 导出合同改为检查源码、测试、关键计划与审查存在；原有静态检查扩展到 `mc2p/` 与 `scripts/`，确认正式入口没有导入实验代码。
- 公开 README／AGENTS 顶部说明候选、已知问题和停止状态，并提供三个组件测试的运行命令。
- D095 和 TP 阶段澄清：允许快照包含实验材料，正式入口仍不得导入原型。
- 先前只保存在本地的原始组件报告一并纳入源码提交，保留原文中的当时状态，不回写历史数字或结论。

## 检查

先修改两个导出合同检查，再在旧清单上运行，得到预期 RED：原型源码缺失，原型测试及报告缺失。两项均为断言失败，`Ran 2 tests in 32.383s / FAILED (failures=2)`。随后更新清单和说明，得到 GREEN：

```text
.venv/python.exe -m unittest tests.motion_nav.test_trajectory_proto_contracts tests.motion_nav.test_trajectory_proto_commitment tests.motion_nav.test_trajectory_proto_reference_search -q
Ran 71 tests in 38.071s / OK

.venv/python.exe -m unittest tests.motion_nav.test_standalone_export -q
Ran 12 tests in 25.545s / OK
```

实际快照固定生成到 `.tmp/tp-public-export-20261010`：

```text
.venv/python.exe scripts/export_motion_navigation_standalone.py export --root .tmp/tp-public-export-20261010 --clean
.venv/python.exe scripts/export_motion_navigation_standalone.py verify --root .tmp/tp-public-export-20261010
```

初次导出校验为 `STANDALONE_EXPORT_OK files=1964`；加入本记录后再次生成并校验，文件数为 1965。清单覆盖 5 个原型源码、3 个测试模块和本目录 13 份 Markdown；不包含 Python 缓存。

从实际公开快照根目录，以项目的 Windows Python 运行同一组三个 TP 模块，71／71 通过。这样验证了克隆所得目录结构和依赖，组件结果仍不能代替完整 P0、正式性能或 Fabric 原型验收。

## 链接边界

检查导出树中的 Markdown 相对文件链接，只检查目标文件或目录存在，不验证远程 URL 或页内锚点。根目录 README／AGENTS、TP 阶段、D095 和本目录材料没有断链；原先 TP 阶段指向独立审查的链接已在快照内可达。

全树共检查 494 个链接，其中 63 个目标未公开：51 个来自 `packaging/` 下 README／AGENTS 模板副本，它们的链接按根目录安装位置编写；其余 12 个位于历史 B03、B04、B01、R26 参考方案和回放工具文档，指向未导出的原始 artifacts、旧脚本或旧工作日志。根目录安装后的模板链接正确。本次保留这些历史引用，不将此结果写成全仓库零断链。

本记录只签署实验材料可公开复现与生产入口隔离。正式完整运动导航、18 族搜索、runner、性能量尺和实机原型未重跑。
