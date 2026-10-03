"""Task lifetime after its current geometric goal has been satisfied."""
from enum import StrEnum


class GoalReachPolicy(StrEnum):
    COMPLETE_ON_REACH = "complete_on_reach"
    KEEP_ACTIVE_ON_REACH = "keep_active_on_reach"
