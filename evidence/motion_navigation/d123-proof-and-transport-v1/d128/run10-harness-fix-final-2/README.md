# D129 run-10 harness 修复最终离线证据

本目录只记录 run-10 harness 最终字节的离线门禁，没有启动 Minecraft／Fabric。

- RED：三个定向用例精确复现 windowed parent 被误注入、assembler 选择首个 windowed request、`bind_and_start` 缺失；结果为 2 failures、1 error。
- GREEN：最终组合运行中 40 个用例通过；两项新增测试修正自身 alias／消息断言后定向 2／2 通过。资源构造失败清理补强后的四方向组装与结构门禁 2／2 通过。
- 正式 offline preflight：PASS；组件 smoke 3／3、matrix 36／36、Java／overlay PASS、`fabric_started=false`。
- standalone：最终源码 export／verify 3495 files；仓库固定 OpenJDK 21 check PASS。
- 生产语义：没有修改 `mc2p`、Session、transport、Java 或协议；exact window 没有扩大。

文件与最终 harness 字节：

- `offline-preflight.json`：`c20c2b73c7cfaffd073b25b4ad01d18cb3c6ac712cedb15e750e127e1e44fa5a`
- `scripts/d122_fabric_timing.py`：`aaee8399a59c22db6dc664bb83b11242901e879ec3fb6c1543d37f9863334026`
- `scripts/d123_fabric_transport.py`：`b1b4b7d1115a0c32a49152846c9ad21cb70e2047a57ad500940217979ca85de4`

同级旧 `run10-harness-fix/` 与 `run10-harness-fix-final/` 均原样保留，只作为较早离线运行；最终签署只引用本目录。
