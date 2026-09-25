# 代表性真实运行证据

这里保留 B10-C 跨隙、C1-B 移动近战和 C1-C 外力恢复各一个完整通过批次、一个完整失败批次，并加入 B11 放置与有限搭桥的最新完整通过批次。归档只含结构化结果、试次清单和轨迹，不含 Minecraft/Fabric JAR、世界、普通日志、画面或缓存。

运行：

```powershell
python scripts/public_runtime_evidence.py verify --root evidence/motion_navigation/representative-v1
```

验证器会检查总清单、SHA-256、归档成员边界、JSON/JSONL 可读性、完整批次数量、通过汇总和失败分类。失败归档仍表示当时真实运行失败；当前代码后来能够重放或已修复，不会改变历史结论。

这些样本能让审查者核对文档引用的真实数据和证据读取链。它们不是新的 Fabric 实验，也不能单独证明当前代码在所有场景继续达到相同成功率。
