"""E129：在 Linux 上运行项目的 D122 Java 门禁测试。

项目测试按 Windows 写：调用 `.venv/Library/bin/javac.exe`、`java.exe`，classpath 用 `;` 分隔，
Gson 从 `.gradle/caches/.../gson/2.10.1/*/*.jar` 取。本脚本不改测试代码，也不改被测 Java 源码：
只在测试模块里替换 `subprocess.run`，把 `javac.exe`／`java.exe` 换成 PATH 上的 JDK 21，
把 classpath 中的 `;` 换成 `:`；Gson 由调用者事先放进上述缓存目录（与 Windows 夹具同一版本 2.10.1）。
然后用 unittest 原样运行项目的测试方法。
用法：python e129_java_gates_linux.py   （在检出根目录运行）
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

_REAL_RUN = subprocess.run


def _linux_run(command, *args, **kwargs):
    translated = []
    for item in command:
        text = str(item)
        if text.endswith(("javac.exe", "java.exe")):
            text = os.path.basename(text)[:-4]
        elif ";" in text and os.sep in text:
            text = text.replace(";", ":")
        translated.append(text)
    return _REAL_RUN(translated, *args, **kwargs)


def main():
    import tests.test_d122_java_permission_bridge as bridge
    import tests.test_d122_java_timing as timing
    import tests.test_standalone_java_gates as gates
    for module in (bridge, timing):
        module.subprocess.run = _linux_run  # 只影响测试模块里的这一处引用
    suite = unittest.TestSuite()
    loader = unittest.defaultTestLoader
    for module in (timing, bridge, gates):
        suite.addTests(loader.loadTestsFromModule(module))
    result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
