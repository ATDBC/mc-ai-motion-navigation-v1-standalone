# F2-R 紧凑证据

正式平台为 Windows、Conda Python 3.11.16。基线为 `409adec8`。原 F2 528 项、93 项 Fabric 和 v7 保持历史记录。

`red/` 保存生产代码尚未修复时的 typed 几何、正式链、固定杂乱扫描和失败测试输出。输入冻结在 `tests/sim/manifests/navigation-product-r28-v8.json`，其 `product_reference` 完整保留原 v7；104 个固定几何任务与 1800 个杂乱输入运行前已冻结。最终清单只保存生成器版本、参数、计数和整体／分层 input digest，共 11.1 KB。`frozen-input-index.jsonl` 从原展开输入记录 1904 个 ID／input hash，测试逐项证明参数清单生成同一批输入。`input-freeze.json` 保留原展开文件本地路径及 SHA256。旧 RED 查询输出通过 ID 关联这份冻结索引；新查询输出直接保存 input hash。杂乱扫描和杂乱正式链使用同一批固定输入。

`green/clutter/` 是修复后的同输入几何扫描。查询可行不等于路线可达，正式 Runtime 链结果与 Fabric 分开报告。大原始流只在本地 artifacts／`.tmp` 保存，并在最终索引登记路径与 SHA256。

复现命令使用仓库的 `.venv/python.exe`：

```powershell
.venv/python.exe -m unittest tests.motion_nav.test_f2r_piecewise_completion
.venv/python.exe scripts/f2r_piecewise_evidence.py geometry --output .tmp/f2r-replay-geometry
.venv/python.exe scripts/f2r_piecewise_evidence.py clutter --output .tmp/f2r-replay-clutter
.venv/python.exe scripts/f2r_piecewise_evidence.py formal --output .tmp/f2r-replay-formal
.venv/python.exe scripts/f2r_piecewise_evidence.py clutter-formal --output .tmp/f2r-replay-clutter-formal
.venv/python.exe scripts/f2_ground_route_runtime.py --f2r --ids f2-f2r_outer_corner-0-normal
.venv/python.exe scripts/f2_ground_route_runtime.py --f2r
```

所有采集入口拒绝覆盖已有输出。RED 来源、修复采集来源和最终交付提交分别记录；dirty 采集保持原标记。潜行仍只有组件证明，正式路线无生产者，本轮不把平台边缘当潜行实机证据。

当前实测见 `final/`：Windows 正逆各 1647／1647，五组与严格空中比较保持；固定几何正式链 104／104，杂乱正式链 1715／1800，总 v8 为 3817／3904，零安全事件。每层保存 input hash、原目标 bounds、精确块／参考点、完成观察、最终身体及失败原因。控制 P95 是各层 `FixedRouteController` 全部逐帧样本合并计算，原始样本数组留本地并保存 count／SHA256。

`final/fabric/` 保存两族四方向正常／晚1的 16 项正式实机及两个先行 smoke。16／16 原目标完成，8 次迟到实际在窗口内；独立 trace 审计确认每个来源取消并注销，零掉落、伤害、危险接触、终态潜行及正例期限错失。完整原始流只在本地，按 raw manifest 定位及校验。

`diagnostics/d061-shared-simulator/` 保留两次正式超限、同机 A-B-B-A 和临时 cProfile／分段归因。共享模拟器只做全字段等价的视觉射线短路，RED、随机／边界／错误副本和 44 项定向检查保存。修复后单次正式 D061 全门槛通过，完整路径 captured／retained 最大均 40.1764 ms；它不计作机器人能力提升。原 F2 的 57.0863／46.5423 ms 和两次失败不覆盖。五组共 3493 项也证明修复夹具前后行为签名完全相同。

原始 v7 与新量尺保留不同时间范围：v7 是旧固定任务的整项时序，新层是逐帧控制分层。17 项起点已经满足目标、30 项立即几何拒绝，没有完整异步工作，不能把这 47 项用于扩大异步覆盖结论。近战／跟随尺寸只在固定模拟链覆盖，本轮两族新增实机使用产品目标尺寸。

`final/public/` 保存干净 `cc28816` 的独立公开检查：verify 1539 个文件、专项 63 项与完整 motion_nav 1647 项通过。最后签署只改文档与证据，另导出快照 verify，并与已测试的 638 个代码、配置和夹具文件逐字节比较。完整运行保留真实来源，不改写为最后文档提交上的重跑。
