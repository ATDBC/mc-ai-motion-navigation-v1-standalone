# D114 证据

D114 把 B10 协调器矩阵改为逐帧经过正式 `RuntimeNavigationDriver`。唯一一次 Fabric 命令原样记录为 failed：旧量尺把传送复位后坐标不变的一帧 `on_ground=false` 当成离开支撑，因此 10-tick 安全数写成 5/8。

随后只修正量尺，没有重跑 Fabric。只读分析对原始 trial 和 trace 做哈希保护，确认三项都是坐标完全未变的地面标志瞬态；修正后 10-tick 为 8/8 安全有界。baseline 20/20、2/5-tick 16/16、never-return 4/4，零伤害，旧 manual/session 意图从未获胜，来源全部显式释放。

原始失败、修正前字段和完整 gzip 证据全部保留。D110-SOLVE-01 仍打开；本阶段没有授权连续两跳或新动作。
