# F2-SC：普通地面安全尾迹与完成判断分离方案

日期：2026-10-08。状态：已按 fail-fast 实施并停止，阶段未通过。

**目标：** 普通 `STANDING + WALK` 每玩家 tick只推进一步。硬安全检查所有可能生效的输入前缀和中性尾迹；completion 单独判断完成并指导制动、换向和一／两 tick许可选择。

**依据：** [D082](../decisions/0082-separate-safe-tail-from-completion.md)、[D081](../decisions/0081-select-ground-control-duration-from-stopping-margin.md)、[F2-PC 失败记录](../acceptance/F2PC-ground-precision-control.md)和 [D080](../decisions/0080-share-ground-tracking-policy-for-terminal-approach.md)。

## 1. 已知事实

- F2-PC 的生产改动已撤销，只保留 RED 和两次失败记录；
- 组件已经证明一 tick和两 tick两种控制粒度都有实际用途；
- F2S-C-04 的直接阻塞是每帧安全尾迹被要求立即停进 completion；
- 普通 Walk 正式执行按玩家 tick重新决定，固定两 tick只存在于策略模拟；
- 严格动作、Session 生命周期、A* 通用循环和目标区域不在本阶段修改范围内。

## 2. 预计修改范围

只在确有调用关系时修改：

- `mc2p/motion_nav/ground_tracking_policy.py`；
- `mc2p/motion_nav/fixed_route.py`；
- 普通地面 rollout、完成谓词和停滞监视器；
- 普通 Walk 的意图与输入账本接线；
- 对应测试、证据和四类文档。

明确不改：通用 A* 循环、Session／PlannerWorker 生命周期、completion 几何、目标面集合、动作接口、严格动作证明、D068 原白名单。

## 3. 批次 0：冻结 RED 和错误副本

先保存当前 F2S-C-04 失败以及 F2-PC 的 expected-failure，不改生产代码。

至少冻结这些行为：

1. 安全尾迹留在支撑和走廊，但没有停进 completion，候选仍应可用；
2. 当前身体在 completion 外，但预测停止点更接近 completion，不能提前完成；
3. 当前身体在 completion 内，但速度和中性尾迹会带出区域，不能完成；
4. 当前身体、模式、姿态、终速和中性尾迹都满足目标，才能完成；
5. 两 tick许可分别检查 `0/1/2` tick前缀；一 tick许可检查 `0/1` tick前缀；
6. rollout 一次只推进一个玩家 tick；
7. 冲过 completion 后能够选择经过证明的反向修正；
8. 制动或换向的短期平台期不立即算停滞，长期无进展必须有界退出；
9. 晚到、丢失和只生效部分前缀后，从正式观察重新锚定；
10. 严格动作不进入这套规则。

错误副本至少包括：

- 继续要求安全尾迹停进 completion；
- 只检查完整许可，不检查 `0` tick和中间前缀；
- 只看当前位置，不检查完成后的中性尾迹；
- 预测会进入 completion 就提前完成；
- 两 tick许可让 rollout 一次前进两 tick；
- 把许可长度直接当作 A* 成本；
- 冲过后仍只允许原方向前进；
- 每帧距离未下降就立即判停滞；
- 无进展永不终止；
- 用 reason、场景 ID、固定距离或评分权重特判主例；
- 晚到输入延后补发；
- 严格动作复用普通地面完成谓词。

**完成条件：** 每个错误副本至少由一个行为断言检出；现有 F2-PC RED 保持失败证据，不改写为通过。

## 4. 批次 1：实现两个纯谓词

先实现并单独测试：

- `safe_tail`：给定当前状态、输入和许可长度，检查所有可能前缀及其中性尾迹是否留在已知安全支撑和路线走廊内；
- `completion`：检查当前状态和当前中性尾迹是否完整满足目标。

两者不能相互调用，也不能共享一个“必须在 completion”的最终布尔条件。可以共用物理计算、扫掠和缓存，但结果必须分别记录。

**完成条件：** 改坏任一谓词时，只破坏它对应的错误副本；UNKNOWN 和危险边界没有放宽。

## 5. 批次 2：逐 tick控制和时长选择

1. rollout 每次只推进一个玩家 tick；
2. 一／两 tick许可都先过 `safe_tail`；
3. 在安全候选之间，用预测停止点到 completion 的距离、沿末段的有符号误差、预计终速和停止余量确定方向与 `control_ticks`；
4. 两 tick没有质量优势时使用一 tick；
5. 超调后允许经过证明的反向修正；
6. policy、`FixedRouteDecision`、移动意图、Runtime 窗口、账本和 rollout 记录相同的 `control_ticks`；
7. 新玩家 tick到达后总是重新计算，不回放旧决定。

许可长度不进入单步 rollout 的位移和成本。规划端要累计每一步真实预测结果。

**完成条件：** 0／1／2 tick可能前缀全覆盖；候选顺序变化不改变结果；错误副本全部被发现。

## 6. 批次 3：进展、制动和停滞

普通路线中段保持现有进展定义。terminal 段记录：

- 当前状态到 completion 的距离；
- 中性停止点到 completion 的距离；
- 沿末段方向的有符号误差；
- 当前速度与预计终速；
- 正在接近、制动还是反向修正。

