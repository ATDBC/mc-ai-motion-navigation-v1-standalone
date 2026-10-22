# D123 smoke-run-11 原始证据与只读归因

本目录保留正式 launcher 的四个原始文件，并逐字节复制 source run 的 44 个原始文件。`manifest.json` 绑定共 48 项；历史原始字节未改写。

本次归类为 `INVALID_HARNESS_RUN/fixture-scheduling`。这是针对 run-11 raw trace、V4 receipt、F-TICK、physics 和 supervision 的独立只读因果归因，**不表示 run-10 专用的 D129 通用 validator 已覆盖 run-11**。

Anchor 为 observation 8／movement tick 20，parent request 8 的 exact window 为 `[21,21]`。host 在收到 anchor 后又经过 380.016 ms 才提交 intent；客户端在 tick 27 收到 request，并于 tick 28 记为 `window_missed`。request 8 的 input application 为 0，dropped sample 为 0；没有 fallback／default application。tick 20—34 身体位置保持 `(0.5, 64.0, -5.17)`、持续着地，observation 8—13 的 health 均为 20，damage event 为 0。

正式代码只在 parent first result 返回后提交 successor job，因此该 job 已晚于 parent 的 `window_missed`。控制流证明 submit 返回 `ACCEPTED`，但原始证据没有 worker result、inbox、admission、prepared proposal 或 `ACTIVATED`；也没有 successor dispatch／receipt。因此本次不能测量真实 worker 在正常 handoff 中是否及时。

Assembler 对缺失的 successor identity 正确 fail closed，但在 trial JSON 写入前抛错。最后可证 Session 为 `executing`、route 为 `cancelling`；request 13 只有 dispatch，没有 step／receipt。`case.close()` 在 assembler 前返回，但 resource-close payload 没有落盘，也没有 `close_release`，因此不能声称 source 已释放或 worker close report 全绿。Supervision 只证明进程最终全部停止。

有效 smoke 仍为 0／3；matrix 未运行。
