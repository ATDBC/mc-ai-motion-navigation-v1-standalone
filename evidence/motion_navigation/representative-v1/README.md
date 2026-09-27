# 代表性真实运行证据

这里保留九份 profile 3 历史批次，以及一份当前 profile 4 的 B12-B 部分观察战斗移动批次。历史批次覆盖 B10-C 跨隙、C1-B 移动近战、C1-C 外力恢复、B11 放置、B12-A 伤害来源和旧 B12-B；它们只用于核对当时结论和复现旧问题。当前 B12-B 批次使用 profile 4 表面深度，可用于核对本轮现行验收。

B10-C 通过批次保留 210 个协调控制帧、10 个协调试次、142 个求解试次和物理 tick 片段。B12-A 批次保留玩家近战和环境伤害来源诊断。B12-B 批次保留 34 个 Fabric 场景、控制事件和分段 Runtime 轨迹。不复制普通日志、画面或缓存。

运行：

```powershell
python scripts/public_runtime_evidence.py verify --root evidence/motion_navigation/representative-v1
```

验证器会检查总清单、SHA-256、归档成员边界、JSON/JSONL 可读性、完整批次数量、通过汇总和失败分类。失败归档仍表示当时真实运行失败；当前代码后来能够重放或已修复，不会改变历史结论。

这些样本能让审查者核对文档引用的真实数据和证据读取链。索引和每个归档中的 `public-run.json` 会分别标记 `historical_legacy_ray_profile3` 或 `current_surface_depth_profile4`；只有后一类可以设置 `current_acceptance_eligible=true`。
