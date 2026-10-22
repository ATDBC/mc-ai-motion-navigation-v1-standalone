# D123-2 transport 生命周期证据

本目录由 `scripts/d127_transport_lifecycle_evidence.py` 生成。它只运行正式组件 preflight，不启动 Minecraft 或 Fabric。

`summary.json` 记录四类 transport 合同、最小 `not_applied` wire、driver 级 `UNRESOLVED`、独立 resource close 和正式 preflight 3／3。

`final_gates` 记录已经正式确认的源码树、standalone、Java／wire 和 export／verify 结果，同时保留修复前 standalone 的真实失败。这里没有伪造或补写运行报告哈希。
