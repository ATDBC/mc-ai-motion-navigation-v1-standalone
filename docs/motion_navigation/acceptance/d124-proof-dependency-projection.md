# D124 验收：恢复证明依赖分层

日期：2026-10-21。状态：**COMPLETED／SIGNED**。正式平台为 Windows；源码树、独立快照、Java 和最终导出门禁均已通过。本阶段没有运行 Minecraft／Fabric。

依据：[决定](../decisions/0124-separate-broad-physics-reads-from-proof-dependencies.md)、[架构](../architecture/proof-dependency-projection-v1.md)、[计划](../stages/d124-proof-dependency-projection-plan.md)。正式平台为 Windows；Linux 只作补充可移植性检查。

## 1. 前序事实与 RED

| 编号 | 已有事实或待复现缺口 | D124 要验证什么 |
|---|---|---|
| RED-01 | D122 v8：67 个依赖中 55 known、12 unknown，真实协议 0／3 | 宽查询与安全必要依赖分别记录，不能为通过而补事实 |
| RED-02 | D123-0：9 个上方有支撑的下层格仍有宽合同材料反例 | 用真实 1.21 形状与触发规则关闭适用范围，不能仅以三态样本等价删除 |
| RED-03 | 南向下层 `z=0、2、5` 影响父恢复或后继 | 分别未知时仍拒绝；只核 parent／successor 任一方的 mutation 被检出 |
| RED-04 | shapes、支撑查询和 prepared 支撑汇总会保留宽依赖 | 只在源查询按用途收窄，后续汇总不重新并回 |
| RED-05 | D123-1 固定站位转头不能取得全部下层事实 | 不恢复穿墙查询，不把本轮依赖收缩称为 actor 校准通过 |

RED-01—05 引用的是前序事实。D124 实施仍须保存当前源码上的可复现 RED、命令和哈希；本页没有伪造新的失败运行。

## 2. 依赖与几何门槛

- 四方向按格子、读取用途和 parent／successor／fallback／release 分支分类。联合必要依赖只合并实际安全前提。
- 完整方块屏障的几何、材料和版本前提均被记录；下层事实保持 `UNKNOWN`，WorldModel 不得新增该格空气或方块事实。
- 南向 `(0,62,z)` 的 `z=-6…-1、1、6、7` 为 9 个待证明对象；`z=0、2、5` 保留。其他方向检查对应旋转身份，不硬编码南向坐标放行。
- 若证明这 9 个下层格不再属于必要依赖且没有其他变化，67→58 是解释结果，不是数量型放行规则。有一格证据不足就保留并报告 PARTIAL，不删到目标数量。
- 上半砖、下半砖、楼梯和多盒拼接不继承完整方块放行；屏障失效后，下层必要依赖恢复，为未知时正式缺信息拒绝。
- 真实原版材料／形状成对取样，并由通用触发规则或保守上界覆盖未知格的全部适用状态。不能以若干材料样本代表所有未知；未覆盖机制、异常输入或不满足模型范围时保持明确拒绝或保守路径。
- vanilla 注册表只有 ID，移动特征目录只有效果；不把它们当成完整 `BlockState`、邻居或碰撞上下文形状目录。D124-0 须明确每类上下文的上界、合法已知来源或拒绝边界。
- 适用范围由现有 `PhysicsWorldView.__init__()`／`from_snapshot()` 入口按冻结检查表确认，并沿 world → sealed snapshot → worker request → proof 核对会话、规则、版本、digest 和请求归属。仅版本标签不够；不能再次读取已省略格。现有来源或身份链不足时停止，不产生可授权证明。
- 源查询候选与冻结宽查询 reference 独立对照。expected、必要依赖和 safety tail-only 见证不得由同一个新收集器生成；故意少收真实候选必须被独立对照检出。
- 主轨迹、step（含零高度候选）、着地／碰撞、潜行裁剪、精确支撑面积与材料、伤害、可执行 fallback 和 neutral release witness 均保持正确。
- 动态边界审计覆盖 `PistonExtensionBlock`、`PistonBlockEntity`、`Entity` 和 `FlowableFluid`：冻结屏障下的移动形状、推实体盒、蜂蜜拖动、身体流体扫描和表面效果都不能越过屏障影响其上方身体。合成 unsupported 只锁定查询与缓存语义。

## 3. 必须检出的 mutation

