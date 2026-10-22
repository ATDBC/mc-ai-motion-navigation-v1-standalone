# D128 仓库固定 JDK 离线证据

`offline-preflight.json` 来自在 `JAVA_HOME` 为空、`PATH` 只含 Windows 系统目录的进程中运行：

```text
D:\My_project\mc_ai\.venv\python.exe scripts\d123_fabric_transport.py --offline-preflight .tmp\d128-repository-jdk-offline-preflight-final.json
```

结果为 PASS，`fabric_started=false`。组件 smoke 3／3、组件矩阵 36／36、Java／overlay PASS。记录冻结仓库外层 `java`／`javac` 和 canonical `java.home/bin/java.exe` 的路径、vendor、version 与 SHA-256。

本目录没有 Minecraft／Fabric 结果。D123-3 的旧 `smoke-run-1` 保持不变；新的正式 smoke 和后续矩阵命令见 D123 计划第 9 节。
