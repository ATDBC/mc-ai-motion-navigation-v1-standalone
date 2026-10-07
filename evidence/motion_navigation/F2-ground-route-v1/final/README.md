# F2 Task 5 紧凑证据

这份目录保存正式 Windows 组合检查与 Fabric 验收证据。冻结 93 项清单已完整通过独立审计，原失败另存，不计入有效批次。

采集起点为 `5a78b55dcc9b994731558a0ca16cb0e1460ce66c` 加 Task 5 修正。脚本如实记录工作区 dirty。最终 Windows 所用 210 个生产文件总指纹为 `5950cb30230bb0112ddd8b3e47930a248764452d7bd9f639657eebc2279ae495`；交付身份以实际文件哈希为准。历史 baseline、Task 2—4、第一轮及失败证据均保留。

| 文件 | 内容 |
|---|---|
| `windows/forward-final.json`、`reverse-final.json` | 最终代码正式正逆各 1627/1627，场景哈希不变 |
| `windows/forward.json` | 首轮 1617/1618 原失败，不是最终检查 |
| `windows/formal-start-chain.json` | Session／Driver／Executor 自己生成多点 Walk 窗口，检查按时／晚1／晚2／视角失选的真实 Runtime 回执 |
| `windows/formal-start-chain-oldgate-red.txt` | 旧多点排除规则的错误副本被持久正式链正例发现 |
| `windows/look-binding.json` | 独立手工意图仲裁的补充检查；不代替正式链窗口证明 |
| `five-groups/` | 五组完整 ID、行为签名和各组判定 |
| `comparisons/` | 与冻结来源逐 ID 比较、组件、玩家原目标证明和严格动作切片 |
| `f2/` | 最终 528 项索引与摘要 |
| `performance/` | 专项与重新测量的 D058／D061 原门槛、来源和紧凑性能 |
| `source-consistency.json` | 正逆、组合、专项、D058／D061 依赖路径与当前文件逐项一致 |
| `raw-windows-manifest.json` | 本地完整证据的位置、字节数和 SHA256 |
| `fabric/` | 93 项完整计划／有效批次／逐项摘要／质量／90 个来源注销；原失败单独保留 |
| `reproduce/` | 复跑和审计脚本；自动寻找仓库根目录 |

正式环境为 Windows、仓库内 Conda Python 3.11.16。可按以下顺序复跑；脚本输出到 `.tmp/f2-task5-startfix-*`，不会改写冻结历史证据。

```powershell
.\.venv\python.exe evidence/motion_navigation/F2-ground-route-v1/final/reproduce/run_windows_checks.py
.\.venv\python.exe evidence/motion_navigation/F2-ground-route-v1/final/reproduce/audit_comparisons.py groups
.\.venv\python.exe evidence/motion_navigation/F2-ground-route-v1/final/reproduce/audit_comparisons.py f2
.\.venv\python.exe evidence/motion_navigation/F2-ground-route-v1/final/reproduce/audit_strict_motion.py
.\.venv\python.exe evidence/motion_navigation/F2-ground-route-v1/final/reproduce/verify_look_binding.py
.\.venv\python.exe evidence/motion_navigation/F2-ground-route-v1/final/reproduce/verify_formal_start_chain.py --old-gate-mutant
.\.venv\python.exe evidence/motion_navigation/F2-ground-route-v1/final/reproduce/audit_sources.py
.\.venv\python.exe scripts/f2_ground_route_runtime.py --smoke
.\.venv\python.exe scripts/f2_ground_route_runtime.py
```

五组的 ID 和参数不变，但整个轨迹没有逐项全同。产品为 1998/2000，相比冻结 1718/2000 新增 280 项，原成功退步为 0。112 组件行为逐项一致；玩家以 Task 3 `be605fe4` 的只读结果比较，187/400 → 400/400，新增 213 项均有原 `GoalState` 的正式完成证明。严格动作 800 项原结果和作业不变；398 项非空空中动作切片逐帧不变，另外 402 项整任务不变。

大批轨迹没有加入 Git。审计严格动作与五组详细比较需要 `raw-windows-manifest.json` 指向的本地原始运行；仅凭紧凑索引能复核哈希和声明边界，不能重新生成缺失的完整轨迹。

实机 80 个正例原目标完成，40 次晚1在窗口内应用；三组比值 1.000／1.235／1.074，4773 个质量观察没有连续 10 tick 非中性停滞。四个负例拒绝；外力本轮横推 0.35909 格后在同一路线完成，早期 0.53014 格横推后的有界失败保留。90 个 ordered 来源均有注册／注销，安全事件为 0。

```powershell
.\.venv\python.exe evidence/motion_navigation/F2-ground-route-v1/final/reproduce/audit_fabric.py --manifest evidence/motion_navigation/F2-ground-route-v1/final/fabric/accepted-batches.json
```

该审计需要清单中本地 artifacts 原始流；紧凑摘要不能代替缺失的实机观察。无移动活塞表示、动态避障、路线优化器、疾跑或新动作验收。