| 变异 | 必须出现的结果 |
|---|---|
| 去掉屏障、变未知／半砖／楼梯／缺口 | 不能使用完整方块规则；相关下层未知重新拒绝 |
| 屏障与身体／探测范围同时变化 | 越出适用范围就恢复依赖，不能复用旧屏蔽结论 |
| 单格、相邻双格、全部 9 格未知／真实实体组合 | 前提成立且确已证明无关时行为相同，事实本身不被改写 |
| 3 个必要格分别或组合未知 | parent、successor 或其恢复链按实际原因拒绝 |
| 漏掉一个 fallback／release 分支 | 联合依赖或安全检查失败 |
| 删除屏障依赖或修改屏障几何／材料 | 旧证明被拒绝；真实语义变化不能以 stamp 刷新掩盖 |
| owner 缺失、版本历史不完整、缓存跨变化复用 | 沿用既有失效规则拒绝 |
| world／snapshot／request／proof 的会话、ruleset、版本、digest 或请求归属不匹配 | 在对应既有入口拒绝，不能依赖最终 gate 或重新查询省略格补救 |
| expected 与候选调用同一个新收集器，或删掉 reference 中的真实候选 | 独立宽查询 reference 和固定见证检查失败 |
| `0e6b0141` parent-only／完整 safety tail-only 必要事实内容变化或变成 UNKNOWN | 准备前拒绝或撤销完整候选；不能只核继承依赖或后继主轨迹 |
| 上述必要事实只有 stamp 刷新、语义未变 | 保持 eligible；把刷新当成变化的 mutation 失败 |
| 只在序列化／清单／gate 去掉格子 | 检查证明源查询仍依赖该格，必须失败 |
| UNKNOWN 写成 AIR、fixture 回填 actor、隐藏变化通知 | actor 权限检查失败 |
| 真实特殊接触或异常材料／形状被普通化 | 明确未支持或合同拒绝，不能产出普通安全状态 |
| 只比较终点或只比较主轨迹 | 中途失支撑、伤害、fallback 或 release 差异被检出 |
| 导出漏文件，或同时删文件与哈希行 | include／required 及独立 verify 拒绝 |

变异必须在真实行为断言上失败；导入错误、没有运行或没有结果不能记为检出。

## 4. 回归、性能与体积

| 集合 | 门槛 | 当前结果 |
|---|---|---|
| D124 代表、四方向、跨 section 缓存和组合 mutation | 全部满足各自通过／拒绝条件 | 15／15，通过 |
| D120 冻结输入 | 972 个入口、2916 个后缀；旧成功退步 0，行为逐项一致 | 逐项一致，退步 0 |
| D122-3 正式组件矩阵 | 20／20 | 20／20 |
| D122 v1 兼容 | 120／120，wire 语义不变 | 120／120；变异全部检出 |
| B09-R／ground／D102／D116／D121／D122／D123 相邻检查 | 全部通过；历史断言与新语义分开 | 独立审查修复后 206／206 |
| `0e6b0141` parent-only／完整 safety tail-only、独立 reference 与身份链 | 内容变化／UNKNOWN 撤销，纯 stamp 保持；独立对照与 mismatch mutation 全过 | 通过；新增 parent-only 两项通过 |
| 生产控制准备 | P95 ≤ 8 ms、P99 ≤ 15 ms、最大 < 30 ms | 0.4712／4.4607／4.7067 ms，通过 |
| 同步提交计算 | < 50 ms | 最大 14.4417 ms，通过 |
| 每类正式 worker 请求和结果 | protocol-5 ≤ 2 MiB；无第二份完整 proof 或第二管线 | 92,875／442,163／493,588 B，通过 |
| action | 既有 ≤ 16 KiB，Java／wire 不改 | 覆盖全部 operation 类型和最大合法字段；最大 1,102 B，通过 |
| Windows 源码树、独立 standalone | 正序／逆序全部通过 | 源码树各 209 模块／2748 项；standalone 各 208 模块／2724 项；全零失败、错误、超时、跳过和清理失败 |
| Java 相邻门禁与导出校验 | 全部通过，文件与哈希完整 | 设计度量／清单 42／42；源码树和快照 Java 各 3／3；最终 verify 3195 文件 |

依赖和摘要的变化单独解释，不回写 D120 原始 coverage 或历史 proof。不能拿历史 D120 全绿代替 D124 改动后的回归。

`0e6b0141` 的具名检查位于 `tests/motion_nav/test_d121_successor_binding.py`：`test_parent_only_dependency_change_is_rejected_before_prepare`、`test_tail_only_changed_dependency_revokes_full_candidate`、`test_tail_only_unknown_dependency_revokes_full_candidate`、`test_tail_only_stamp_refresh_keeps_full_candidate_eligible`、`test_stamp_only_refresh_keeps_semantic_dependencies_eligible`。D124 另补 parent-only 的 UNKNOWN 撤销和纯 stamp 保持资格用例。必须保留独立见证与结果，不能从新收集器产生的差集反推 expected，也不能因差集为空跳过。

计数与无 profile 计时分开，先冻结同机输入、重复次数和区间。合法等待与端到端延迟单列；不得事后扣掉 GC、最大值或失败样本。所有正式检查的失败、错误、超时、跳过、预期失败、意外成功和清理失败均为 0。

## 5. 计划命令

