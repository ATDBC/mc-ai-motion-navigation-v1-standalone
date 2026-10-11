# P3 验证记录

正式量尺命令：

```text
.venv/python.exe scripts/measure_incremental_motion_p3.py --rounds 3 --output evidence/motion_navigation/incremental-motion-r98-v1/p3/closed-loop-matrix.json
```

证据修正覆盖：

- 后端拒绝裸 `TickInput`，1,227／1,227 次推进携带 owner、ledger、permit 或 timing-branch 来源；
- 应用记录先于阶段计算，承诺命令生效后的下一观察为 `POST_COMMIT`；
- 应用窗口末 tick 的 pending／missing 次序有单独检查；
- 真实 `GoalState` 修订、跨隙落地边界／速度／同 owner 地面接续有直接记录；
- 取消请求持续到安全释放或责任未解决；本轮 6 个承诺边界或承诺后取消均进入 `BODY_RESPONSIBILITY_UNRESOLVED`，没有伪造成功恢复；
- 正常 proof 与四个真实 fault proof／permit／命令都调用 owner 的同一提交门禁；四个变异 4／4 被阻止，ledger 新增提交 0；
- 显著目标修订会检查两条时序终态并拒绝旧输入；仍被旧终态满足的小修订继续允许重证；
- 111 个场景均有正数完整决定耗时，没有 0 ms 占位；
- P3 明确失败：54 个身体责任未解决，6 个伤害超额，承诺后恢复 0／27；
- `mc2p` 相对 P2 基线 `4ccb1309` 无变化；
- 没有运行 Fabric。

最终检查数和 manifest 哈希在本次提交前重新生成，不沿用 `a59efb4f` 的旧记录。

最终命令结果：

- P1／P2／P3 直接检查：55／55；
- 原 TP／D097 与导出检查：113／113；
- 独立 reference generator `--check`：通过；
- `git diff --check`：通过；
- `mc2p` 相对 `4ccb1309`：无变化。
