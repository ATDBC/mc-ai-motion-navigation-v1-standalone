"""审查方补充：在 Linux 上重跑项目 M0 的混合反例探针，与项目在 Windows 上的 M0 数字比较单步耗时。

不是任何门槛；只用来估计平台常数。项目 Windows 数字见 be38685 的
docs/motion_navigation/acceptance/continuous-motion-incremental-proof.md（同一探针、同为 10,056 次 physics step）。
运行：在项目 checkout 根目录，PYTHONPATH=. python <本文件>
"""
import os
import platform
import time

from experiments.motion_navigation.trajectory_proto import m0_probe as M

print(platform.system(), platform.release(), platform.python_version(), "loadavg", os.getloadavg())
for index in range(6):
    began = time.perf_counter()
    result = M.run_case("mixed_ground_jump_air", None, options=M.FORMAL_OPTIONS)
    elapsed = (time.perf_counter() - began) * 1000.
    print(index, result.status.value, result.reason, "steps", result.physics_steps,
          f"{elapsed:.1f} ms", f"{elapsed / result.physics_steps * 1000.:.1f} us/step")
