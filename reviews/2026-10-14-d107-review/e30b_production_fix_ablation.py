"""E30b：`async_gap_revalidation_delivery` 不依赖测试屏障，它是靠哪处生产修复恢复的。

在去掉 receiver 屏障（`wait_for_receiver` 立即返回）的前提下，进程内分别撤销 D107 的两处生产修复：
  - no-wake：`mark_submitted` 不再唤醒 receiver（恢复 D106 写法）；
  - no-retire：被取代的命令一律不算结束（恢复 D106 的账本规则）；
  - neither：两处都撤销。
若都通过，说明这个模块的恢复主要来自观察提交提速（world_model），而不是这两处修复。
只调用项目函数和项目测试，不改项目文件。运行：PYTHONPATH=<检出> python e30b_production_fix_ablation.py <none|no-wake|no-retire|neither>
"""
import io, sys, unittest
from mc2p.motion_nav import motion_worker as MW, online_motion as OM
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.motion_worker import AsyncWorkIdentity
variant = sys.argv[1]
if variant in ("no-wake", "neither"):
    def mark_submitted(self, identity):
        if type(identity) is not AsyncWorkIdentity:
            raise ContractViolation("typed identity")
        with self._lock:
            record = self._active.get(identity)
            if record is not None and not record.finished:
                record.submitted = True
    MW.MotionResultInbox.mark_submitted = mark_submitted
if variant in ("no-retire", "neither"):
    original = OM.input_application_has_finished
    def finished(record, *, by_tick=None):
        if type(record) is OM.InputApplicationRecord and record.status is OM.InputApplicationStatus.SUPERSEDED:
            return False
        return original(record, by_tick=by_tick)
    for module in [m for m in list(sys.modules.values()) if m and getattr(m, "input_application_has_finished", None) is original]:
        module.input_application_has_finished = finished
MW.MotionResultInbox.wait_for_receiver = lambda self, *, timeout=5.: self._receiver is not None
r = unittest.TextTestRunner(stream=io.StringIO()).run(unittest.defaultTestLoader.loadTestsFromName("tests.motion_nav.test_async_gap_revalidation_delivery"))
print(variant, "ran", r.testsRun, "fail", len(r.failures), "err", len(r.errors))
