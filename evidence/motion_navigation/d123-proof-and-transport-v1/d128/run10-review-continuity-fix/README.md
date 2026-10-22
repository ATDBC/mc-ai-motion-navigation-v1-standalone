# D129 run-10 连续性门禁

本目录只保存最后一轮离线复审证据。没有启动 Minecraft／Fabric。

- `classification-validation-v2.json` 先验证历史 manifest 50／50，再验证 movement tick 8—125 共 118 行连续且唯一，相邻 `post_state`／`pre_state` 连贯。
- loader 保留区间内全部 92 个正式 observation；health 全程 20，全部 `damage_events` 为空。
- 删除 tick、复制 tick、中途 health 下降后恢复、中途 damage event 四个 mutation 先 RED，修复后 4／4 GREEN。
- 必要 D122／D123 相邻检查 3／3。
- `offline-preflight.json` 为 PASS：组件 smoke 3／3、矩阵 36／36、Java／overlay PASS，`fabric_started=false`。
- standalone export／verify 3504 files。

正式 smoke 仍为 0／3；matrix 未运行。
