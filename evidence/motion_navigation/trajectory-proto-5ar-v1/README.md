# TP Task 5A-R R1／R2 证据

日期：2026-10-10。正式平台：项目 Windows `.venv\python.exe`。

本目录保存 R1、R2 候选和 R2 独立 G／J／A 限定修正失败的证据。R1 基线为 `5294449598db56b4735f165a7e044b0c5b9d2ead`，首个 R1 提交为 `ce9b2dcf490c0be32da8e3d5646e379a07829a8d`，修正提交为 `104318a00e1af8302e986e1b6b465a99e6a7d71f`。R1通过。原提交保留，没有重写。

R2候选 `cfd36acd27f03099ed3f6e1fb9434a2dd3553419` 把逐 tick 前端换成累计输入层和 `G→J→A→B` 动作元枚举，并加入最小终速、获胜输入层和搜索计数。审查确认它把 G、J、A 绑定为同一控制，遗漏合法混合动作元，因此旧矩阵94／94不能签署R2。限定修正让反例转绿后，三个冻结A15正例耗尽65,536 physics step。R2未通过，R3未开始，TP结束。没有修改 `mc2p`。

文件：

- `red-green.md`：失败测试、通过测试与行为边界；
- `r2-matrix.json`：冻结矩阵的状态、分类和计数；
- `r2-independent-gja-failed.diff`：未提交限定修正的完整diff；
- `r2-independent-gja-failed.json`：反例转绿与三项预算失败的结构化计数；
- `r2-independent-gja-failed.md`：失败经过与退出结论；
- `verification.md`：R1、R2候选与终结复核结果。
