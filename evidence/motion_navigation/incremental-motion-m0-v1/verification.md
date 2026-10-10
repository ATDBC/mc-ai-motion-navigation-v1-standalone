# M0 验证记录

## 阶段判断

M0 未通过。失败项是“正式无高度截断口径下，正例最大耗时不超过 400 ms”。三次正式采集均稳定失败，不能解释为单次系统调度尖峰。

通过项：

- 22 个原正例和混合动作元反例全部找到；
- 6 个负例分类符合冻结预期；
- 有意义负例最大耗时低于 800 ms；
- lazy 单次证明估算 P95 低于 10 ms；
- 已知输入 rollout P95 低于 15 ms；
- `PYTHONHASHSEED=1／77／991` 的结果、输入和计数一致；
- 直线跨隙在 A3／A5／A15 返回相同输入哈希。

未通过项：

- 混合动作元反例三次最大值为 587.9058／591.1611／736.8842 ms，全部超过 400 ms。最终模块入口复跑的 P50／P95／最大值为 733.5156／735.3165／736.8842 ms。

## 边界

- M0 量的是控制器参数探测、已知输入完整扫描和拆出的 step 估算。
- 增量证明尚未实现，lazy 毫秒数只是 `step 数 × 同轮 rollout ms/step`。
- 未修改 `mc2p`，未生成离线表，未接正式链，未运行 Fabric。
- 带高度截断结果只作诊断；正式判断没有使用这项未证明限制。
- `tiny_budget` 只检查分类，不进入无解成本门槛。
- `controller_ms` 包含约 26—31 ms 的冻结夹具和世界快照构造。缓存夹具后混合反例仍约 556—695 ms，停止结论不变。
- M0 的 `tiny_budget` 是直接分类，不是新的真实计数器耗尽试验。`PHYSICS_STEP_BUDGET` 的计数器路径沿用旧 TP 94 项冻结回归。

## 最终检查

```text
python -m unittest tests.motion_nav.test_trajectory_proto_m0_probe -q
Ran 7 tests ... OK

python -m unittest \
  tests.motion_nav.test_trajectory_proto_contracts \
  tests.motion_nav.test_trajectory_proto_commitment \
  tests.motion_nav.test_trajectory_proto_reference_search \
  tests.motion_nav.test_trajectory_proto_primitive_matrix -q
Ran 94 tests ... OK

python -m unittest tests.motion_nav.test_standalone_export -q
Ran 12 tests ... OK
```

`git diff --check` 通过。相对 M0 方案提交 `3b313d2a`，`mc2p` 差异为空。
