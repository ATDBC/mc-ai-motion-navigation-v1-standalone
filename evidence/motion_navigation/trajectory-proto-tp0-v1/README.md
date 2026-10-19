# TP-0：正式对照原因与停止策略

来源基线：31354d9c。Windows 项目环境运行。

当前状态：TP-0 已通过，正式对照冻结为 `da56bfa297cec6e9c0861bb976e608b462237cd2`，生产字节同 `c73c66ed96ce37760661265ae8228f5fca7fb135`。下面任务 1 的首轮状态按当时范围保留；最终签署以本文末尾的任务 2 为准。P0 原型尚未开始。

- red/causes.txt 保存首轮三项行为失败和一项夹具错误；目标修订必须通过 RuntimeNavigationDriver.replace_goal 重锚。
- red/goal-revision.txt 使用正确正式入口，稳定复现目标解析失败误报 GOAL_REVISED。
- red/proof-boundary.txt 是首版测试记录：当时入口尚未观察，不能证明依赖撤销。保留为测试覆盖错误的历史记录。
- green/affected.txt 是受影响三个模块 85／85。

生产修改仅交接拥有者。没有修改 ActionRouteExecutor。完整正逆、四方向实机和最终对照SHA由主 agent 整合后补入；当前不作 TP-0 总体签署。D093／D094 原证据保持原字节。

## 任务 1 复审补正

来源为 1c1c76d2，Windows .venv/python.exe。

- red/limit-next-frame.txt：后续帧新许可与旧恢复身份能改写既有终结，稳定 RED。未证明正式需要的补写分支已删除，不新增 permit 字段或 Session 状态。
- green/session-same-frame-limit.txt：真实 Session 的同帧世界依赖 STOP 与无进展额度到期，最终有界失败；最终身体 stop 为 MOTION_UNSOLVABLE，报告 cause 为 None，未提交 REPLAN。不能把拥有者原因测试泛化为该路径。
- red/formal-boundaries.txt 和 red/session-same-frame-limit.txt 保留本轮夹具诊断与最初错误预期，不作为生产缺陷证据。前者读取了不存在的诊断字段，后者误以为终态报告会保存通用 stop cause。
- red/proof-no-changes-mutation.txt：入口已激活、首条命令已实际应用，只删除 changed_cells 后 typed 状态断言稳定失败。原测试存在假覆盖；新测试证明依赖撤销已被消费，再次取消仍不能恢复 verified command。
- green/affected-round2.txt：D094、交接、B10 定向 88／88。green/session-validation-round2.txt：相邻 Session 与 D058 正式复核 166／166。

本轮不运行完整正逆和 Fabric，由主 agent 整合后签署。D093／D094 历史证据不回写。

## 任务 2：完整检查和四方向实机

- `checks-forward-direct-summary.json`：直接 unittest 正序会话 1713／1713、511.059 秒。不是标准检查脚本的结构化全记录；没有逐项结果、运行开始的提交身份或场景哈希。
- `checks-reverse.json`：标准脚本逆序 1713／1713，失败、错误和跳过为零，前后场景哈希一致；原始字节保留。
- `frozen-control-manifest.json`：正式对照、生产提交、84 个生产文件字节哈希和各证据入口。
- `fabric/source-byte-manifest.json`：原始记录的来源路径、压缩前后哈希和字节数。
- `fabric/raw/`：原始结构化试次、帧、客户端应用、追踪和清理记录，压缩后约 3 MB；不含世界、JAR 或游戏日志。
- `fabric/launcher/`：本次冻结 plan、runs、quality 原文件。
- `fabric/audit.py` 和 `fabric/audit-summary.json`：独立核对四方向真实输入应用、任务完成、伤害与显式期限、来源注销。

四方向任务 4／4 完成。四次普通 Walk 的真实回执均在请求后晚一 tick，`actual_offset=2`，原始状态为 `applied_outside_window`，各单列一条 `unwindowed_input_delay`；没有额外显式期限违规。这里用 `late_input`、`entry_gate` 和客户端实际应用记录证明注入；`injection_applied=false` 只表示没有外力或世界变化注入，不能用于否定本次晚到。

普通 Walk 未获得新的生效窗口，也没有因此承诺所有晚到均安全。D093 的旧四批失败保持原字节。本包签署 TP-0 的正式对照，不包含连续轨迹原型结果。

复核命令：

```powershell
.venv/python.exe evidence/motion_navigation/trajectory-proto-tp0-v1/fabric/audit.py
```
