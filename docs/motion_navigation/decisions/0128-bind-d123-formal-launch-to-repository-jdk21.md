# D128：正式入口绑定仓库固定 OpenJDK 21

日期：2026-10-22。状态：**IMPLEMENTED；JDK 正式入口已通过，后续失败按 D129 分类**。

依据：[D123 决定](0123-resolve-proof-dependencies-and-transport-loss.md)、[D123 计划](../stages/d123-proof-dependency-and-transport-loss-plan.md)、[D123 验收](../acceptance/d123-proof-dependency-and-transport-loss.md)和 `evidence/motion_navigation/d123-proof-and-transport-v1/d123-3/smoke-run-1/stop.json`。

## 问题

D123-3 第一次正式 smoke 在启动 Minecraft 前停止。仓库固定的 `.venv/Library/bin/java.exe` 和 `javac.exe` 存在，但正式终端的 `JAVA_HOME` 为空，`PATH` 没有 Java、JDK 或仓库 `.venv`。D123 offline gate 调用通用 `discover_java_tools()`，只检查调用终端环境，因此没有找到仓库已经固定的 OpenJDK 21。

D123-ENV-01 和 `smoke-run-1` 保持原字节。它们继续证明第一次正式命令因环境门禁停止，不能改写成 harness 或 Fabric 通过。

## 决定

D128 只修复 D123 正式 Windows 入口发现 Java 工具的方式：

- 入口显式解析仓库 `.venv/Library` 下固定的 `java.exe` 和 `javac.exe`；
- 必须用正式 typed Java 工具检查确认主版本为 21；缺文件、版本错误或身份不一致都在启动 Minecraft 前失败；
- 同一组已验证路径和字节身份同时交给 offline Java gate 与 Fabric 子进程；
- 不依赖调用终端偶然设置的 `JAVA_HOME` 或 `PATH`，也不回退接受系统中的其他 Java；
- 不修改运动、proof、transport、Session、Runtime 或 Java 正式语义。

## 授权和运行次数

实现和离线门禁通过后，授权一次新的 D123 south smoke。冻结命令仍走 D123 launcher，但必须写入新的 append-only 输出目录。只有 smoke 3／3 后，才授权一次四方向 × 三状态 × 三重复的 36 场矩阵。

新的 smoke 在 offline 或 pre-trial 失败时不消耗 Fabric attempt，但 D128 仍立即停止，不得现场修正后重跑。进入首个 movement trial 后，任何失败都保留原始证据并停止；矩阵不得运行。smoke 非 3／3、生产或 harness 字节变化、JDK 身份变化，也都禁止矩阵。

## 停止条件

若修复需要修改 `mc2p` 生产语义、Session 状态、transport 协议、Java overlay 正式行为、运动门槛或安全解释，立即停止并另立决定。D128 不运行 Fabric；正式 smoke 由离线实现和独立复核通过后另行执行。

## 实施结果

D123 正式入口现在只解析仓库 `.venv/Library`。它确认外层 `java.exe`／`javac.exe` 同源且为 21，再核对两者报告的 canonical `java.home`、vendor 和 version 完全一致；外层两个工具和 canonical `java.home/bin/java.exe` 的 SHA-256 一并冻结。路径任一祖先为 symlink／reparse point、文件缺失、版本错误、canonical home 不同或 hash 变化都会拒绝。

正式 `main` 只选择一次权威 binding。同一对象用于 environment preflight、Java 单测和 Gradle overlay；同一组路径、properties、hash 和 binding 摘要写入 Fabric probe 环境。D123 parent probe 和 worker 都在任何 server/client `Popen` 前复核环境，服务器和客户端只使用已验证的 canonical executable。调用终端的假 Java 不参与选择；已有 `MC2P_D128_*` 外部变量先删除再重建。

根因 RED 为 3 error：旧入口没有仓库 JDK resolver。实现后 D128 定向 8／8；D123 原专项中 12 项通过，修正调用签名断言后目标项 1／1；probe／standalone 相邻 83／83。清空 `JAVA_HOME` 且把 `PATH` 限制为 Windows 系统目录后，完整 offline preflight 通过：组件 smoke 3／3、组件矩阵 36／36、Java／overlay PASS、standalone 3234 个文件，`fabric_started=false`。没有运行 Minecraft／Fabric，也没有修改 `mc2p`、Session、transport 或 Java overlay 正式语义。

## 独立审查 round 1

首份 `offline-preflight.json` 生成后，probe 的最终字节又发生变化，因此它对当前 `validate_offline_preflight(..., require_java=True)` 返回 false。该文件保留为历史运行，不覆盖、不删除，也不再作为当前 smoke 的 offline 依据。最终代码上追加生成 `offline-preflight-run-2.json`；其 probe hash 与当前字节相同，当前 validator 返回 true。

新增行为测试真实进入 `run_worker`，分别在 server 和 client 两次 `Popen` 边界检查 `JAVA_HOME`、`PATH` 与全部 `MC2P_D128_*`。另一个测试检查 Java unittest 与 Gradle subprocess 收到同一 binding。删除四处任一传播都会得到 mutation RED。最小修正让 `_d128_child_environment()` 返回完整 binding，不依赖 `client_environment()` 偶然保留 Java 环境。最终 D128／D123 专项 23／23，probe／standalone 相邻 75／75；追加 offline 仍为组件 3／3、矩阵 36／36、Java／overlay PASS、standalone 3234 个文件，且 `fabric_started=false`。

## 唯一正式 smoke 结果

唯一正式命令为：

```text
python scripts\d123_fabric_transport.py --launch smoke --output evidence\motion_navigation\d123-proof-and-transport-v1\d128\smoke-run-1
```

同进程 offline preflight 全部通过。因此 D128-ENV-01 的 JDK 发现与传递修复已经通过正式入口。probe 随后以 1 退出，`source_run=null`。独立 build 的 `compileJava`、`processResources`、`classes` 和 `jar` 都是 up-to-date；`:remapJar` 在 offline 模式解析 `:minecraftRuntimeLibraries` 时失败。固定缓存缺少 LWJGL 3.3.3 的 freetype、glfw、jemalloc、openal、opengl、stb、tinyfd 和 lwjgl 本体，每类均缺 Windows、Windows ARM64 和 Windows x86 native，共 24 个文件。该问题登记为 D128-ENV-02／BUILD_CACHE。

失败发生在 Minecraft、Fabric、pre-trial 和首个 movement trial 之前。smoke 为 0／3，矩阵未运行，Fabric 产品行为没有结论。原始 build 证据从 `artifacts/fabric-deployment-build/20261022T024718508750Z-0420fce8/` 逐字节复制到 `evidence/motion_navigation/d123-proof-and-transport-v1/d128/smoke-run-1/`，由 manifest 和 stop 记录绑定。

## D129 后续分类

[D129](0129-classify-pretrial-environment-failures.md) 取代了“任何 pre-trial 失败永久停止”的旧规则。D128 `smoke-run-1` 现归类为 `INVALID_PRETRIAL_RUN/environment/build_cache_native`，不消耗 attempt。固定缓存按当前 `build.gradle` 的精确版本修复，正式 offline build 与 launch export 均通过；D128 的 JDK 发现与环境传递结论保持通过。run-2 为 `tool/standalone_export_manifest`，run-3 为 `environment/runtime_classpath_jline`，run-4—run-7 为 `fixture/assembly`，run-8／9 为 `fixture-adapter`；这些运行同属 `INVALID_PRETRIAL_RUN`，原始证据保持不变，也均未进入首个 movement trial。新的正式运行目前暂停，等待 harness 修复的独立复核。
