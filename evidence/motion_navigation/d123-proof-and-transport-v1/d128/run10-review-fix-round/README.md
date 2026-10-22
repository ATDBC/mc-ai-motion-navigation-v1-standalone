# D129 run-10 复审修复离线证据

本目录只保存复审后的离线门禁。没有启动 Minecraft／Fabric，也没有运行新的 formal smoke 或 matrix。

## 原始分类验证

`../smoke-run-10/classification-validation.json` 由 `scripts/d129_invalid_harness.py` 只读生成。validator 先验证历史 `manifest.json` 的 50 个文件大小与 SHA-256，再读取 raw trial、trace、typed V4 receipt、F-TICK、physics sidecar 和 close terminal。它不读取 `classification-review.json`。

结果为 `PASS`：七项合取全部为 true，全部 disqualifier 为 false；被测 input application 0、default application 0，检查 physics tick 8—125 共 118 行。

## RED／GREEN

新增 mutation 在修复前精确暴露：terminal 位移没有进入身体门槛；畸形 receipt batch 被当成空批次；dropped samples 没有触发 transport disqualifier；prepared stream 未 activation 仍可能绑定为 successor。修复后，manifest hash、七项逐项翻转、全部 disqualifier、typed receipt 畸形、任一 input/default application、身体移动／伤害、transport／正式 runtime 或 driver failure、identity 重复、未 activation、字段缺失和单字段 stale mutation均被拒绝。

验证结果：

- D129、D122 Fabric timing、D123 Fabric transport、D126：45／45；
- D122 timing／formal timing、D123 actor／dependency 相邻检查：43／43；
- `offline-preflight.json`：PASS，组件 smoke 3／3、矩阵 36／36、Java／overlay PASS，`fabric_started=false`；
- standalone export／verify：3501 files。

正式产品结果保持 smoke 0／3；matrix 仍未运行。
