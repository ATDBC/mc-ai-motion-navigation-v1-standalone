"""Public strategy identity, separate from the frozen body configuration."""
from mc2p.skills.normal_navigation_types import NormalNavigationConfig

GOAL_DIRECTED_EXPLORATION = 'goal_directed_exploration'
JOINT_STRATEGIES = frozenset(('D', 'E', GOAL_DIRECTED_EXPLORATION))


def strategy_config(strategy):
    return NormalNavigationConfig(strategy)
