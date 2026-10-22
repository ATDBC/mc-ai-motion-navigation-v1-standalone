# D128 正式 smoke 环境无效运行

唯一正式命令：

```text
python scripts\d123_fabric_transport.py --launch smoke --output evidence\motion_navigation\d123-proof-and-transport-v1\d128\smoke-run-1
```

offline preflight 已通过，D128-ENV-01 的仓库 JDK 修复成立。probe 随后在独立 offline build 的 `:remapJar` 失败：固定 Gradle 缓存缺少当前 `build.gradle` 已锁定的 LWJGL 3.3.3 Windows native 包。`compileJava`、`processResources`、`classes`、`jar` 均为 up-to-date。

Minecraft、Fabric、pre-trial 和 movement trial 都没有启动。smoke 为 0／3，matrix 未运行。本次按 D129 分类为 `INVALID_ENVIRONMENT_RUN`，不消耗 Fabric attempt，也没有产品结论。允许只修缓存后在新的 append-only `smoke-run-2` 重试；原目录不得覆盖。

`build.log` 和 `result.json` 从 `artifacts/fabric-deployment-build/20261022T024718508750Z-0420fce8/` 原字节复制。`manifest.json` 绑定原有四个 launcher 文件与两个 build 文件；`stop.json` 绑定 manifest，并记录 D128-ENV-02／BUILD_CACHE 的环境失败边界。