下列命令均已运行。Python 检查使用项目环境；Java 门禁明确使用项目环境内的 OpenJDK 21。

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_d124_proof_dependency_projection -v
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/d124_proof_dependency_probe.py --output evidence/motion_navigation/d124-proof-dependency-projection-v1/targeted/run-8
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.motion_nav.test_b09r_physics_contracts tests.motion_nav.test_b09r_physics_step tests.motion_nav.test_world_knowledge_geometry tests.motion_nav.test_d116_semantic_dependency_handoff tests.motion_nav.test_d121_successor_binding tests.motion_nav.test_d121_momentum_handoff tests.motion_nav.test_d122_formal_timing tests.motion_nav.test_d122_timing_contract tests.motion_nav.test_d123_dependency_probe tests.motion_nav.test_d123_actor_calibration -v
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/run_motion_nav_shards.py --root . --report .tmp/d124-raw/windows/source-forward-final2.json
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python scripts/run_motion_nav_shards.py --root . --reverse --report .tmp/d124-raw/windows/source-reverse-final2.json
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output python -m unittest tests.test_standalone_java_gates tests.motion_nav.test_standalone_export -v
git diff --check
```

探针拒绝覆盖已有证据。D120 重算、D122 120 项、生产性能和正式消息体积应接入新探针或明确的既有入口；实施时在本页补齐实际命令、冻结配置和结果，不得因当前未列 CLI 就跳过门槛。

导出先指定上一份已验证 byte base，并把下列占位路径替换成实际路径。不能使用未验证目录或导出目标自身作为 byte base。独立快照源码与主树分开执行。

```text
python scripts/export_motion_navigation_standalone.py export --root <全新独立快照目录> --byte-base-root <上一份已验证快照>
python scripts/export_motion_navigation_standalone.py verify --root <全新独立快照目录>
python scripts/run_motion_nav_shards.py --root <全新独立快照目录> --report <新证据目录>/standalone-forward.json
python scripts/run_motion_nav_shards.py --root <全新独立快照目录> --reverse --report <新证据目录>/standalone-reverse.json
```

上面 `python` 同样通过项目 conda 环境调用。源码树与独立快照均须使用当次完整模块集合，不能写死历史模块数。共同文件只有行尾差异时保留旧公开字节。

## 6. 证据、停止与签署

D124 证据只追加到 `evidence/motion_navigation/d124-proof-dependency-projection-v1/`。每批保存环境、源码／原版来源哈希、RED、分类、mutation、命令、原始失败和最终结论。公开紧凑索引必须引用真实存在的文件，并标明哪些原始大文件仅由主项目保管。

每批独立复核后才进入下一批；最多两轮限定修正。原版反例推翻规则、适用范围不清、3 格被删、必须 gate 过滤／补事实、扩大 sensing／viewpoint／Session／transport／Java，或正式门槛失败时，停止扩大验收并保存证据。

D124 完成只签署依赖合同与已证明范围。剩余 3 格缺失就继续报告 `NEEDS_WORLD`；它不授权 D123-2／D123-3，不证明 actor 校准或连续跑酷已通过。D121—D123 历史失败及 PARTIAL／STOPPED 状态不变。代码、测试和原始证据与本文目标冲突时，以查明后的事实为准并修正文档。

## 7. 当前证据边界

公开证据位于 `evidence/motion_navigation/d124-proof-dependency-projection-v1/`。公开目录只保留规则审计、逐方向签名、结果摘要、源码哈希和消息体积。D120 每入口明细与 D122 逐帧结果保留在本地生成目录，其 SHA-256 写入紧凑记录，不进入 standalone。

`targeted/run-1` 是实现过程中的首次探针，`targeted/run-2` 是首次实现字节。`targeted/run-3` 保留独立审查修复后的过程记录，但源码清单尚未纳入 `safe_ground_control.py`；`targeted/run-4` 保留首次完整源码清单；`targeted/run-5` 绑定跨 section 修复后的最终生产字节和 action 体积；`targeted/run-6` 增加活塞动态边界审计；`targeted/run-7` 因 Java float token 写错而停止并保留失败；`targeted/run-8` 是修正后的最终规则审计。D120 紧凑记录保留四方向的旧／新签名和原始结果哈希；D122 紧凑记录保留 20 场结果、性能分位数和完整结果哈希。历史 D121—D123 证据未改写。

完整 Windows 源码树正逆分片、独立 standalone 正逆分片、Java 相邻门禁和最终导出均已通过。首次源码全量与首次 standalone 的失败报告没有改写：前者暴露旧合同断言，后者暴露缺少 D122 紧凑历史和 D123 历史证据；限定修正后最终字节全绿。完整原始分片报告保留在本地 `.tmp/d124-raw/windows/`，公开 `windows/summary.json` 记录其 SHA-256、大小与统计。D124 只签署依赖合同与已证明范围，不授权 D123-2、连续跑酷或新 Fabric。

## D126 纠偏记录

D126 没有修改 D124 proof 或生产地图语义。四方向仍各有 58 个必要依赖；测试夹具以已知地图提供这些事实。少一格和改错材质的反例继续由同一门禁拒绝。
