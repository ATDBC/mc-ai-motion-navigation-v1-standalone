# 代表性真实运行证据

这里保留 B10-C 跨隙、C1-B 移动近战和 C1-C 外力恢复各一个完整通过批次、一个完整失败批次，并加入 B11 放置与有限搭桥、B12-A 伤害来源、B12-B 部分观察下战斗移动的历史批次。它们全部使用已经退出正式主线的 profile 3 稀疏射线，只用于核对当时的运行结论和复现旧问题，不能证明当前 profile 4 表面深度主线已经通过对应阶段。

B10-C 通过批次保留 210 个协调控制帧、10 个协调试次、142 个求解试次和物理 tick 片段。B12-A 批次保留玩家近战和环境伤害来源诊断。B12-B 批次保留 34 个 Fabric 场景、控制事件和分段 Runtime 轨迹。不复制普通日志、画面或缓存。

运行：

```powershell
python scripts/public_runtime_evidence.py verify --root evidence/motion_navigation/representative-v1
```

验证器会检查总清单、SHA-256、归档成员边界、JSON/JSONL 可读性、完整批次数量、通过汇总和失败分类。失败归档仍表示当时真实运行失败；当前代码后来能够重放或已修复，不会改变历史结论。

这些样本能让审查者核对文档引用的真实数据和证据读取链。索引和每个归档中的 `public-run.json` 都把它们标成 `historical_legacy_ray_profile3`，并明确写出 `current_acceptance_eligible=false`。
