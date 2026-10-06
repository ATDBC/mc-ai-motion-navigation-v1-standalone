"""Run tests/motion_nav with modules in reverse alphabetical order (same discovery as the forward command).

    python -B reverse_suite.py        (from the repository root)

Several tests start planner workers with the spawn start method, which re-imports the main
module; the guard below keeps those children from running the suite again.
"""
import os
import sys
import unittest


def main():
    sys.path.insert(0, os.getcwd())
    suite = unittest.defaultTestLoader.discover("tests/motion_nav", pattern="test_*.py")
    modules = list(suite)
    modules.reverse()
    result = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(modules))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
