# B01 验收：三版参照与统一比较口径

日期：2026-09-18
结论：通过有限验收；B01 完成

## 验收范围

本次验收回答三个问题：三版身份是否能准确复核，四类场景是否有固定身份，现有实机证据是否能在不修改原文件的前提下导出统一格式。它不评价新导航算法，也不补做新的游戏成绩。

## 结果

| 检查 | 结果 |
|---|---|
| 三版职责独立，且都未标记为新底座 | 通过 |
| GitHub 固定提交 | `d72d6b99a9ceb99eca454a3e35b580bc6bf36a9b` |
| 公开源码与报告 | 165 个文件，2,216,300 字节，逐文件 SHA-256 通过 |
| 校验表 SHA-256 | `73b261231d179df68ab6187c3743868cb9c8167db763bcc3243787fef8cbc8c6` |
| S00／S02／S04／S05 身份 | 通过；地图内容哈希、种子、起终点已登记 |
| 归一化单测 | 5 项通过，包括损坏证据、覆盖目标目录和跨时钟规则 |
| 三版真实封存记录 | 均成功导出四个统一文件；原始目录未写入 |

三份真实记录的导出数量：

| 参照 | 封存运行 | 输入 | 身体采样 | 计时 |
|---|---|---:|---:|---:|
| `pre-floating-7ab0b35f` | `20260915T142028637941Z-8ab3b1a3` | 319 | 319 | 319 |
| `overlap-air-legacy` | `20260916T163223155358Z-acfa57d8` | 1248 | 1248 | 1248 |
| `hierarchical-current` | `20260917T112213243710Z-6eb30c9f` | 326 | 326 | 326 |

## 实际命令

```powershell
D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output `
  python -m unittest discover -s tests/motion_nav -p 'test_*.py' -v

D:\Miniforge3\Scripts\conda.exe run --prefix D:\My_project\mc_ai\.venv --no-capture-output `
  python scripts/motion_navigation_references.py verify `
  --snapshot-root output/github-navigation-code-share-v2
```

真实封存记录另外分别执行了 `normalize`。临时导出只用于验收，核对后删除，避免再次积累实验产物。

## 证据边界

三个参照没有在四个共同场景上形成完整的十二场同条件实测矩阵。注册表中的 `existing_evidence` 只表示当时直接保存的成绩，不能借给另一版本或另一场景。后续 B03 需要比较固定路线步行时，再按同一地图、任务、权限和运行环境产生新的公平成绩。
