"""Task-level choice of the first planning path for geometric goals."""
from enum import StrEnum


class GoalPlanningPolicy(StrEnum):
    BACKGROUND_PLANNER = "background_planner"
    PROVED_LOCAL_DIRECT_THEN_BACKGROUND = (
        "proved_local_direct_then_background"
    )
