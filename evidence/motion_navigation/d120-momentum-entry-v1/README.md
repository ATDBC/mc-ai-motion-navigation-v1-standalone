# D120 有限入口校准公开证据

Windows 正式平台，2026-10-19。离线 972 个联合入口、2916 个后缀通过；朝南 3 场和四方向 36 场通过。仅为固定输入与有限样本校准，`sound_envelope=unverified`，不授权生产连续跑酷。

`index.json` 校验这里每个小文件，并登记未复制的大文件原哈希、正式命令与分析脚本语义哈希。`source-index.json` 是本地原输出的原字节索引，指向的完整 coverage/analysis 不全部包含在本公开包；不能拿它当作公开包的文件清单。

`analysis.json` 只省去后缀的逐 tick trace 和 final_state，保留逐 trial 结果、时序、入口余量、完整尾迹支撑、物理差异、健康与责任截止信息。完整 raw 没有复制，只有 summary 中的哈希。公开读者可核验摘要身份并重新生成离线输入，无法仅凭这些摘要独立重放 Fabric 全轨迹。原始轨迹保持在 summary 的 source_run 本地目录。

`history/` 按原字节保存 JDK 预检失败、首条 reset 健康观察误拒、北向 ±180° 角差误拒的失败摘要及日志；历史失败不改写。D118 仍为历史 FAILED／STOPPED。

L+2 是固定 RUN hold 对照，不是网络迟到注入。连续实数入口范围、真实网络任意晚到及 D118-1—D118-5 仍未验证、未授权。

最终发布门禁：Windows 源码树正逆各 197 模块／2534 项，独立公开快照正逆各 196 模块／2510 项，失败、错误、超时、跳过和清理失败均为 0。Java 源码树和快照各 3/3。完整矩阵所用快照 verify 为 2634 文件；新增本次小摘要后的导出校验另记，不能混为同一个文件集。四份本地原报告只登记路径、字节数和 SHA-256，见 `windows-regression/summary.json`。

补签后的独立导出已通过 verify，共 2635 文件，比矩阵快照增加本次 `windows-regression/summary.json` 一个文件。