短期制动和换向可以不增加弧长进展，但必须在冻结窗口内改善上述至少一项。窗口耗尽后返回 typed 停滞结果，不得永久往返。

**完成条件：** 正常制动和一次超调修正不会误报；制造原地往返、无位移和停止点不改善时都会有界退出。

## 7. 批次 4：只接正式主例

恢复 F2S-C-04 所需的最小 terminal edge、接纳和执行接线。只运行：

- F2-SC 定向检查；
- 原 F2-GP `51/51`；
- F2S-C-04 normal、晚到和丢失负对照。

若 normal 主例仍失败，只允许判断失败属于：

- 完成谓词错误；
- terminal 进展或停滞谓词错误。

晚到或丢失负对照单独失败时，才允许检查输入账本与实际前缀是否一致。

只允许修正这三类契约或接线错误。若需要调权重、改 completion、加候选或固定距离阈值，立即停止。

## 8. 批次 5：Windows 分层回归

主例通过后，依次运行：

1. 剩余 9 项和已恢复 42 项；
2. v9 216、v8 固定 104、v8 杂乱 1800；
3. v7 2000、F2 528、F2-R 1904；
4. 五组行为、严格动作和协调集合；
5. 完整 motion_nav 正序和逆序；
6. D061；
7. 1、4、16、32 终点规划矩阵。

任何一层失败即停。Windows 是正式验收平台。性能门槛保持控制生产 P95 `≤8 ms`、P99 `≤15 ms`、最大值 `<30 ms`，完整夹具最大值 `<50 ms`，deadline miss 为 `0`。

## 9. 批次 6：Fabric 正式链

至少覆盖：

- 四方向窄 completion 的 normal／晚到／丢失；
- 一 tick精细接近和远处两 tick对照；
- 超调后的安全反向修正；
- 当前在区域内但尾迹会离开的未完成反例；
- 当前状态和中性尾迹都在区域内的完成正例；
- 目标修订、相关与无关世界变化；
- JumpGap 或 ControlledDrop 严格动作对照。

记录每帧正式观察、实际生效前缀、`safe_tail`、completion、停止点距离、方向、`control_ticks`、进展窗口、来源所有者和终态。

## 10. 止损和关闭条件

出现以下任一情况立即停止：

- 需要调评分权重、完成区域、候选数量或固定距离阈值；
- rollout 需要一次跨过多个玩家 tick；
- 需要新增 Session 状态、等待或重试；
- 严格动作证据改变；
- 历史成功退步或安全门槛失败；
- F2S-C-04 normal 主例的失败不属于完成或停滞谓词；
- 晚到或丢失负对照的失败不属于账本前缀接线。

F2-SC 只有在定向检查、分层 Windows 回归、D061、规划性能和 Fabric 都通过后才能关闭。关闭只解除普通地面末段控制阻塞；F2-GP、F2-SG、F2-S 和路线优化器仍需各自验收。

## 11. 实施结果

本轮保留了三批可独立验证的改动：

- `safe_tail` 与 `completion` 已分开。前者检查 `0/1/2` tick可能前缀及完整中性尾迹，后者只判断当前状态是否完成；
- 普通 `FixedRoute` 已传递策略选择的 `control_ticks`，rollout 每次仍只推进一个玩家 tick；
- terminal 监视器记录当前位置、预计停止点和速度是否改善。completion 规则只在最后一段生效，中段继续使用原路线进展。

Windows 聚焦检查为 `54/54`。F2S-C-04 normal 主例完成，结果为 `goal_state_satisfied`，零伤害、零安全违规。

随后按顺序运行 remaining9。结果为 `4/9` 完成、`5/9` 有界失败、零安全事件：

- `f2r/clutter/0.1/7/22/product` 返回 `fixed_route_has_no_forward_control`。身体已经停在 completion 外约 `0.007` 格，存在安全修正，但现有前进资格仍只认路线弧长，没有把 completion 距离改善算作该候选的进展；
- 另外四项返回 `no_safe_ground_candidate`。九个简化候选都要求完整 1.21 复核，现有有界复核只检查前三个；前三个没有形成可接纳前缀，后续候选没有被检查。

第一项属于尚未补齐的 terminal 进展谓词。后四项需要改变完整复核候选数量或顺序，已经越过本阶段止损边界。因此没有继续调权重、completion、候选数量或场景参数，也没有接回 A*、接纳链和后续大集合。

当前保留已经通过的职责分离和 C04 修复。F2-SC 继续记为未通过。remaining42、v9/v8/v7、F2/F2-R、五组、严格动作、协调集合、完整正逆序、D061、性能矩阵和 Fabric 均未运行。紧凑证据见 `evidence/motion_navigation/F2SC-safe-tail-completion-v1/`。

后续不在 F2-SC 内调大复核名额或改变候选顺序。[D083](../decisions/0083-verify-the-closed-ground-candidate-family.md) 和 [F2-CV](F2CV-closed-ground-candidate-verification.md) 已冻结封闭候选族、共享物理前缀、typed 预算结果及独立 terminal 历史进展方案。F2-CV 通过后，再回到本阶段尚未运行的门槛。
