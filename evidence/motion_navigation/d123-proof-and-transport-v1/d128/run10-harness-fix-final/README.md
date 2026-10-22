# D129 run-10 harness 修复离线证据

本目录只记录 run-10 的 harness 修复门禁，没有启动 Minecraft／Fabric。

- 基线提交：`60f58c2c954ce3bf17ddc2ed4dd0009469e40b3c`。
- RED：三个定向用例分别复现 windowed parent 被当作 successor 注入、assembler 选择首个 windowed request、`bind_and_start` 缺失；结果为 2 failures、1 error。
- GREEN：D126／D122／D123 组合中的 40 个未受测试断言修正影响的用例通过；两项 mutation 测试修正自身 alias／错误消息断言后定向 2／2 通过。合计 42 项当前行为检查通过。
- 正式 offline preflight：PASS；组件 smoke 3／3、matrix 36／36、Java／overlay PASS、`fabric_started=false`。
- standalone：最终源码 export／verify 3493 files；仓库固定 OpenJDK 21 check PASS。
- 生产语义：没有修改 `mc2p`、Session、transport、Java 或协议；exact window 没有扩大。

`offline-preflight.json` SHA-256：`388167d6a997f70a41b8cdb9caac1fc02df90524f2dca6eb408cdebd34c6ecc5`。

最终 harness 字节：

- `scripts/d122_fabric_timing.py`：`fe4b09fc615dc3e553380395a96ebfdc07913129018a59ad55e6948248486168`
- `scripts/d123_fabric_transport.py`：`b1b4b7d1115a0c32a49152846c9ad21cb70e2047a57ad500940217979ca85de4`

旧的 `run10-harness-fix/offline-preflight.json` 保留。它早于最后的 close／injection identity 门禁，只作为历史离线运行，不作为最终签署文件。
