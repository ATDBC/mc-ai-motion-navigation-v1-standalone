# D123-3 Fabric smoke 停止证据

D123-3 状态为 **PARTIAL／STOPPED**。2026-10-22，第一次正式 south smoke 在同进程 offline preflight 的 Java 工具发现阶段停止。正式终端的 `JAVA_HOME` 为空，`PATH` 没有 Java、JDK 或仓库 `.venv`，因此 `discover_java_tools()` 抛出 `ExportViolation`。

失败发生在 Minecraft／Fabric 启动和首个 movement trial 之前。Fabric attempt 未消耗，smoke 为 0／3，36 场矩阵未运行。预登记规则要求 D123-3 仍立即停止，不能在本阶段补环境后重跑。

`smoke-run-1/` 保存正式命令、root 终端手工封存的 stderr 与退出码、环境摘要和停止结论。手工封存文件不是进程重定向产生的原始字节；其来源和边界均在文件内明确记录。

本目录不包含 Fabric 运行结果，也不支持任何真实运动、安全、健康或完成率结论。
