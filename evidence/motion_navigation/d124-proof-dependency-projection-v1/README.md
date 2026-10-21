# D124 证明依赖分层证据

本目录只发布可以复核结论的紧凑证据。逐帧 D122 结果和 D120 每入口明细保留在本地生成目录，通过记录的 SHA-256 与重跑命令核对，不进入公开快照。

- `targeted/run-1`：实现过程中的首次只读探针，保留原始结果。
- `targeted/run-2`：首次实现字节上的规则、依赖、来源边界与源码哈希。
- `targeted/run-3`：独立审查修复后的生产字节；当时源码清单尚未纳入 `safe_ground_control.py`，作为过程记录保留。
- `targeted/run-4`：首次完整源码清单、双 JAR 来源和自动字节码审计。
- `targeted/run-5`：跨 section 缓存修复后的最终生产字节，并补充全部 operation 类型的 action 体积实测。
- `targeted/run-6`：在相同生产字节上补齐原版活塞动态边界审计；合成 unsupported 只验证查询与缓存语义，不冒充原版方块状态。
- `targeted/run-7`：首次动态边界审计因把 Java `float` 常量写成 `double` token 而停止；失败记录保留。
- `targeted/run-8`：修正 token 后的最终规则审计、依赖、生产哈希和 action 体积。
- `production-semantic-hashes.json`：D124 当前生产文件的 LF 规范哈希，并以摘要接续 D122 的当前语义记录；旧阶段证据不改写。
- `harness-semantic-hashes.json`：D124 当前 D122 Fabric 夹具脚本的 LF 规范哈希，并以摘要接续 D122 历史夹具记录。
- `incremental-motion-proof-current.json`：保留 D098 冻结参考的原始摘要，同时记录 D124 当前参考生成结果；两种口径不互相改写。
- `full-source-diagnostics/`：完整源码分片暴露的旧合同失败及限定修复复跑结果。
- `d120/compact.json`：972 个入口、2916 个后缀的逐方向签名和零退步结论。
- `d122/compact.json`：20 场正式矩阵和生产控制性能摘要。
- `d122/v1-compat.json`：120 个 v1 兼容样本与变异检查。
- `d122/smoke-history.json`：standalone 无本地 `artifacts/` 时使用的 v1—v8 紧凑历史；源码树仍直接核对原始结果。
- `performance/message-sizes.json`：正式 worker 消息 protocol-5 与 wire 体积。

没有运行 Minecraft 或 Fabric。Windows 源码树正逆各 209 模块／2748 项，standalone 正逆各 208 模块／2724 项；Java 源码树与快照各 3／3，最终快照 verify 3195 文件，全部通过。完整分片报告保留在本地生成目录，`windows/summary.json` 公开其哈希和统计；历史失败原样保留。
